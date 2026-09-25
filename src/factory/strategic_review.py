"""Phase 5b: the question nothing in this system has ever asked before.

Every review anywhere else in this company is about one product (`decide_release`)
or one department (a manager's own tick). Nothing ever reads the company's own
state as a whole and asks whether the objective -- the one row in `objectives`,
Phase 5a -- is actually being served.

**Triggered on a condition, not a clock.** The same preference this project has
held throughout: a decision worth taking again is keyed on the world changing, not
on time passing (Phase 2.8's `unmet_signature`). Concretely, a run of consecutive
`rethink`/`abandon` release decisions, company-wide -- not one product stuck
repeatedly, several different products all failing to ship in a row, which is a
fact about the company rather than about any one of them.

**A three-way split, not a verdict**, and `undetermined` is a real fourth answer --
see `StrategicReviewVerdict`'s own docstring for why each is a different owner.

**No autonomous owner exists yet.** Phase 3 (Product/Management), Phase 4 (the rest
of the organisation) and Phase 5c (method choice) are the eventual owners of the
three findings, and none of them exist. So every review here escalates to a human,
the same way `ProductNext`'s `rethink` did before Phase 3 existed to receive it --
the operator is an escalation target, not a subscriber, and there is nothing else
to be a target of this yet.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from factory import attribution
from factory.hitl import interrupts
from factory.logging import get_logger
from factory.settings import EngineName, TaskType
from factory.state import repository
from factory.state.orm import Customer, Decision, Memory, Outcome, Subscription, Task
from factory.state.schemas import InterruptReason, StrategicReviewVerdict
from factory.supervisor import audit
from factory.supervisor.context import FactoryContext

log = get_logger(__name__)

#: How many consecutive release decisions must be `rethink`/`abandon` -- none of
#: the more recent ones a `ship` -- before this company asks about itself rather
#: than about one more product. Scaffolding, the same way `MAX_HEAL_CYCLES` was: a
#: number chosen at a desk because there is no data yet to discover it from, and
#: the first thing worth revisiting once a real review has actually run a few
#: times.
TRIGGER_STREAK = 3

#: How many of the most recent release decisions to look at when checking the
#: streak. Bounded so the query never table-scans; must be >= TRIGGER_STREAK.
_STREAK_LOOKBACK = 10


@dataclass(frozen=True, slots=True)
class ScorecardClause:
    """One clause of the founding paragraph, checked against real rows.

    Never asserted from memory of what the paragraph says. Same discipline
    `product_brief` already holds for one product's own gaps: a claim that cannot
    be traced back to a row does not belong here. `status` is one of `met`,
    `partial`, `not_met`, `undetermined` -- the fourth exists for the same reason
    it exists on the verdict itself: a clause this company has not yet generated
    enough real evidence to score either way is not the same as a clause that
    failed.
    """

    clause: str
    status: str
    evidence: str


async def founding_paragraph_scorecard(session: AsyncSession) -> list[ScorecardClause]:
    """The founding paragraph, checked clause by clause against what actually
    exists on disk -- not a restatement of the artifact ledger, a fresh query."""
    clauses: list[ScorecardClause] = []

    objective = await repository.live_objective(session)
    clauses.append(
        ScorecardClause(
            "The objective is immutable.",
            "met" if objective is not None else "not_met",
            (
                f"One live row in `objectives`, set by {objective.set_by} "
                f"({objective.created_at.date().isoformat()})."
                if objective is not None
                else "No row in `objectives` has ever been set."
            ),
        )
    )

    review_count = int(
        (
            await session.execute(
                select(func.count())
                .select_from(Decision)
                .where(Decision.kind == "strategic_review")
            )
        ).scalar_one()
    )
    clauses.append(
        ScorecardClause(
            "A recurring strategic review cycle runs.",
            "met" if review_count >= 1 else "not_met",
            (
                f"{review_count} prior review(s) recorded; this is review "
                f"#{review_count + 1}."
                if review_count
                else "None recorded before this one -- this is review #1."
            ),
        )
    )

    decision_count = int(
        (await session.execute(select(func.count()).select_from(Decision))).scalar_one()
    )
    outcome_count = int(
        (await session.execute(select(func.count()).select_from(Outcome))).scalar_one()
    )
    memory_count = int(
        (await session.execute(select(func.count()).select_from(Memory))).scalar_one()
    )
    clauses.append(
        ScorecardClause(
            "An external persistent memory stores mission, strategies, experiments, "
            "decisions, financial results, and lessons learned.",
            "met" if decision_count and memory_count else "partial",
            f"{decision_count} decision(s), {outcome_count} outcome(s), "
            f"{memory_count} memory row(s) on disk.",
        )
    )

    clauses.append(
        ScorecardClause(
            "A hierarchical decision structure exists.",
            "partial",
            "Decisions have identity and can be attributed to what they cost and "
            "returned (Phase 2.14), but every one today is made by a department "
            "engine directly -- no management layer above them exists yet (Phase 3).",
        )
    )

    customer_count = int(
        (await session.execute(select(func.count()).select_from(Customer))).scalar_one()
    )
    subscription_count = int(
        (await session.execute(select(func.count()).select_from(Subscription))).scalar_one()
    )
    clauses.append(
        ScorecardClause(
            "The organisation learns from real-world feedback.",
            "undetermined" if customer_count == 0 else "partial",
            f"{customer_count} customer(s), {subscription_count} subscription(s). "
            + (
                "With zero real customers, this cannot yet be told apart from "
                "'nothing has been tried on the market long enough to know.'"
                if customer_count == 0
                else "Real market outcomes exist; whether the company visibly acts "
                "on them is still a judgement, not a count."
            ),
        )
    )

    clauses.append(
        ScorecardClause(
            "A General Manager chooses between revenue streams, not a "
            "predetermined path.",
            "not_met",
            "No method registry exists (Phase 5c); every product made so far has "
            "been a SaaS validation, by construction rather than by choice.",
        )
    )

    return clauses


@dataclass(frozen=True, slots=True)
class CompanyDossier:
    """What a strategic review reads. Not one product's issues -- the aggregate a
    human founder would actually look at before deciding whether to change course."""

    since: datetime
    streak: list[Decision]
    decision_returns: list[attribution.DecisionReturn]
    concerns: list[str]
    scorecard: list[ScorecardClause]


async def rethink_or_abandon_streak(
    session: AsyncSession, *, limit: int = _STREAK_LOOKBACK
) -> list[Decision]:
    """The most recent release decisions, newest first, that are all `rethink` or
    `abandon` -- stops at the first `ship`/`fix_first`. Literal "consecutive",
    company-wide rather than per-product: several different products all failing
    to ship in a row is the fact worth a review, not one product retried often."""
    stmt = (
        select(Decision)
        .where(Decision.kind == "release")
        .order_by(Decision.created_at.desc())
        .limit(limit)
    )
    recent = list((await session.execute(stmt)).scalars().all())
    streak: list[Decision] = []
    for decision in recent:
        if decision.choice not in ("rethink", "abandon"):
            break
        streak.append(decision)
    return streak


async def should_trigger(session: AsyncSession) -> list[Decision] | None:
    """`None` if nothing yet warrants a review; otherwise the streak that does,
    newest decision first -- its id is what the caller keys the enqueue on."""
    streak = await rethink_or_abandon_streak(session)
    if len(streak) >= TRIGGER_STREAK:
        return streak
    return None


async def assemble_company_dossier(
    session: AsyncSession, streak: list[Decision]
) -> CompanyDossier:
    since = streak[-1].created_at if streak else datetime.now(UTC)
    decision_returns = [await attribution.decision_return(session, d.id) for d in streak]
    # Safe to call unconditionally: `check_business_health` dedups its own escalation
    # on today's date, so a second call this same tick from elsewhere in the system
    # collapses rather than doubling up.
    concerns = await audit.check_business_health(session)
    scorecard = await founding_paragraph_scorecard(session)
    return CompanyDossier(
        since=since,
        streak=streak,
        decision_returns=decision_returns,
        concerns=concerns,
        scorecard=scorecard,
    )


def render_dossier_for_prompt(dossier: CompanyDossier) -> str:
    lines = [
        f"# The triggering streak: {len(dossier.streak)} consecutive "
        "rethink/abandon release decisions, company-wide",
        "",
    ]
    if dossier.streak:
        for decision, ret in zip(dossier.streak, dossier.decision_returns, strict=True):
            measured = "measured" if ret.measured else "not yet measured"
            lines.append(
                f"- {decision.created_at.date().isoformat()} product "
                f"{decision.subject_id[:8]}: **{decision.choice}** -- "
                f"{decision.rationale[:200]} "
                f"(${ret.cost_cents / 100:.2f} spent, ${ret.value_cents / 100:.2f} "
                f"returned, {measured})"
            )
    else:
        lines.append("- No active streak at the moment this review actually ran.")
    lines += [
        "",
        "# Business-health concerns (deterministic checks, not judgement)",
    ]
    lines += [f"- {c}" for c in dossier.concerns] if dossier.concerns else ["- None flagged."]
    lines += [
        "",
        "# The founding paragraph, checked against real rows",
    ]
    lines += [f"- [{c.status}] {c.clause} -- {c.evidence}" for c in dossier.scorecard]
    return "\n".join(lines)


SYSTEM = """\
You are asked the one question nothing else in this company asks: taken as a \
whole, is it still serving its objective, and if not, why not.

