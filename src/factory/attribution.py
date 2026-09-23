"""Who gets the credit, and the blame.

An outcome is evidence about every decision that led to it. Establishing which those
were is the whole of this module, and it is done by walking the causal chain rather
than by asking a caller to list them -- a caller that has to remember is a caller that
forgets, which is exactly how `caused_by_task_id` came to be populated in 0 of 232
rows before `factory/causality.py`.

**Evidence, not causation.** Being upstream of a result is not the same as having
produced it. This module credits every ancestor decision, which is generous on
purpose: the alternative is a judgement about which link in a chain "really" caused an
outcome, made at write time, by code, with no way to revisit it. Recording the whole
chain and letting later analysis weigh it keeps that judgement where it belongs and
open to being redone. Any reader of these rows should treat them as "this decision was
in the history of this result", never as proof.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from factory.logging import get_logger
from factory.state.orm import (
    Decision,
    LLMCall,
    Outcome,
    OutcomeEvidence,
    Task,
    TaskEvent,
    new_id,
)

log = get_logger(__name__)

#: How far back up the chain to walk. A guard against a cycle rather than a policy:
#: `caused_by_task_id` should form a tree, and a bug that made it circular would
#: otherwise hang a write path. Deep enough that no real chain reaches it -- the
#: longest observed is idea -> compliance -> decision -> build -> patch -> review.
MAX_CHAIN_DEPTH = 40


async def ancestors(session: AsyncSession, task_id: str) -> list[str]:
    """Every task that led to this one, nearest first, including the task itself.

    Reads `task_events.caused_by_task_id`, which `supervisor/graph.py` records on each
    task's `engine.start` from the payload `causality.attribute` filled in. A task
    enqueued outside any handler has no parent and the walk stops immediately, which is
    correct rather than missing: the scheduler really is where that work came from.
    """
    chain: list[str] = [task_id]
    seen = {task_id}
    current = task_id
    for _ in range(MAX_CHAIN_DEPTH):
        parent = (
            await session.execute(
                select(TaskEvent.caused_by_task_id)
                .where(TaskEvent.task_id == current, TaskEvent.caused_by_task_id.is_not(None))
                .limit(1)
            )
        ).scalar_one_or_none()
        if parent is None or parent in seen:
            break
        chain.append(parent)
        seen.add(parent)
        current = parent
    return chain


async def record_decision(
    session: AsyncSession,
    *,
    kind: str,
    subject_kind: str,
    subject_id: str,
    choice: str,
    rationale: str,
    made_by_engine: str,
    made_by_task_id: str | None = None,
    alternatives: Sequence[str] = (),
    detail: dict[str, object] | None = None,
) -> str:
    """Record a judgement as something later work can point at.

    `alternatives` matters more than it looks: "shipped" says nothing about the quality
    of a decision unless you also know it could have rethought or abandoned. A decision
    with no recorded alternatives cannot be second-guessed, only agreed with.
    """
    row = Decision(
        id=new_id(),
        kind=kind,
        subject_kind=subject_kind,
        subject_id=subject_id,
        choice=choice,
        rationale=rationale,
        made_by_engine=made_by_engine,
        made_by_task_id=made_by_task_id,
        alternatives=list(alternatives),
        detail=detail or {},
    )
    session.add(row)
    await session.flush()
    log.info("decision.recorded", kind=kind, choice=choice, subject_id=subject_id, id=row.id)
    return row.id


async def record_outcome(
    session: AsyncSession,
    *,
    kind: str,
    succeeded: bool | None = None,
    value_cents: int | None = None,
    subject_kind: str | None = None,
    subject_id: str | None = None,
    detail: dict[str, object] | None = None,
    task_id: str | None = None,
    parameters: Sequence[str] = (),
    credit_decisions: bool = True,
) -> str:
    """Record something that happened, and link it to what it is evidence about.

    `parameters` are named explicitly because a prompt version is not in the causal
    chain -- it was *served* to a call, not enqueued by one. Decisions are derived from
    the chain instead, so nobody has to remember them; `credit_decisions=False` is for
    outcomes that genuinely have no upstream judgement worth crediting.
    """
    outcome = Outcome(
        id=new_id(),
        kind=kind,
        succeeded=succeeded,
        value_cents=value_cents,
        subject_kind=subject_kind,
        subject_id=subject_id,
        detail=detail or {},
        task_id=task_id,
    )
    session.add(outcome)
    await session.flush()

    subjects: list[tuple[str, str]] = [("parameter", p) for p in parameters if p]
    if credit_decisions and task_id:
        chain = await ancestors(session, task_id)
        decided = (
            (await session.execute(select(Decision.id).where(Decision.made_by_task_id.in_(chain))))
            .scalars()
            .all()
        )
        subjects += [("decision", d) for d in decided]

    for subject_kind_, subject_id_ in subjects:
        session.add(
            OutcomeEvidence(
                id=new_id(),
                outcome_id=outcome.id,
                subject_kind=subject_kind_,
                subject_id=subject_id_,
            )
        )
    await session.flush()
    log.info(
        "outcome.recorded",
        kind=kind,
        succeeded=succeeded,
        value_cents=value_cents,
        subjects=len(subjects),
        id=outcome.id,
    )
    return outcome.id


async def outcome_counts(
    session: AsyncSession,
    *,
    subject_kind: str,
    subject_id: str,
    limit: int | None = None,
) -> tuple[int, int]:
    """(successes, total) for one subject, most recent first when `limit` caps it.

    The replacement for `store.episode_counts`, and the reason the migration copies
    every episode across with its id and `created_at` intact: for `subject_kind
    ='parameter'` this must return exactly what the old query returned, or every trial
    currently open silently changes its own arithmetic mid-flight.
    """
    stmt = (
        select(Outcome.succeeded)
        .join(OutcomeEvidence, OutcomeEvidence.outcome_id == Outcome.id)
        .where(
            OutcomeEvidence.subject_kind == subject_kind,
            OutcomeEvidence.subject_id == subject_id,
            Outcome.succeeded.is_not(None),
        )
        .order_by(Outcome.created_at.desc())
    )
    if limit is not None:
        stmt = stmt.limit(limit)
    rows = list((await session.execute(stmt)).scalars().all())
    return sum(1 for r in rows if r), len(rows)


#: How wide a decision's consequences may be walked. A guard on cost, not a policy:
#: the subtree of a decision that commissioned a build is dozens of tasks, and a bug
#: that made the chain circular must not turn a read into a table scan.
MAX_SUBTREE = 500


@dataclass(frozen=True, slots=True)
class DecisionReturn:
    """What a decision cost this company, against what came back from it.

    **The first figure here that survives a change of method.** Every metric the
    learning loop optimises today is a SaaS funnel proxy -- idea survival, page
    interest, outreach conversion -- and each stops meaning anything the moment the
    company does something that is not a landing page. Cost against value does not:
    a freelance engagement, a productised service and a subscription all reduce to it,
    which is what lets a general manager hold them side by side and ask one question.

    Three limits, stated here rather than left to be discovered by somebody reading a
    number as more than it is:

    - **Cost is a lower bound.** `llm_calls` is the only spend attributed to a task.
      Hosting, mail, domains and a human's time are real costs of a decision and appear
      nowhere, so a decision that looks cheap may only be cheap in the one currency this
      company happens to meter.
    - **The subtree over-claims.** Everything downstream of a decision is counted
      against it, and being upstream of a cost is not the same as having caused it. A
      decision to build is charged for a repair loop it did not choose.
    - **Value arrives late and cost arrives immediately.** A decision read too early
      looks worse than it is, which is the single most likely way this number gets
      misused.
    """

    decision_id: str
    cost_cents: int
    value_cents: int
    #: How many tasks the cost was drawn from, and how many outcomes the value was.
    #: Present so a reader can tell "this decision returned nothing" from "nothing has
    #: happened yet" -- the distinction the whole instrumentation principle turns on.
    tasks: int
    outcomes: int

    @property
    def net_cents(self) -> int:
        return self.value_cents - self.cost_cents

    @property
    def measured(self) -> bool:
        """Whether anything bearing on this decision has actually been observed.

        Not `tasks`: a decision's subtree always contains at least the task it was made
        in, so counting that made every decision look measured the moment it existed --
        which is exactly the "nothing yet" and "returned nothing" collapse this property
        was added to prevent. Spend and outcomes are observations; existing is not.
        """
        return bool(self.cost_cents or self.outcomes)


async def descendants(session: AsyncSession, task_id: str) -> list[str]:
    """Every task this one led to, including itself.

    The mirror of `ancestors`, and the reason `caused_by_task_id` had to be populated
    before any of this was possible: a decision's consequences are exactly the subtree
    of the task it was made in.
    """
    frontier = [task_id]
    seen = {task_id}
    while frontier:
        rows = (
            (
                await session.execute(
                    select(TaskEvent.task_id).where(
                        TaskEvent.caused_by_task_id.in_(frontier),
                        TaskEvent.task_id.is_not(None),
                    )
                )
            )
            .scalars()
            .all()
        )
        frontier = [r for r in set(rows) if r is not None and r not in seen]
        for r in frontier:
            seen.add(r)
        if len(seen) >= MAX_SUBTREE:
            log.warning("attribution.subtree_truncated", task_id=task_id, size=len(seen))
            break
    return list(seen)


async def decision_return(session: AsyncSession, decision_id: str) -> DecisionReturn:
    """Cost against value for one decision.

    Cost is summed over the decision's whole task subtree; value over every outcome
    that names it as evidence. Both sides already existed on disk and nothing joined
    them -- `llm_calls.cost_usd` has carried the price of every call since long before
    this, and `outcomes.value_cents` since migration 0044.
    """
    decision = await session.get(Decision, decision_id)
    if decision is None or not decision.made_by_task_id:
        return DecisionReturn(decision_id, 0, 0, 0, 0)

    subtree = await descendants(session, decision.made_by_task_id)
    # Rounded to cents at the edge rather than carried as a float: money is compared,
    # summed and shown, and a float that is nearly an integer is a bug waiting for a
    # report to disagree with a total.
    spent = (
        await session.execute(
            select(func.coalesce(func.sum(LLMCall.cost_usd), 0.0)).where(
                LLMCall.task_id.in_(subtree)
            )
        )
    ).scalar_one()
    earned, counted = (
        await session.execute(
            select(
                func.coalesce(func.sum(Outcome.value_cents), 0),
                func.count(Outcome.id),
            )
            .join(OutcomeEvidence, OutcomeEvidence.outcome_id == Outcome.id)
            .where(
                OutcomeEvidence.subject_kind == "decision",
                OutcomeEvidence.subject_id == decision_id,
            )
        )
    ).one()

    return DecisionReturn(
        decision_id=decision_id,
        cost_cents=round(float(spent) * 100),
        value_cents=int(earned or 0),
        tasks=len(subtree),
        outcomes=int(counted or 0),
    )


async def product_spend_to_date(session: AsyncSession, product_id: str) -> int:
    """Total LLM cost, in cents, of every task ever run against this product.

    Not a subtree walk from one decision -- a release judgement needs "what has this
    product cost so far, in total", and finding "the one decision that commissioned
    it" to walk from would be a guess about which `Decision` row that was.
    `Task.product_id` already answers the question directly and exactly, across
    every repair cycle and every rebuild this product has ever had.
    """
    spent = (
        await session.execute(
            select(func.coalesce(func.sum(LLMCall.cost_usd), 0.0))
            .select_from(LLMCall)
            .join(Task, Task.id == LLMCall.task_id)
            .where(Task.product_id == product_id)
        )
    ).scalar_one()
    return round(float(spent) * 100)
