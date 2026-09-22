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

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from factory.logging import get_logger
from factory.state.orm import Decision, Outcome, OutcomeEvidence, TaskEvent, new_id

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
