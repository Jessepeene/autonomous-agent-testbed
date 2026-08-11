"""The Bug Council — the deploy gate.

Five reviewers, unanimity required. Two of them are deterministic scanners rather
than model calls, because "did a credential get committed" and "does this ask for
root" are questions with correct answers that must not depend on a model's mood:

* **secrets** — regex + entropy scan, in code.
* **privilege** — pattern scan for privilege escalation and sandbox escape, in code.
* **security**, **performance**, **dependencies** — Claude reviewers, which reason
  about logic rather than pattern-match.

The security reviewer is explicitly tasked with unbounded-loop and compute-burn
detection: this code runs unattended on a metered container, so an accidental
``while True`` is a financial incident, not just a bug.
"""

from __future__ import annotations

import asyncio
import math
import re
from collections import Counter

from factory.llm.providers.base import LLMError
from factory.logging import get_logger
from factory.settings import EngineName, TaskType
from factory.state.schemas import (
    CouncilResult,
    HealingInstructions,
    ReviewCategory,
    ReviewFinding,
    ReviewVerdict,
)
from factory.supervisor.context import FactoryContext

log = get_logger(__name__)

#: A live scaffold has been observed at ~150 KB (15 files, ~37k output tokens) --
#: the model context window is 1M tokens (~4M chars), so there is no technical
#: reason to cap this anywhere near that low. Raised 5x from the original 60_000,
#: which silently dropped files (and, per `render_sources`, gave no indication it
#: had) on any build even modestly larger than a small scaffold; `sorted()` order
#: means whatever sorts alphabetically last (often `tests/`) was always what got
#: cut. Still bounded, not unlimited: three reviewers each pay for this corpus
#: (the first read is cached for the other two, but the first write and any cache
#: miss are not free), and a pathological build should not make one review
#: unboundedly expensive.
_MAX_REVIEW_CHARS = 300_000

REVIEWER_PROMPTS: dict[ReviewCategory, str] = {
    ReviewCategory.SECURITY: """\
You are the security reviewer on a deploy gate. Approve only if you would put this \
code on the public internet, unattended, tonight.

Look for: injection (SQL, command, template), missing authentication or authorisation \
on any state-changing endpoint, IDOR, unvalidated redirects, unsafe deserialisation, \
path traversal in any file operation, secrets read from anywhere other than the \
environment, and CORS or cookie settings that expose sessions.

Also look specifically for compute-burn defects, because this runs unattended on a \
metered container: unbounded loops, recursion without a depth limit, retry loops with \
no maximum attempts, polling with no backoff, sleeps longer than five seconds, and \
unbounded queue or list growth. Treat these as high severity — they cost real money \
every second they run.

Set approved=false if you find anything of medium severity or above.""",
    ReviewCategory.PERFORMANCE: """\
You are the performance reviewer on a deploy gate.

Look for: N+1 query patterns, queries without indexes on their filter columns, \
synchronous blocking calls inside async handlers, unbounded result sets returned to \
clients, missing pagination, work that belongs in a background task sitting in a \
request handler, and per-request construction of expensive clients.

Set approved=false for anything that would fall over under modest concurrent load.""",
    ReviewCategory.DEPENDENCIES: """\
You are the dependency reviewer on a deploy gate.

Look for: unpinned or loosely-pinned versions, packages that are abandoned or \
typosquat a popular name, dependencies pulled in for a single trivial function, \
licences incompatible with commercial use (AGPL, SSPL), and any dependency that is \
not actually imported anywhere.

Set approved=false for an unvetted or unnecessary dependency.""",
}

# --- deterministic scanners -------------------------------------------------

_SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("aws_access_key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("private_key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |PGP )?PRIVATE KEY-----")),
    ("anthropic_key", re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{20,}")),
    ("openai_key", re.compile(r"\bsk-[A-Za-z0-9]{32,}\b")),
    ("google_key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("stripe_key", re.compile(r"\b(?:sk|rk)_(?:live|test)_[0-9A-Za-z]{16,}\b")),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b")),
    ("slack_token", re.compile(r"\bxox[abprs]-[0-9A-Za-z-]{10,}\b")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b")),
)