You are not judging one product. You are looking at a streak of products that all \
failed to ship, this company's standing concerns, and an honest scorecard of what \
the company was actually founded to be. Your answer is one of four, and each is a \
different kind of finding with a different eventual owner:

- **execution_problem** -- something is broken. A bug, a missing check, a process \
that does not do what it is supposed to. The fix is inside how the company already \
operates.
- **failed_strategy** -- the company tried something real and the market said no. \
Not a bug; a hypothesis that turned out wrong. The fix is choosing differently \
within the same method.
- **wrong_assumption** -- something about the *method itself* -- building and \
validating SaaS products -- is the wrong way to pursue the objective, not just this \
attempt at it. This is the rare, serious one: say it only when the evidence points \
at the method, not at one more execution failure dressed up as a bigger claim.
- **undetermined** -- the honest answer when there genuinely is not enough evidence \
yet to tell these apart. With very few decisions and no real customers, this is \
often correct, and saying so is not a failure to conclude -- manufacturing a \
confident finding from thin data is the actual failure.

Weigh the scorecard's [undetermined] and [partial] clauses as what they are: this \
company has very little real-world evidence yet. Do not let a small streak of \
failed products read as proof about the method when the company has barely tried \
the method at all. Prefer undetermined over a dramatic conclusion the data cannot \
actually support.\
"""


