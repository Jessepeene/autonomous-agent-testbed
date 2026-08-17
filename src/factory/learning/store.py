"""The sole writer of ``tunable_parameters`` and ``learning_episodes``.

Mirrors ``factory.memory.store`` being the sole writer of ``Memory`` rows. Every
function here that can create or change a ``tunable_parameters`` row calls
``allowlist.assert_mutable(key)`` first — including ``bootstrap_active``, even though
in practice it is only ever reached via ``learning.prompts.get_prompt``/
``get_scoring_weights`` with the caller's own literal key string. Defense in depth
costs nothing here and a future refactor that adds a second bootstrap path inherits
the guard for free.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from factory.learning.allowlist import assert_mutable
from factory.logging import get_logger
from factory.state.orm import LearningEpisode, TunableParameter

log = get_logger(__name__)


async def get_active(session: AsyncSession, key: str) -> TunableParameter | None:
    stmt = select(TunableParameter).where(
        TunableParameter.key == key, TunableParameter.status == "active"
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def get_serving(session: AsyncSession, key: str) -> TunableParameter | None:
    """The version that should actually serve the *next* call for this key: the
    trial version if one is running for it, otherwise the active version.

    This is not the same query as ``get_active`` — a trial that never actually serves
    a real call could never accumulate the outcome data its own evaluation depends on.
    At most one row across the *entire* allowlist can ever be ``trial`` at a time (the
    one-trial-at-a-time serialization rule enforced in ``promote_eligible_candidates``),
    so "the trial row for this key, if any" is always well-defined.
    """
    stmt = select(TunableParameter).where(
        TunableParameter.key == key, TunableParameter.status == "trial"
    )
    trial = (await session.execute(stmt)).scalar_one_or_none()
    if trial is not None:
        return trial
    return await get_active(session, key)


async def bootstrap_active(
    session: AsyncSession,
    *,
    kind: str,
    key: str,
    value: dict[str, Any],
    created_by: str,
    target_metric: str,
) -> str:
    """Seed version 1 as ``active`` the first time a key is ever read, from the code
    constant that shipped before this system existed. Concurrency-safe: two
    simultaneous first-reads both attempt the insert, only one wins, both re-select
    to get the winning row's id — the same ``ON CONFLICT DO NOTHING`` idiom already
    used by ``repository.insert_lead``/``upsert_idea``."""
    assert_mutable(key)
    stmt = (
        pg_insert(TunableParameter)
        .values(
            kind=kind,
            key=key,
            value=value,
            version=1,
            status="active",
            target_metric=target_metric,
            rationale="bootstrapped from the code constant at first read",
            created_by=created_by,
            promoted_at=datetime.now(UTC),
        )
        .on_conflict_do_nothing(index_elements=[TunableParameter.key, TunableParameter.version])
        .returning(TunableParameter.id)
    )
    row = (await session.execute(stmt)).scalar_one_or_none()
    if row is not None:
        await session.flush()
        log.info("learning.bootstrapped", key=key)
        return str(row)

    # Lost the race — the other writer's row is now the active one.
    existing = await get_active(session, key)
    assert existing is not None, f"bootstrap conflict for {key!r} but no active row exists"
    return existing.id


async def _next_version(session: AsyncSession, key: str) -> int:
    stmt = select(func.coalesce(func.max(TunableParameter.version), 0)).where(
        TunableParameter.key == key
    )
    return int((await session.execute(stmt)).scalar_one()) + 1


async def insert_candidate(
    session: AsyncSession,
    *,
    key: str,
    kind: str,
    value: dict[str, Any],
    rationale: str,
    created_by: str,
    parent_id: str | None,
    target_metric: str,
) -> str:
    """Write a proposal. Never promotes it — status is always ``candidate``, full
    stop. Promotion to ``trial`` is a separate, deterministic decision
    (``promote_eligible_candidates``), never something the proposing agent controls."""
    assert_mutable(key)
    version = await _next_version(session, key)
    row = TunableParameter(
        kind=kind,
        key=key,
        value=value,
        version=version,
        status="candidate",
        target_metric=target_metric,
        rationale=rationale,
        created_by=created_by,
        parent_id=parent_id,
    )
    session.add(row)
    await session.flush()
    log.info("learning.candidate_proposed", key=key, version=version, tunable_parameter_id=row.id)
    return row.id


async def has_open_work(session: AsyncSession, key: str) -> bool:
    """A candidate awaiting promotion, or a trial still running, for this key."""
    stmt = select(func.count()).where(
        TunableParameter.key == key,
        TunableParameter.status.in_(("candidate", "trial")),
    )
    return int((await session.execute(stmt)).scalar_one()) > 0


async def any_trial_in_flight(session: AsyncSession) -> bool:
    """The one-trial-at-a-time serialization rule, checked across the *entire*
    allowlist, not just one key. GENERATOR_SYSTEM and DISCRIMINATOR_SYSTEM both move
    the same outcome metric (idea survival rate) — concurrent trials on both would
    make neither result attributable."""
    stmt = select(func.count()).where(TunableParameter.status == "trial")
    return int((await session.execute(stmt)).scalar_one()) > 0


async def recently_resolved(session: AsyncSession, key: str, *, cooldown_hours: int) -> bool:
    """A candidate proposed for this key more recently than the cooldown — regardless
    of whether it was promoted, confirmed, or rolled back — so a fresh proposal isn't
    made on top of one still within its human-visible-audit-trail grace period."""
    since = datetime.now(UTC) - timedelta(hours=cooldown_hours)
    stmt = select(func.count()).where(
        TunableParameter.key == key,
        TunableParameter.created_at >= since,
    )
    return int((await session.execute(stmt)).scalar_one()) > 0


async def promote_eligible_candidates(
    session: AsyncSession, *, cooldown_hours: int, trial_days: int
) -> list[str]:
    """Deterministic, no LLM call. A candidate is promoted to ``trial`` once it has
    existed for at least ``cooldown_hours`` (so the audit trail has a chance to be
    noticed) and no other key is currently on trial (the serialization rule)."""
    if await any_trial_in_flight(session):
        return []

    since = datetime.now(UTC) - timedelta(hours=cooldown_hours)
    stmt = (
        select(TunableParameter)
        .where(TunableParameter.status == "candidate", TunableParameter.created_at <= since)
        .order_by(TunableParameter.created_at.asc())
        .limit(1)
    )
    candidate = (await session.execute(stmt)).scalar_one_or_none()
    if candidate is None:
        return []

    assert_mutable(candidate.key)
    now = datetime.now(UTC)
    await session.execute(
        update(TunableParameter)
        .where(TunableParameter.id == candidate.id)
        .values(status="trial", trial_started_at=now, trial_until=now + timedelta(days=trial_days))
    )
    log.info("learning.trial_started", key=candidate.key, tunable_parameter_id=candidate.id)
    return [candidate.id]


async def list_trials(session: AsyncSession) -> list[TunableParameter]:
    stmt = select(TunableParameter).where(TunableParameter.status == "trial")
    return list((await session.execute(stmt)).scalars().all())


async def list_all(session: AsyncSession, *, limit: int = 50) -> list[TunableParameter]:
    """Every tunable parameter regardless of status (candidate/trial/active/
    retired), newest first -- the dashboard's Learning Loop view. Unlike
    `list_trials` above, which only ever returns the single row currently on
    trial (only one trial is ever in flight globally, by design)."""
    stmt = select(TunableParameter).order_by(TunableParameter.created_at.desc()).limit(limit)
    return list((await session.execute(stmt)).scalars().all())


async def episode_counts(
    session: AsyncSession, tunable_parameter_id: str, *, limit: int | None = None
) -> tuple[int, int]:
    """(successes, total) for a version's episodes, most recent first when ``limit``
    caps a baseline lookup to a rolling window rather than all-time history."""
    stmt = select(LearningEpisode.outcome).where(
        LearningEpisode.tunable_parameter_id == tunable_parameter_id
    )
    stmt = stmt.order_by(LearningEpisode.created_at.desc())
    if limit is not None:
        stmt = stmt.limit(limit)
    outcomes = list((await session.execute(stmt)).scalars().all())
    successes = sum(1 for o in outcomes if o == "success")
    return successes, len(outcomes)


async def confirm_trial(
    session: AsyncSession, tunable_parameter_id: str, *, numerator: int, denominator: int
) -> None:
    """The trial becomes ``active``; its parent (the previous active version, if any)
    is retired in the same transaction — the partial-unique-active index means both
    cannot be ``active`` at once."""
    trial = await session.get(TunableParameter, tunable_parameter_id)
    assert trial is not None
    assert_mutable(trial.key)
    now = datetime.now(UTC)
    if trial.parent_id is not None:
        await session.execute(
            update(TunableParameter)
            .where(TunableParameter.id == trial.parent_id)
            .values(
                status="retired",
                retired_at=now,
                retired_reason=f"superseded by version {trial.version}",
            )
        )
    await session.execute(
        update(TunableParameter)
        .where(TunableParameter.id == tunable_parameter_id)
        .values(
            status="active",
            promoted_at=now,
            trial_numerator=numerator,
            trial_denominator=denominator,
        )
    )
    log.info(
        "learning.trial_confirmed",
        key=trial.key,
        tunable_parameter_id=tunable_parameter_id,
        numerator=numerator,
        denominator=denominator,
    )


async def rollback_trial(
    session: AsyncSession,
    tunable_parameter_id: str,
    *,
    numerator: int,
    denominator: int,
    reason: str,
) -> None:
    """The trial is retired; the previous active version is left exactly as it was —
    there is nothing to "reactivate," it never stopped being active."""
    trial = await session.get(TunableParameter, tunable_parameter_id)
    assert trial is not None
    assert_mutable(trial.key)
    await session.execute(
        update(TunableParameter)
        .where(TunableParameter.id == tunable_parameter_id)
        .values(
            status="retired",
            retired_at=datetime.now(UTC),
            retired_reason=reason[:500],
            trial_numerator=numerator,
            trial_denominator=denominator,
        )
    )
    log.warning(
        "learning.trial_rolled_back",
        key=trial.key,
        tunable_parameter_id=tunable_parameter_id,
        reason=reason[:200],
    )


async def record_episode(
    session: AsyncSession,
    *,
    tunable_parameter_id: str,
    outcome: str,
    detail: dict[str, Any] | None = None,
    task_id: str | None = None,
) -> None:
    session.add(
        LearningEpisode(
            tunable_parameter_id=tunable_parameter_id,
            outcome=outcome,
            detail=detail or {},
            task_id=task_id,
        )
    )
