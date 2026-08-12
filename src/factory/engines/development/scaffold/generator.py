"""MVP scaffolding.

Claude writes the product; this module writes it to disk safely. Every generated path
is resolved and checked against the workspace root before a single byte is written,
because a model-supplied path like ``../../.env`` is a plausible output and must be a
rejected write rather than an exfiltrated credential.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from factory.logging import get_logger
from factory.sandbox.runner import resolve_workspace
from factory.settings import EngineName, Settings, TaskType
from factory.state.schemas import ScaffoldPlan, ValidatedIdea
from factory.supervisor.context import FactoryContext

log = get_logger(__name__)

SYSTEM_PROMPT = """\
You are the engineering agent of an autonomous SaaS factory. You produce a complete, \
runnable MVP for one validated idea.

The MVP must include, as actual files with real contents:
- A FastAPI backend with a /health endpoint and the core feature endpoints.
- A minimal single-page frontend (plain HTML/CSS/JS, no build step).
- Token-based authentication with password hashing. Never store plaintext passwords.
- Stripe Checkout billing wiring, reading the key from an environment variable. Every
  Checkout Session you create must set client_reference_id to the exact value of the
  PRODUCT_ID environment variable, read at request time. Never hard-code it, never
  omit it — it is the only way a payment can be attributed back to this product.
- An analytics beacon: a small inline script on the frontend's page load that POSTs
  JSON {"path": ..., "referrer": ...} to the exact analytics URL given below.
  Fire-and-forget — never block page rendering on it, and never surface a failure
  to the visitor.
- SEO hygiene on the frontend page: a non-empty <title>, a <meta name="description"
  content="..."> summarising what the product does, Open Graph tags (og:title,
  og:description), a <link rel="canonical"> pointing at the product's own root
  URL, and semantic landmark elements (<header>, <main>, <nav> as applicable)
  rather than an unstructured soup of <div>s. Also produce two standalone files:
  sitemap.xml (listing the root URL) and robots.txt (allowing all crawlers).
- A pytest suite covering the core feature and the auth boundary.
- requirements.txt with a minimum-version lower bound per package (e.g. \
"pydantic>=2.9,<3"), and a README with run instructions. The sandbox installs \
nothing beyond this file, so requirements.txt must list pytest itself, not just \
the packages the tests import.

Hard constraints:
- Every file must be complete. No TODOs, no placeholders, no "implement this".
- Read every secret from os.environ. Never hard-code a key, password, or token, not \
even a fake one.
- Do not write files outside the project directory. All paths are relative, with no \
leading slash and no "..".
- No unbounded loops, no unbounded recursion, no retry loop without a maximum, and \
no sleep longer than 5 seconds. This code runs unattended in a metered container.
- The analytics beacon's fetch/XHR target must be the EXACT analytics URL given in \
the prompt, verbatim. Do not invent your own endpoint path or guess at one.
- test_command must be a single shell command that runs the suite.
- requirements.txt runs on Python 3.13 in a container with no compiler toolchain and \
no writable-and-executable temp directory (/tmp is mounted noexec). Never pin an \
exact old version of a package with a compiled extension (pydantic, cryptography, \
orjson, and similar) — an old exact pin can predate the prebuilt wheel for Python \
3.13, which forces pip to compile from source, which cannot succeed here. Observed \
live: "pydantic==2.6.4" forced a from-source build of pydantic-core that failed \
trying to install a Rust toolchain. Use a lower-bound range instead, so pip resolves \
a release recent enough to ship a Python 3.13 wheel.

Request/response validation:
- Define an explicit Pydantic model for every request body and response — never a \
bare dict. Give every field a type and, where it matters, a Field(...) constraint \
(min_length, ge/le, a regex) so bad input is rejected by the framework before your \
handler code ever runs.
- Do not silently catch pydantic.ValidationError to paper over bad input. Let FastAPI \
return its own 422 with the validation detail — a caller needs to see exactly which \
field failed, not a generic 400 or a 500.
- Test the validation boundary explicitly: at least one test per endpoint asserting \
that malformed input is rejected with 422, not merely that valid input succeeds.