_ASSIGNED_SECRET = re.compile(
    r"""(?ix)
    \b(password|passwd|secret|token|api[_-]?key|access[_-]?key|private[_-]?key)\b
    \s*[:=]\s*
    ['"]([^'"\s]{12,})['"]
    """
)

_ENV_LOOKUP = re.compile(r"(?i)os\.environ|getenv|process\.env|settings\.|config\.")

_PRIVILEGE_PATTERNS: tuple[tuple[str, re.Pattern[str], str], ...] = (
    ("docker_privileged", re.compile(r"--privileged\b"), "requests a privileged container"),
    ("docker_socket", re.compile(r"/var/run/docker\.sock"), "mounts the host Docker socket"),
    ("cap_add", re.compile(r"--cap-add\b"), "adds Linux capabilities"),
    ("sudo", re.compile(r"(?m)^\s*sudo\s+"), "invokes sudo"),
    ("setuid", re.compile(r"\bos\.set[ug]id\b"), "changes process uid/gid"),
    ("chmod_777", re.compile(r"chmod\s+(?:-R\s+)?0?777\b"), "makes files world-writable"),
    ("run_as_root", re.compile(r"(?im)^\s*USER\s+root\s*$"), "runs the container as root"),
    ("shell_true", re.compile(r"shell\s*=\s*True"), "executes a shell with shell=True"),
    ("eval_exec", re.compile(r"\b(?:eval|exec)\s*\("), "evaluates code at runtime"),
)

_HIGH_ENTROPY_MIN = 4.0


def _entropy(value: str) -> float:
    if not value:
        return 0.0
    counts = Counter(value)
    length = len(value)
    return -sum((n / length) * math.log2(n / length) for n in counts.values())


def scan_secrets(sources: dict[str, str]) -> ReviewVerdict:
    """Deterministic credential scan. No model involved."""
    findings: list[ReviewFinding] = []
    for path, content in sources.items():
        for line_no, line in enumerate(content.splitlines(), start=1):
            for name, pattern in _SECRET_PATTERNS:
                if pattern.search(line):
                    findings.append(
                        _finding(
                            ReviewCategory.SECRETS,
                            "critical",
                            path,
                            line_no,
                            f"hard-coded {name} detected",
                            "The credential ships in the repository and is compromised "
                            "the moment the code is published.",
                            "Remove the literal and read it from an environment variable.",
                        )
                    )
            match = _ASSIGNED_SECRET.search(line)
            if match and not _ENV_LOOKUP.search(line):
                value = match.group(2)
                if _entropy(value) >= _HIGH_ENTROPY_MIN:
                    findings.append(
                        _finding(
                            ReviewCategory.SECRETS,
                            "high",
                            path,
                            line_no,
                            f"literal assigned to {match.group(1)!r}",
                            "A high-entropy literal in a credential-named variable is a "
                            "committed secret.",
                            "Read the value from os.environ instead.",
                        )
                    )
    return ReviewVerdict(
        category=ReviewCategory.SECRETS, approved=not findings, findings=findings[:40]
    )


def scan_privilege(sources: dict[str, str]) -> ReviewVerdict:
    """Deterministic privilege-escalation and sandbox-escape scan."""
    findings: list[ReviewFinding] = []
    for path, content in sources.items():
        for line_no, line in enumerate(content.splitlines(), start=1):
            for name, pattern, description in _PRIVILEGE_PATTERNS:
                if pattern.search(line):
                    escape = name in {"docker_privileged", "docker_socket"}
                    findings.append(
                        _finding(
                            ReviewCategory.PRIVILEGE,
                            "critical" if escape else "high",
                            path,
                            line_no,
                            f"{name}: code {description}",
                            "Generated code runs unreviewed; anything that raises its own "
                            "privileges defeats the sandbox that contains it.",
                            "Remove the escalation and run with the least privilege that works.",
                        )
                    )
    return ReviewVerdict(
        category=ReviewCategory.PRIVILEGE, approved=not findings, findings=findings[:40]
    )


