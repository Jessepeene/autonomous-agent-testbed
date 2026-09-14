"""The one comparison: does this build meet what it was for?

Jesse's loop, which is the whole of it::

    test/review don't meet requirements -> turn these gaps into issues
    -> fix issues -> done

This module is the first arrow. Everything that judged a build before this judged
it against something *other* than its purpose -- the codegen contract against a
hardcoded list of environment facts, Bug Council against six fixed categories --
and neither of them can read the requirements at all. That is not a small omission:
the two judges that could veto a release were the two that did not know what the
product was supposed to do.

**Gaps are finite. Findings are infinite.** A gap is the distance between a product
and a stated standard, so fixing one removes it, and when there are none the work is
done -- a state the old gate could not reach, because "no outstanding findings" is
unbounded and there is always another unbounded cache or missing rate limit. The
observed cost of that was 19 issues becoming 28 in a single cycle while the thing
the product was actually for went unexamined.

A worked example of what only this can see. A landing page was repaired until it
satisfied every gate: contract check green, tests passing, Bug Council's six
categories quiet. It runs an analytics beacon and tells its visitors, in its own
copy, "No spam, no newsletter, no tracking scripts." Every existing judge passes it.
"The page says it does X while doing Y" is a gap against intent, and it is invisible
to anything that reviews categories.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from factory import issues
from factory.engines.development.bug_council import render_sources, review_corpus
from factory.engines.development.requirements import (
    HOUSE_STYLE,
    render_design_for_prompt,
    render_for_prompt,
)
from factory.logging import get_logger
from factory.settings import EngineName, TaskType
from factory.state import repository
from factory.state.schemas import GapReport, WorkRequirements
from factory.supervisor.context import FactoryContext

log = get_logger(__name__)

#: A MUST is what the product had to do, so an unmet one is high severity and will
#: hold a release on its own. A SHOULD is a trade -- worth recording, never worth
#: blocking on, which is the distinction that lets a product be done without being
#: perfect.
_TIER_SEVERITY = {"must": "high", "should": "low"}

GAP_SYSTEM = """\
You compare a built product against the acceptance criteria it was built to meet, \
and report only the difference.

You are not reviewing the code. Do not report security holes, performance problems, \
missing rate limits, or anything else you happen to notice -- other reviewers do \
that, and a list of everything wrong is exactly what stops products ever shipping. \
Report only whether each criterion is met.

For every criterion, decide one of three things:

- **met**: the product does this, and you can point at what shows it.
- **gap**: the product does not do this. Say what you found instead -- the specific \
file, line, string or missing element. "Not implemented" is not evidence.
- **undetermined**: you cannot tell from what you were given. Say so rather than \
guessing. A criterion about what a visitor sees after submitting a form usually \
cannot be settled by reading source, and claiming it passed because nothing looked \
wrong is the failure this whole comparison exists to prevent.

Two things to watch for, because they are the gaps other reviewers structurally \
cannot see:

- A product that *says* one thing and *does* another. Copy promising a feature that \
is not built, a privacy claim contradicted by a script on the page, a price in the \
text that differs from the one in the code. These pass every category-based review.
- Something present that the criteria list as out of scope. That is a gap too: it \
was deliberately not wanted, and building it anyway is as wrong as leaving a \
requirement unbuilt.

Never invent a criterion. Judge only what you were given.\
"""


async def find_requirement_gaps(
    ctx: FactoryContext,
    *,
    sources: dict[str, str],
    requirements: WorkRequirements,
    task_id: str | None = None,
) -> GapReport | None:
    """Compare a build against its acceptance criteria. ``None`` if it could not run.

    A ``None`` return leaves the caller on whatever it did before rather than
    blocking a deploy on a model call -- the same degradation stance the rest of the
    development path takes. Failing to reach a comparison must not become an outage.
    """
    rendered, truncated = render_sources(sources)
    corpus = review_corpus(rendered, truncated=truncated)
    prompt = (
        f"{corpus}\n\n"
        "The acceptance criteria this product was built to meet:\n\n"
        f"{render_for_prompt(requirements)}\n\n"
        # Design is judged here for the first time. Until now "it looks
        # machine-generated" was true, obvious to anyone who opened the page, and
        # invisible to every automated judge -- so a build could meet every
        # criterion and still read as generated, which is exactly what shipped and
        # what only a human caught. Guidelines written to be falsifiable make that
        # a gap like any other.
        f"{render_design_for_prompt(HOUSE_STYLE)}\n\n"
        "Report which criteria are met, which are gaps, and which you could not "
        "determine from the code above. Judge the design guidelines the same way: "
        "a page using a framework's stock palette or no webfont at all is a gap "
        "against a stated guideline, not a matter of taste."
    )
    try:
        return await ctx.llm.parse(
            GapReport,
            task_type=TaskType.ANALYSIS,
            system=GAP_SYSTEM,
            prompt=prompt,
            engine=EngineName.DEVELOPMENT,
            task_id=task_id,
            # Sized here rather than inherited from ANALYSIS's 8_192 default, which
            # thinking consumed entirely before a single gap was emitted -- the same
            # per-category-constant problem as the old flat result cap. `parse`
            # already takes this, so the caller that knows the shape of the call is
            # the right place to set it: this one reads an entire codebase against a
            # list of criteria and answers about every one of them.
            max_tokens=32_000,
        )
    except Exception as exc:
        log.warning("gaps.comparison_failed", detail=str(exc)[:200])
        return None


async def record_gaps_as_issues(
    session: AsyncSession, product_id: str, report: GapReport
) -> tuple[int, int]:
    """File every gap as an issue. Returns ``(blocking_gaps, total_gaps)``.

    The identity is the criterion itself, so the same unmet requirement found on two
    consecutive builds is one issue with `times_seen = 2` rather than two rows -- and
    that count is the convergence signal a heal counter never had: a criterion still
    unmet after three attempts is a different situation from three different ones.
    """
    blocking = 0
    present: set[str] = set()
    for gap in report.gaps:
        severity = _TIER_SEVERITY.get(gap.tier, "low")
        if severity == "high":
            blocking += 1
        present.add(
            issues.fingerprint_for(
                product_id=product_id,
                kind="bug",
                location=f"requirement: {gap.criterion[:150]}",
                title=f"{gap.tier.upper()} not met: {gap.criterion}",
            )
        )
        await repository.record_issue(
            session,
            product_id=product_id,
            kind="bug",
            severity=severity,
            title=f"{gap.tier.upper()} not met: {gap.criterion}",
            # Stable across rewording and across superseded requirement rows,
            # because the criterion text is the thing that identifies the gap.
            location=f"requirement: {gap.criterion[:150]}",
            expected=gap.criterion,
            actual=gap.evidence,
            found_by="requirements_gap_check",
        )
    # This pass looked at every criterion, so a gap it previously reported and did
    # not report now is met. Without closing it the issue stays open forever,
    # `blocking_now` never falls, and the convergence signal reads "not converging"
    # on a run that is working.
    await repository.close_resolved_issues(
        session,
        product_id=product_id,
        found_by="requirements_gap_check",
        still_present=present,
        verified_by="requirements_gap_check",
    )
    log.info(
        "gaps.recorded",
        product_id=product_id,
        gaps=len(report.gaps),
        blocking=blocking,
        met=len(report.met),
        undetermined=len(report.undetermined),
    )
    return blocking, len(report.gaps)
