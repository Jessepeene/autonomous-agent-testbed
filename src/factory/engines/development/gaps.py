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
    standard_catalogue,
)
from factory.grounding import Grounded, ground
from factory.logging import get_logger
from factory.settings import EngineName, TaskType
from factory.state import repository
from factory.state.schemas import DesignGuidelines, GapReport, WorkRequirements
from factory.supervisor.context import FactoryContext

log = get_logger(__name__)

#: A MUST is what the product had to do, so an unmet one is high severity and will
#: hold a release on its own. A SHOULD is a trade -- worth recording, never worth
#: blocking on, which is the distinction that lets a product be done without being
#: perfect. Extended by `_DEPARTURE` below, which keys on where a claim actually
#: grounds rather than on the tier a reviewer named -- this stays because it is still
#: the correct statement of what the two tiers mean.
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
- Something present that the standard lists as out of scope. That is a departure \
too, in the opposite direction: it was deliberately not wanted and it got built. \
Report it and quote the out-of-scope line -- never as a missing MUST, because it is \
not missing.

Judge only what you were given, and quote the line you are judging. Standing policy \
is not a criterion: it is the rule the criteria are read against, nothing a product \
does can leave a policy line unmet, and it is never something to report.\
"""


async def find_requirement_gaps(
    ctx: FactoryContext,
    *,
    sources: dict[str, str],
    requirements: WorkRequirements,
    task_id: str | None = None,
    #: Resolved the same way the builder resolves it, by
    #: `requirements.py::effective_design`. A page built to one standard and judged
    #: against another fails for reasons nobody can act on.
    design: DesignGuidelines = HOUSE_STYLE,
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
        # No heading of our own. The block states what each of its sections is and
        # what force it carries, and the heading that used to sit here -- "The
        # acceptance criteria this product was built to meet" -- was applied to all
        # of it, the deliberate non-goals and the standing policy included. That is
        # how a policy line came to be graded and filed as an unmet MUST.
        f"{render_for_prompt(requirements)}\n\n"
        # Design is judged here for the first time. Until now "it looks
        # machine-generated" was true, obvious to anyone who opened the page, and
        # invisible to every automated judge -- so a build could meet every
        # criterion and still read as generated, which is exactly what shipped and
        # what only a human caught. Guidelines written to be falsifiable make that
        # a gap like any other.
        f"{render_design_for_prompt(design)}\n\n"
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


#: How a departure is recorded, keyed by *which part of the standard the claim was
#: actually about* rather than by the tier the reviewer named. A reviewer works in
#: prose and can only pick from `must`/`should`; where a statement really lives is a
#: fact about the standard, and the standard is right here to be checked against.
#:
#: The two non-`must` entries are the ones that were previously unrepresentable, and
#: each is a real departure that must not block a release:
#: - `out_of_scope` is something built that nobody asked for. Worth recording as scope
#:   creep; a product that does more than asked is not a product that fails its job.
#: - `policy` and "grounded nowhere" both mean the claim is not a departure from this
#:   standard at all. Kept anyway -- a reviewer noticing something the criteria never
#:   mentioned is the holistic signal a criteria-shaped gate is structurally blind to,
#:   and it is worth more than the noise of one low issue that closes itself.
_DEPARTURE = {
    "must": ("high", "MUST not met"),
    "must_avoid": ("high", "Present, and the standard says it must not be"),
    "should": ("low", "SHOULD not met"),
    "out_of_scope": ("low", "OUT OF SCOPE, but present"),
    "policy": ("low", "Observed, but standing policy says this is correct"),
    None: ("low", "Observed, outside the stated standard"),
}


async def record_gaps_as_issues(
    session: AsyncSession,
    product_id: str,
    report: GapReport,
    *,
    requirements: WorkRequirements | None = None,
    #: The same guidelines `find_requirement_gaps` rendered. Passed rather than
    #: re-resolved so the standard a page was judged against and the catalogue its
    #: report is placed against cannot be two different things.
    design: DesignGuidelines | None = None,
) -> tuple[int, int]:
    """File every gap as an issue. Returns ``(blocking_gaps, total_gaps)``.

    The identity is the criterion itself, so the same unmet requirement found on two
    consecutive builds is one issue with `times_seen = 2` rather than two rows -- and
    that count is the convergence signal a heal counter never had: a criterion still
    unmet after three attempts is a different situation from three different ones.

    **Each claim is placed against the standard before it is filed.** The reviewer
    names the criterion in prose and `tier` only lets it say `must` or `should`, so
    what it names and what it meant can come apart -- and when they do, the result is
    a blocking MUST that can never be satisfied. Both live cases were that: a policy
    line reported as an unmet MUST (a sentence explaining why something is fine, filed
    as a defect) and an out-of-scope entry reported as missing, because a reviewer
    correctly told out-of-scope violations are gaps had no tier to put one in. Two
    repair cycles moved nothing and a human had to break the loop.

    So the tier is derived from where the claim actually grounds rather than taken on
    trust. Nothing is discarded by this -- an ungrounded claim is still filed, just not
    as a failure against a criterion nobody wrote.
    """
    catalogue = standard_catalogue(requirements, design=design) if requirements is not None else {}
    blocking = 0
    present: set[str] = set()
    for gap in report.gaps:
        # Without a standard to check against, the reviewer's own tier is the best
        # available answer -- never a silent downgrade to "ungrounded", which would
        # turn a missing argument into a product with no blocking gaps.
        placed = ground(gap.criterion, catalogue) if catalogue else Grounded(gap.tier, None, 1.0)
        severity, label = _DEPARTURE[placed.group]
        if placed.group != gap.tier:
            log.info(
                "gaps.reclassified",
                product_id=product_id,
                claimed=gap.tier,
                placed=placed.group or "nowhere",
                score=placed.score,
                criterion=gap.criterion[:120],
            )
        if severity == "high":
            blocking += 1
        # The canonical wording when there is one, so the same gap reported twice in
        # two paraphrases is one issue rather than two -- the identity this function
        # depends on survives the reviewer rewording itself between builds.
        subject = placed.entry or gap.criterion
        title = f"{label}: {subject}"
        location = f"requirement: {subject[:150]}"
        present.add(
            issues.fingerprint_for(
                product_id=product_id, kind="bug", location=location, title=title
            )
        )
        await repository.record_issue(
            session,
            product_id=product_id,
            kind="bug",
            severity=severity,
            title=title,
            # Stable across rewording and across superseded requirement rows,
            # because the criterion text is the thing that identifies the gap.
            location=location,
            expected=subject,
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