If you are patching an existing product because a previous build failed, the failure
text may include raw stderr from the sandbox. Two categories, and they call for
different fixes:
- Python tracebacks, assertion failures, and HTTP status mismatches are your code.
  Fix the logic.
- Lines like "permission denied", "command not found", "no space left on device", \
"cannot connect to the docker daemon", or a bare exit code with no Python traceback \
at all are the *environment*, not your code — rewriting application logic will not \
fix a missing binary or a host permission. If everything you are shown is one of \
these, say so plainly in your summary rather than guessing at a code change: change \
nothing risky, and if a dependency genuinely needs pinning to a specific version or \
platform to avoid the failure, do only that.

If you are shown the CURRENT CONTENTS of an existing workspace, this is a repair
pass on real, already-written code — not a blank slate. Keep every existing file
path and module name exactly as given wherever the fix allows it, and edit those
files in place. Do not invent a second, differently-named copy of the app (e.g.
a new "backend/" tree alongside an existing "app/") to work around a problem —
that produces two divergent implementations of the same bugs, which is worse than
either one. Only add a new file for functionality that genuinely does not exist
yet.\
"""


#: Mutable via the Learning Loop (see learning/allowlist.py) -- unlike SYSTEM_PROMPT
#: above, which is permanently excluded from mutation.
LANDING_PAGE_SYSTEM = """\
You are the validation agent of an autonomous SaaS factory. Before any real product \
is built, you produce a single, cheap pitch page that measures whether real people \
actually want it.