def _finding(
    category: ReviewCategory,
    severity: str,
    path: str,
    line: int,
    summary: str,
    failure_scenario: str,
    remediation: str,
) -> ReviewFinding:
    return ReviewFinding(
        category=category,
        severity=severity,  # type: ignore[arg-type]
        file=path[:500],
        line=line,
        summary=summary[:500],
        failure_scenario=failure_scenario[:1500],
        remediation=remediation[:1500],
    )


# --- model reviewers --------------------------------------------------------


def render_sources(sources: dict[str, str], limit: int | None = None) -> tuple[str, bool]:
    """Returns ``(rendered, truncated)``. When the budget runs out, an explicit
    marker naming every omitted or partially-shown file is appended -- a reviewer
    silently handed a partial corpus (the previous behaviour) cannot account for
    what it never saw, and `review_corpus` used to actively tell it "the complete
    codebase under review follows" even when that was false.

    ``limit`` defaults to the module-level ``_MAX_REVIEW_CHARS``, read at call
    time (not baked in as a parameter default) so a test can monkeypatch it.
    """
    if limit is None:
        limit = _MAX_REVIEW_CHARS
    parts: list[str] = []
    budget = limit
    omitted: list[str] = []
    cut_short: str | None = None
    paths = list(sources)
    for index, path in enumerate(paths):
        content = sources[path]
        chunk = f"--- {path} ---\n{content}\n"
        if len(chunk) > budget:
            parts.append(chunk[:budget])
            cut_short = path
            omitted = paths[index + 1 :]
            break
        parts.append(chunk)
        budget -= len(chunk)
    truncated = cut_short is not None
    if truncated:
        names = ([cut_short] if cut_short else []) + omitted
        parts.append(
            "\n\n[TRUNCATED: the review budget ran out. The following file was cut "
            f"off mid-content or never shown at all: {', '.join(names)}]\n"
        )
    return "".join(parts), truncated


def review_corpus(rendered: str, *, truncated: bool = False) -> str:
    """Byte-identical across all three model reviewers — any variation here defeats
    the cache, so `convene` computes this once and passes the same string to each.
    Document-then-instruction is also the better prompt order for long context, which
    is what moving the corpus ahead of the per-reviewer mandate does anyway."""
    if truncated:
        return (
            "The codebase under review follows. IMPORTANT: it was too large to show "
            "in full -- see the [TRUNCATED] marker near the end for exactly what you "
            "were not shown. Do not assume anything you cannot see is safe; if a file "
            "or region relevant to your mandate was omitted, say so explicitly and "
            "withhold approval rather than reasoning about code you never saw.\n\n"
            f"{rendered}"
            "\n\nEnd of codebase (partial -- see the TRUNCATED marker above). Your "
            "specific review mandate follows."
        )
    return (
        "The complete codebase under review follows.\n\n"
        f"{rendered}"
        "\n\nEnd of codebase. Your specific review mandate follows."
    )


class CouncilUnavailable(RuntimeError):
    """One or more reviewers could not complete -- an infrastructure/provider
    failure, not a verdict on the code. Must never be silently converted into a
    normal `approved=False` rejection: that would route into the ordinary heal
    cycle, spending an LLM call regenerating code that was never actually
    reviewed, let alone found bad. Callers should treat this like any other
    `DevOpsError` (see `development/graph.py::_verify_and_ship`).
    """


async def _model_review(
    ctx: FactoryContext,
    category: ReviewCategory,
    corpus: str,
    *,
    task_id: str | None = None,
) -> ReviewVerdict:
    """Raises whatever the provider call raises -- callers decide what an
    incomplete review means; this function does not silently turn "could not run"
    into "ran and rejected"."""
    return await ctx.llm.parse(
        ReviewVerdict,
        task_type=TaskType.CODE_REVIEW,
        system=REVIEWER_PROMPTS[category],
        prompt="Emit your verdict for the codebase above.",
        engine=EngineName.DEVELOPMENT,
        task_id=task_id,
        cache_prefix=corpus,
    )