async def run_strategic_review(ctx: FactoryContext, session: AsyncSession, task: Task) -> str:
    """The task handler. Re-derives the streak at run time rather than trusting
    whatever triggered the enqueue -- the state a review judges should be the state
    at the moment it actually runs, not a stale snapshot from whenever it was
    queued."""
    streak = await rethink_or_abandon_streak(session)
    dossier = await assemble_company_dossier(session, streak)

    try:
        verdict = await ctx.llm.parse(
            StrategicReviewVerdict,
            task_type=TaskType.ANALYSIS,
            system=SYSTEM,
            prompt=render_dossier_for_prompt(dossier),
            engine=EngineName.SUPERVISOR,
            task_id=task.id,
            max_tokens=8_000,
        )
    except Exception as exc:
        # A reviewer that could not think has not concluded "fine". Failing the task
        # leaves nothing recorded and the next trigger asks again -- the honest
        # outcome, the same reasoning `decide_next` already holds for its own
        # equivalent failure.
        log.warning("strategic_review.failed", detail=str(exc)[:200])
        return f"supervisor: strategic review could not be judged -- {str(exc)[:150]}"

    objective = await repository.live_objective(session)
    subject_id = objective.id if objective is not None else "no-objective-set"

    await attribution.record_decision(
        session,
        kind="strategic_review",
        subject_kind="company",
        subject_id=subject_id,
        choice=verdict.finding,
        rationale=verdict.rationale,
        made_by_engine=EngineName.SUPERVISOR.value,
        made_by_task_id=task.id,
        alternatives=["execution_problem", "failed_strategy", "wrong_assumption", "undetermined"],
        detail={"assessment": verdict.assessment[:2000], "streak_length": len(dossier.streak)},
    )

    await interrupts.request(
        session,
        reason=InterruptReason.STRATEGIC_REVIEW,
        subject=f"Strategic review: {verdict.finding} ({datetime.now(UTC).date().isoformat()})",
        detail=(
            f"{len(dossier.streak)} consecutive rethink/abandon release decisions "
            f"triggered this review.\n\n**{verdict.finding}** -- {verdict.rationale}"
            f"\n\n{verdict.assessment}"
        ),
        headline=f"\U0001f9ed Strategic review: {verdict.finding}",
        reply_hint=(
            "No autonomous owner exists yet for this finding (Phase 3/4/5c) -- read "
            "and decide by hand. No reply needed to clear it."
        ),
        needs_reply=False,
    )

    log.info("strategic_review.concluded", finding=verdict.finding, streak=len(dossier.streak))
    return f"supervisor: strategic review concluded {verdict.finding}"