The page must include, as actual files with real contents:
- One static HTML page (plain HTML/CSS/JS, no build step, no framework) that pitches \
the idea clearly: the problem it solves, who it's for, and why it's worth paying for. \
Visual polish matters directly here: this page's only job is to earn a stranger's \
trust in the few seconds before they decide whether to leave an email address, and a \
generic-template or amateurish look will suppress genuine interest regardless of how \
good the idea is — a low response rate must reflect the idea, not the page. Use a \
clear visual hierarchy, real (not lorem-ipsum) and specific copy, professional and \
credible styling, and a mobile-responsive layout.
- An interest-capture form (email, and an optional short message) that POSTs as JSON \
to the exact interest URL given below. On success, replace the form with a short \
thank-you message. On failure, show an error and leave the form intact so the visitor \
can retry.
- An analytics beacon: a small inline script that runs on page load and POSTs JSON \
{"path": ..., "referrer": ...} to the exact analytics URL given below. \
Fire-and-forget — never block page rendering on it, and never surface a failure to \
the visitor.
- SEO hygiene: a non-empty <title>, a <meta name="description" content="..."> \
summarising the pitch, Open Graph tags (og:title, og:description), a \
<link rel="canonical"> pointing at the page's own root URL, and semantic landmark \
elements (<header>, <main>) rather than an unstructured soup of <div>s. Also \
produce two standalone files: sitemap.xml (listing the root URL) and robots.txt \
(allowing all crawlers) — a page nobody can find gathers no interest signal at all.
- A pytest suite that serves the page (e.g. via FastAPI's StaticFiles, or an \
equivalent minimal static server) and asserts the page loads and contains the pitch \
and the form.
- requirements.txt with a minimum-version lower bound per package (see the pinning \
rule below), and a README with run instructions. The sandbox installs nothing \
beyond this file, so requirements.txt must list pytest itself, not just the \
packages the tests import.

This is deliberately NOT a real product:
- No authentication, no user accounts, no database beyond nothing.
- No Stripe or any billing integration.
- No backend business logic of any kind beyond serving the static page. If a minimal \
backend is needed to serve the page, its only job is that.

Hard constraints:
- Every file must be complete. No TODOs, no placeholders, no "implement this".
- Do not write files outside the project directory. All paths are relative, with no \
leading slash and no "..".
- test_command must be a single shell command that runs the suite.
- requirements.txt runs on Python 3.13 in a container with no compiler toolchain and \
no writable-and-executable temp directory (/tmp is mounted noexec). Never pin an \
exact old version of a package with a compiled extension — use a minimum-version \
lower bound instead (e.g. "fastapi>=0.115,<1"), so pip resolves a release recent \
enough to ship a Python 3.13 wheel.
- The interest form's fetch/XHR target must be the EXACT interest URL given in the \
prompt, verbatim. Do not invent your own endpoint path or guess at one.
- The analytics beacon's fetch/XHR target must be the EXACT analytics URL given in \
the prompt, verbatim. Do not invent your own endpoint path or guess at one.\
"""


#: Appended to `LANDING_PAGE_SYSTEM`'s prompt only when a live AB test is running
#: for this product (see `development/ab_testing.py`). Deliberately a client-side
#: split, not two deployments behind an edge router: it fits the existing "single
#: static deploy, no build step" constraint with no new hosting mechanism, and it
#: reuses the interest-capture and analytics endpoints exactly as they already
#: exist -- only one new field (`variant`) is added to each POST body.
_AB_TEST_INSTRUCTIONS = """\

This page is running a live AB test. Render BOTH variants below in the SAME page \
as two separate content blocks, exactly `<div id="variant-a">...</div>` and \
`<div id="variant-b">...</div>`, each a complete, fully-styled pitch on its own \
(not a diff against the other) built from its own brief:

Variant A brief: {variant_a_brief}

Variant B brief: {variant_b_brief}

Add a small inline script, run before either block is shown, that:
1. Reads `localStorage.getItem('ab_variant')`. If empty, picks "a" or "b" with \
equal probability and stores it with `localStorage.setItem('ab_variant', ...)` -- \
so a returning visitor always sees the same variant they saw before.
2. Shows only the chosen variant's block (`hidden = false`) and hides the other \
(`hidden = true`).
3. Includes `variant: <the chosen value>` in the JSON body of BOTH the interest-form \
POST and the analytics beacon POST -- this is the only way results can be told \
apart per arm afterward.\
"""


class UnsafeGeneratedPath(ValueError):
    """A generated file path tried to escape the product workspace."""


async def generate_scaffold(
    ctx: FactoryContext,
    idea: ValidatedIdea,
    *,
    product_id: str,
    task_id: str | None = None,
    existing_files: dict[str, str] | None = None,
) -> ScaffoldPlan:
    """``existing_files`` (from `read_workspace`) turns this into a repair pass:
    the model sees exactly what is already on disk instead of regenerating a whole
    MVP blind to its own previous attempt. Without this, two generations for the
    same product routinely disagree on file layout (one calls its package `app/`,
    the next `backend/`), and since `write_plan` only ever adds files, both
    versions -- and both sets of bugs -- pile up in the workspace side by side.
    Observed live: Bug Council rejected a build for exactly this, having found the
    same N+1 query bug duplicated across `app/main.py` and `backend/main.py`.

    ``product_id`` is required (unlike a real backend, which reads PRODUCT_ID from
    its own environment at request time for Stripe attribution) because the
    frontend's analytics beacon is static JS with no server-side templating and no
    env var to read -- the exact URL has to be composed here and handed to the
    model, the same reason `generate_landing_page` already does this for its
    interest-capture URL.
    """
    analytics_url = f"https://{ctx.settings.public_domain}/analytics/{product_id}"
    prompt = (
        f"Validated idea:\n"
        f"- problem: {idea.problem_statement}\n"
        f"- target niche: {idea.target_niche}\n"
        f"- monetization: {idea.monetization_hypothesis}\n"
        f"- evidence: {idea.source_url}\n\n"
        f'Analytics beacon URL (POST JSON {{"path": ..., "referrer": ...}} here, '
        f"exactly, verbatim): {analytics_url}\n\n"
    )
    if existing_files:
        prompt += (
            "CURRENT CONTENTS of the workspace (this is a repair pass -- see the "
            "system prompt for how to treat these):\n\n"
            + "\n\n".join(
                f"--- {path} ---\n{contents}" for path, contents in existing_files.items()
            )
            + "\n\n"
        )
    prompt += "Produce the complete MVP."
    return await ctx.llm.parse(
        ScaffoldPlan,
        task_type=TaskType.CODE_GENERATION,
        system=SYSTEM_PROMPT,
        prompt=prompt,
        engine=EngineName.DEVELOPMENT,
        task_id=task_id,
    )


async def generate_landing_page(
    ctx: FactoryContext,
    idea: ValidatedIdea,
    *,
    product_id: str,
    system: str = LANDING_PAGE_SYSTEM,
    tunable_parameter_id: str | None = None,
    task_id: str | None = None,
    existing_files: dict[str, str] | None = None,
    ab_variants: tuple[str, str] | None = None,
) -> ScaffoldPlan:
    """A cheap, pitch-only page for the idea validation gate -- see
    `development/graph.py::_build_landing_page`. Unlike `generate_scaffold` (whose
    SYSTEM_PROMPT is permanently excluded from the Learning Loop), the caller is
    expected to have already resolved `system`/`tunable_parameter_id` via
    `learning_prompts.get_prompt(key=..., default=LANDING_PAGE_SYSTEM)` -- this
    prompt's copy is meant to self-improve over time.

    The interest-capture URL is baked into the prompt, not left for the model to
    guess: a static page has no server-side env var to read PRODUCT_ID from the way
    a real backend does (see `deploy.py::_runtime_env`), so `settings.public_domain`
    + `product_id` are composed here into the one exact URL the model must use. The
    analytics beacon URL is composed the same way, for the same reason.

    ``ab_variants``, when given (the two variant briefs, "a" then "b"), switches
    the prompt into the dual-variant pattern -- see `_AB_TEST_INSTRUCTIONS`. Comes
    from `repository.get_running_ab_test`, checked by the caller before every
    landing-page (re)generation, not threaded through the task payload -- so a
    repair pass mid-test still regenerates both variants correctly without
    needing to know a test was involved.
    """
    interest_url = f"https://{ctx.settings.public_domain}/interest/{product_id}"
    analytics_url = f"https://{ctx.settings.public_domain}/analytics/{product_id}"
    prompt = (
        f"Validated idea:\n"
        f"- problem: {idea.problem_statement}\n"
        f"- target niche: {idea.target_niche}\n"
        f"- monetization: {idea.monetization_hypothesis}\n"
        f"- evidence: {idea.source_url}\n\n"
        f'Interest capture URL (POST JSON {{"email": ..., "message": ...}} here, '
        f"exactly, verbatim): {interest_url}\n\n"
        f'Analytics beacon URL (POST JSON {{"path": ..., "referrer": ...}} here, '
        f"exactly, verbatim): {analytics_url}\n\n"
    )
    if ab_variants is not None:
        variant_a_brief, variant_b_brief = ab_variants
        prompt += _AB_TEST_INSTRUCTIONS.format(
            variant_a_brief=variant_a_brief, variant_b_brief=variant_b_brief
        )
    if existing_files:
        prompt += (
            "CURRENT CONTENTS of the workspace (this is a repair pass -- see the "
            "system prompt for how to treat these):\n\n"
            + "\n\n".join(
                f"--- {path} ---\n{contents}" for path, contents in existing_files.items()
            )
            + "\n\n"
        )
    prompt += "Produce the complete validation page."
    return await ctx.llm.parse(
        ScaffoldPlan,
        task_type=TaskType.CODE_GENERATION,
        system=system,
        prompt=prompt,
        engine=EngineName.DEVELOPMENT,
        task_id=task_id,
        tunable_parameter_id=tunable_parameter_id,
    )


def safe_target(root: Path, relative: str) -> Path:
    """Resolve a generated path inside ``root`` or refuse it."""
    candidate = (root / relative).resolve()
    if candidate == root or root not in candidate.parents:
        raise UnsafeGeneratedPath(f"generated path escapes workspace: {relative!r}")
    return candidate


def clear_workspace(workspace: str, settings: Settings | None = None) -> None:
    """Wipe everything under the workspace root before `write_plan` writes a fresh
    scaffold.

    `write_plan` only ever adds files -- it never removes ones that existed from a
    previous attempt. Every heal cycle, and every `build_mvp` retried after a crash
    (both reuse the same product-scoped workspace directory), call `generate_scaffold`
    again, and two generations rarely agree on exact file layout. Without clearing
    first, stale files from the previous attempt silently persist alongside the new
    ones regardless of how well the model reuses the existing structure -- this is
    the deterministic guarantee; the prompt guidance in `generate_scaffold` is the
    complementary attempt to avoid needing it in the first place.
    """
    root = resolve_workspace(workspace, settings)

    def _log_delete_failure(func: object, path: str, exc: BaseException) -> None:
        # ignore_errors=True previously swallowed every deletion failure (a
        # permission error, a file locked by another process, a symlink) with no
        # trace at all -- exactly the "stale files silently persist" failure this
        # function exists to prevent, just one layer deeper: the delete itself
        # silently failing instead of the write silently not clearing first.
        log.warning(
            "scaffold.clear_workspace_failed",
            workspace=workspace,
            path=path,
            detail=str(exc)[:200],
        )

    for child in root.iterdir():
        if child.is_dir():
            shutil.rmtree(child, onexc=_log_delete_failure)
        else:
            child.unlink(missing_ok=True)


def write_plan(plan: ScaffoldPlan, workspace: str, settings: Settings | None = None) -> list[str]:
    """Materialise the plan into the product workspace. Returns the paths written."""
    root = resolve_workspace(workspace, settings)
    written: list[str] = []
    for generated in plan.files:
        target = safe_target(root, generated.path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(generated.contents, encoding="utf-8")
        written.append(str(target.relative_to(root)))
    log.info("scaffold.written", workspace=workspace, files=len(written))
    return written


#: Shared by every reader of a generated workspace -- `read_workspace` (what Bug
#: Council reviews) and `deploy._collect_files` (what actually ships) must exclude
#: exactly the same directories, or something can ship that was never reviewed by
#: anyone. `.deps`: where _verify_and_ship's install step puts dependencies (see
#: graph.py) so they survive into the separate test-run container -- without this,
#: Bug Council reviews third-party library source as if it were the model's own
#: code (pytest/sqlalchemy/pydantic's legitimate internal eval()/exec()/shell=True
#: usage tripped the privilege scanner and blocked every deploy). `.pytest_cache`:
#: sorts early (dot-prefixed) and is pure post-test-run artifact, never source.
#: `.factory`: the durable per-product knowledge file a repair agent reads before it
#: starts (see `development/repair.py`). It is notes *about* the product, not part of
#: it -- reviewing it would spend Council tokens on prose no user runs, and shipping
#: it would publish the factory's internal commentary alongside the product.
WORKSPACE_SKIP_DIRS = frozenset(
    {
        ".git",
        "__pycache__",
        "node_modules",
        ".venv",
        "venv",
        "dist",
        "build",
        ".deps",
        ".pytest_cache",
        ".factory",
    }
)


def read_workspace(workspace: str, settings: Settings | None = None) -> dict[str, str]:
    """Read back the generated source for review. Skips binaries and vendored trees."""
    root = resolve_workspace(workspace, settings)
    sources: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if any(part in WORKSPACE_SKIP_DIRS for part in path.parts):
            continue
        try:
            sources[str(path.relative_to(root))] = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
    return sources