async def convene(
    ctx: FactoryContext, sources: dict[str, str], *, task_id: str | None = None
) -> CouncilResult:
    """Run all five reviewers. Deploy requires unanimous approval."""
    if not sources:
        return CouncilResult(
            approved=False,
            verdicts=[],
            healing=HealingInstructions(
                summary="No source files were produced, so there is nothing to deploy.",
                ordered_steps=["Regenerate the scaffold and ensure files are written to disk."],
            ),
        )

    rendered, truncated = render_sources(sources)
    corpus = review_corpus(rendered, truncated=truncated)
    if truncated:
        log.warning("council.corpus_truncated", files=len(sources), limit=_MAX_REVIEW_CHARS)
    categories = list(REVIEWER_PROMPTS)

    # The shared corpus is a cached prefix (see `_model_review`/`_system_blocks`),
    # but a cache entry is only readable once the write that created it has actually
    # landed. Firing all three requests at once means every one of them misses —
    # worse, each pays the 1.25x cache-*write* premium with nothing ever reading it
    # back, a net loss over not caching at all. Running the first alone lets its
    # write land before the other two are sent, so they can read it at 0.1x instead.
    #
    # return_exceptions=True on the gather (and a try/except around the solo first
    # call): a raised exception here is never routed into `_finding()`-style
    # "reviewer rejected" bookkeeping. Without return_exceptions, one reviewer
    # raising would cancel this coroutine's await on the gather while the *other*
    # sibling reviewer tasks keep running in the background -- a real, billed
    # provider call whose result is never retrieved or recorded.
    try:
        first_verdict: ReviewVerdict | BaseException = await _model_review(
            ctx, categories[0], corpus, task_id=task_id
        )
    except LLMError as exc:
        first_verdict = exc
    rest = await asyncio.gather(
        *(_model_review(ctx, category, corpus, task_id=task_id) for category in categories[1:]),
        return_exceptions=True,
    )

    outcomes: list[ReviewVerdict | BaseException] = [first_verdict, *rest]
    failures = [outcome for outcome in outcomes if isinstance(outcome, BaseException)]
    if failures:
        detail = "; ".join(str(f)[:200] for f in failures)
        log.warning("council.unavailable", failed_reviewers=len(failures), detail=detail)
        raise CouncilUnavailable(f"{len(failures)} reviewer(s) could not complete: {detail}")

    model_verdicts = [outcome for outcome in outcomes if isinstance(outcome, ReviewVerdict)]
    verdicts = [scan_secrets(sources), scan_privilege(sources), *model_verdicts]

    approved = all(v.approved for v in verdicts)
    log.info(
        "council.verdict",
        approved=approved,
        blocked_by=[str(v.category) for v in verdicts if not v.approved],
    )

    result = CouncilResult(approved=approved, verdicts=verdicts)
    if approved:
        return result
    return CouncilResult(
        approved=False,
        verdicts=verdicts,
        healing=build_healing(result),
    )


def build_healing(result: CouncilResult) -> HealingInstructions:
    """Turn blocking findings into an ordered, actionable repair list."""
    blocking = result.blocking
    severity_rank = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    ordered = sorted(blocking, key=lambda f: severity_rank.get(f.severity, 9))

    steps = [
        f"[{f.severity}] {f.file}:{f.line or 0} — {f.summary}. {f.remediation}"
        for f in ordered[:25]
    ] or ["Address the reviewer feedback and resubmit."]

    categories = sorted({str(v.category) for v in result.verdicts if not v.approved})
    return HealingInstructions(
        summary=(
            f"Deploy blocked by {len(categories)} reviewer(s): {', '.join(categories)}. "
            f"{len(blocking)} finding(s) must be resolved."
        )[:1000],
        ordered_steps=steps,
        blocking_findings=ordered[:40],
    )
