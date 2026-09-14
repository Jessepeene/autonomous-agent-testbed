"""Deciding whether to release, instead of evaluating whether to.

This replaces `if not deploy_blocking: ship`. That line was the highest-judgement
moment in the whole company -- is this good enough to put in front of a customer --
expressed as a boolean, and it is why the build loop was a retry loop rather than a
process. Every transition was automatic: Council blocks, heal; heal fails,
regenerate; N cycles, escalate. Nobody ever decided anything, so the system had
exactly one move and made it until a counter ran out.

A human team at the same moment has options. Ship it. Ship it with known flaws.
Fix these two and leave the rest. Rethink the approach. Kill it. The reviewer's
finding is *input to a decision*, not a verdict that mechanically triggers the next
lap.

What makes the decision possible rather than a vibe is `requirements`: `must` is
what the product has to do, `should` is what a release weighs against the cost of
waiting, and `out_of_scope` is what nobody should be counting against it. Before
those existed this call would have had nothing to judge against -- "good enough"
with no answer to "good enough for what".

**The rails do not move.** A `deploy_blocking` finding (critical or high anywhere,
medium in security/secrets/privilege) cannot be shipped past, whatever this decides
-- see `CouncilResult.deploy_blocking`. The judgement operates inside the space the
rails allow, which is the whole of it: choosing *which* issues matter, whether the
approach is wrong, and whether the thing is worth continuing at all.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from factory.engines.development.requirements import render_for_prompt
from factory.logging import get_logger
from factory.settings import EngineName, TaskType
from factory.state import repository
from factory.state.schemas import CouncilResult, ReleaseDecision, WorkRequirements
from factory.supervisor.context import FactoryContext

log = get_logger(__name__)

RELEASE_SYSTEM = """\
You decide whether a product is released. You are not a checklist -- the findings \
below are evidence for a judgement, not a verdict you are restating.

The question is whether this is good enough to put in front of a real person today, \
given what it is for. Weigh what is actually wrong against the cost of not \
shipping: a validation landing page exists to find out whether anyone wants the \
idea, and a page that never goes live answers nothing.

Your options:

- **ship**: it meets what it must do. Known issues remain and you accept them \
deliberately -- list them, so accepting is on the record and not an oversight.
- **fix_first**: it cannot go out as it is, and you can name specifically what has \
to change. Name the fewest things that would make it shippable, not everything \
anyone found. This is the one that gets acted on immediately, so a long list here \
is how a product never ships.
- **rethink**: the problem is not a defect list, it is the approach. The build does \
not address what it was for and patching it will not close the gap.
- **abandon**: this is not worth more effort.

Rules for the judgement itself:

- A MUST that is not met means it is not shippable. Say which.
- A SHOULD that is not met is a trade, never a blocker. Weigh it and move on.
- Anything listed as out of scope is absent on purpose. Never count it against the \
product.
- A finding raised on many reviews without ever being fixed is evidence about the \
repair loop, not fresh evidence about the product.
- If you are told the blocking set is not shrinking, that is the signal to stop \
patching. Two more attempts at something that has not moved in three is worse than \
saying so: choose rethink and say what the approach is missing. Nothing counts \
attempts on your behalf any more, so noticing a wall is your job.
- rethink and abandon are real options and under-used ones. Say so when the honest \
answer is that more patching will not help.

Your rationale is read by a human who has not seen any of this. Say what you \
decided and why, in plain terms, referring to what the product is actually for.\
"""


async def decide_release(
    ctx: FactoryContext,
    session: AsyncSession,
    *,
    product_id: str,
    product_name: str,
    council: CouncilResult,
    requirements: WorkRequirements | None,
    heal_attempts: int,
    task_id: str | None = None,
) -> ReleaseDecision | None:
    """Judge whether this build is released. ``None`` if the judgement could not run.

    A ``None`` return leaves the caller on its previous deterministic behaviour
    rather than blocking a deploy on a model call -- the same degradation stance
    `requirements` takes. Failing to reach a judgement must not become an outage.
    """
    open_issues = await repository.open_issues_for(session, product_id)
    progress = await repository.product_progress(session, product_id)
    blocking = council.deploy_blocking

    facts = [f"Product: {product_name}", ""]
    if requirements is not None:
        facts += [render_for_prompt(requirements), ""]
    else:
        facts += [
            "No acceptance criteria were written for this product, so there is no "
            "stated standard to judge it against. Say so in your rationale.",
            "",
        ]

    facts.append(
        f"The code reviewers raised {len(council.blocking)} finding(s), of which "
        f"{len(blocking)} are severe enough that the rails will not allow a release "
        "regardless of what you decide."
    )
    if blocking:
        facts += [f"  BLOCKED BY: [{f.severity}/{f.category}] {f.summary}" for f in blocking[:10]]

    if open_issues:
        facts += ["", "Everything currently open against this product:"]
        facts += [
            f"  [{i.severity}] {i.title} (seen {i.times_seen}x since "
            f"{i.first_seen_at:%Y-%m-%d}, status {i.status})"
            for i in open_issues[:20]
        ]

    # The thread across attempts, assembled from rows rather than remembered. This
    # is what replaces a heal counter: "we have tried three times" cannot tell
    # progress from a wall, and "the blocking set went 6 -> 4 -> 4 and these two
    # criteria have failed on every pass" can.
    facts += ["", f"Repair attempts on this product so far: {heal_attempts}."]
    if progress.blocking_at_last_attempt is not None:
        direction = {
            True: "fewer than last time -- this is converging",
            False: "no better than last time -- this is not converging",
        }[bool(progress.converging)]
        facts.append(
            f"Blocking issues: {progress.blocking_now} now, "
            f"{progress.blocking_at_last_attempt} at the last attempt. {direction}."
        )
    if progress.stuck:
        facts += [
            "",
            "Failed on three or more separate passes without ever being resolved. "
            "This is evidence about the approach, not fresh evidence about the build:",
        ]
        facts += [f"  - {title}" for title in progress.stuck]
    if progress.last_decision:
        facts.append(
            f"\nThe last release decision on this product was: {progress.last_decision}."
        )
    facts.append(
        "The test suite passes and the deterministic contract checks pass; those are "
        "not in question."
    )

    try:
        decision = await ctx.llm.parse(
            ReleaseDecision,
            task_type=TaskType.ANALYSIS,
            system=RELEASE_SYSTEM,
            prompt="\n".join(facts),
            engine=EngineName.DEVELOPMENT,
            task_id=task_id,
        )
    except Exception as exc:
        log.warning("release.undecided", product_id=product_id, detail=str(exc)[:200])
        return None

    # The rail, applied after the judgement rather than instead of it. The decision
    # is still recorded as made -- what it chose and why is exactly the evidence
    # worth keeping when a release is overruled -- but a severe finding is not
    # something an employee may sign off.
    if decision.decision == "ship" and blocking:
        log.warning(
            "release.ship_overruled",
            product_id=product_id,
            blocking=len(blocking),
            rationale=decision.rationale[:200],
        )
        decision = decision.model_copy(
            update={
                "decision": "fix_first",
                "rationale": (
                    "Overruled by the release rails: judged shippable, but "
                    f"{len(blocking)} finding(s) are at a severity that cannot ship. "
                    f"Original reasoning: {decision.rationale}"
                ),
                "fix_first": [f.summary for f in blocking[:10]],
            }
        )

    await repository.record_task_event(
        session,
        event="release.decided",
        task_id=task_id,
        product_id=product_id,
        engine=EngineName.DEVELOPMENT,
        detail={
            "decision": decision.decision,
            "rationale": decision.rationale[:1000],
            "unmet_must": decision.unmet_must[:10],
            "accepted": decision.accepted_issues[:10],
            "fix_first": decision.fix_first[:10],
            "heal_attempts": heal_attempts,
            # Recorded so the *next* decision can compare and see a direction.
            # Convergence is not knowable from one snapshot, and this is how the
            # thread survives without anything having to remember it.
            "open_blocking": progress.blocking_now,
        },
    )
    log.info(
        "release.decided",
        product_id=product_id,
        decision=decision.decision,
        unmet_must=len(decision.unmet_must),
        accepted=len(decision.accepted_issues),
    )
    return decision
