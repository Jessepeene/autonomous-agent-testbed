"""All database access.

Engines never write SQL. They call these functions, which own the invariants:
idempotent enqueue, leased claims that survive a crash, the never-negative ledger,
and the minimisation boundary for leads.
"""

from __future__ import annotations

import hashlib
import re
import secrets
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlalchemy import CursorResult, delete, func, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from factory.engines.compliance.minimize import minimize
from factory.logging import get_logger
from factory.settings import EngineName, Settings, get_settings
from factory.state.orm import (
    ABTest,
    CampaignApproval,
    ChannelExperiment,
    EntityVerdictRow,
    HealAttempt,
    Idea,
    Interrupt,
    Lead,
    LedgerEntry,
    LLMCall,
    MarketingFreeze,
    OutreachLog,
    PageView,
    Product,
    ProductLock,
    ScoutSignal,
    Task,
    ValidationSignal,
    new_id,
)
from factory.state.schemas import (
    InterruptReason,
    LeadRecord,
    ProductStatus,
    ScoredIdea,
    TaskKind,
    TaskStatus,
    ValidatedIdea,
)

log = get_logger(__name__)

ACTIVE_DEV_STATUSES = (
    ProductStatus.BUILDING.value,
    ProductStatus.REVIEWING.value,
    ProductStatus.PATCHING.value,
)


def _now() -> datetime:
    return datetime.now(UTC)


def fingerprint(*parts: str) -> str:
    joined = "␟".join(p.strip().lower() for p in parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:64]


# ------------------------------------------------------------------------- tasks


async def enqueue_task(
    session: AsyncSession,
    *,
    kind: TaskKind,
    engine: EngineName,
    idempotency_key: str,
    payload: dict[str, Any] | None = None,
    priority: int = 100,
    idea_id: str | None = None,
    product_id: str | None = None,
    not_before: datetime | None = None,
) -> str | None:
    """Insert a task unless the same logical work is already queued.

    Returns the new task id, or ``None`` when the idempotency key already existed —
    which makes "enqueue then crash then enqueue again" safe.
    """
    stmt = (
        pg_insert(Task)
        .values(
            kind=kind.value,
            engine=engine.value,
            status=TaskStatus.PENDING.value,
            priority=priority,
            payload=payload or {},
            idea_id=idea_id,
            product_id=product_id,
            idempotency_key=idempotency_key,
            not_before=not_before,
        )
        .on_conflict_do_nothing(index_elements=[Task.idempotency_key])
        .returning(Task.id)
    )
    result = await session.execute(stmt)
    row = result.scalar_one_or_none()
    return str(row) if row is not None else None


async def claim_next_task(
    session: AsyncSession,
    *,
    worker_id: str,
    engines: Sequence[EngineName],
    settings: Settings | None = None,
) -> Task | None:
    """Lease the highest-priority ready task for one of the given engines.

    ``FOR UPDATE SKIP LOCKED`` means several workers can claim concurrently without
    ever handing the same task to two of them.
    """
    settings = settings or get_settings()
    now = _now()
    stmt = (
        select(Task)
        .where(
            Task.status == TaskStatus.PENDING.value,
            Task.engine.in_([e.value for e in engines]),
            (Task.not_before.is_(None)) | (Task.not_before <= now),
        )
        .order_by(Task.priority.asc(), Task.created_at.asc())
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    task = (await session.execute(stmt)).scalar_one_or_none()
    if task is None:
        return None

    task.status = TaskStatus.CLAIMED.value
    task.claimed_by = worker_id
    task.attempt_count += 1
    task.lease_expires_at = now + timedelta(seconds=settings.task_lease_seconds)
    await session.flush()
    return task


async def reclaim_expired_leases(session: AsyncSession) -> int:
    """Return crashed workers' tasks to the queue. Called on every tick and at boot."""
    stmt = (
        update(Task)
        .where(
            Task.status.in_([TaskStatus.CLAIMED.value, TaskStatus.RUNNING.value]),
            Task.lease_expires_at.isnot(None),
            Task.lease_expires_at < _now(),
        )
        .values(status=TaskStatus.PENDING.value, claimed_by=None, lease_expires_at=None)
    )
    result = cast("CursorResult[Any]", await session.execute(stmt))
    return int(result.rowcount or 0)


async def complete_task(session: AsyncSession, task_id: str) -> None:
    await session.execute(
        update(Task)
        .where(Task.id == task_id)
        .values(
            status=TaskStatus.DONE.value,
            claimed_by=None,
            lease_expires_at=None,
            last_error=None,
        )
    )


async def fail_task(
    session: AsyncSession,
    task_id: str,
    error: str,
    *,
    settings: Settings | None = None,
) -> bool:
    """Record a failure. Returns True when the task has exhausted its attempts.

    Three autonomous attempts, then escalate — the decision hierarchy's last step.
    """
    settings = settings or get_settings()
    task = await session.get(Task, task_id)
    if task is None:
        return False
    exhausted = task.attempt_count >= settings.max_task_attempts
    task.status = TaskStatus.ESCALATED.value if exhausted else TaskStatus.PENDING.value
    task.claimed_by = None
    task.lease_expires_at = None
    task.last_error = error[:4000]
    if not exhausted:
        backoff = min(2**task.attempt_count, 60)
        task.not_before = _now() + timedelta(seconds=backoff)
    await session.flush()
    return exhausted


async def reopen_escalated_task(session: AsyncSession, task_id: str) -> bool:
    """Give an escalated task a fresh attempt budget.

    The human-facing counterpart to `fail_task`'s exhaustion path: a reply
    confirming the root cause is fixed (see `hitl.interrupts.collect_replies`)
    turns back into exactly the state a task is in right after `enqueue_task` --
    nothing here is retry-specific, it is just undoing exhaustion. Only acts on a
    task that is actually `escalated`; replying to an interrupt whose task already
    moved on (retried some other way, or the product was retired) is a no-op, not
    an error.
    """
    task = await session.get(Task, task_id)
    if task is None or task.status != TaskStatus.ESCALATED.value:
        return False
    task.status = TaskStatus.PENDING.value
    task.attempt_count = 0
    task.claimed_by = None
    task.lease_expires_at = None
    task.not_before = None
    task.last_error = None
    await session.flush()
    return True


#: Retention is tied to the owning product's lifecycle, not a fixed calendar cutoff
#: -- see `purge_product_pii`. Left short and unambiguous so a human auditing the
#: table can tell a purge from a normal empty/short escalation reply at a glance.
_PII_REDACTED = "[redacted: retention tied to product lifecycle, see purge_product_pii]"


async def retire_product(session: AsyncSession, product_id: str) -> bool:
    """The single terminal transition for a product, however it was reached (a
    human's explicit REJECT to a heal-exhaustion escalation, or a validation gate
    that never cleared its interest bar). Terminal -- nothing else in this codebase
    ever moves a product out of ``retired``.

    Also the one place PII retention is enforced: once a product can never do
    anything else, the free-text fields that carried professional contact context
    about real people (`CampaignApproval.brief`, `Interrupt.detail`) have no further
    operational purpose.
    """
    product = await session.get(Product, product_id)
    if product is None:
        return False
    product.status = ProductStatus.RETIRED.value
    await purge_product_pii(session, product_id)
    await session.flush()
    return True


async def purge_product_pii(session: AsyncSession, product_id: str) -> int:
    """Redact the free-text fields a retired product no longer needs.

    Subject/status/timestamps are left intact everywhere -- the audit trail (what
    kind of thing happened, when, how it was resolved) survives; only the free text
    a human or model wrote role/company detail into does not. Idempotent: already-
    redacted rows are excluded from the ``WHERE``, so calling this twice (e.g. a
    retry after a crash) does not inflate the returned count.
    """
    interrupt_ids = (
        (
            await session.execute(
                select(Interrupt.id).where(
                    or_(
                        Interrupt.product_id == product_id,
                        Interrupt.task_id.in_(select(Task.id).where(Task.product_id == product_id)),
                    )
                )
            )
        )
        .scalars()
        .all()
    )

    purged = 0
    if interrupt_ids:
        result = cast(
            "CursorResult[Any]",
            await session.execute(
                update(Interrupt)
                .where(Interrupt.id.in_(interrupt_ids), Interrupt.detail != _PII_REDACTED)
                .values(detail=_PII_REDACTED)
            ),
        )
        purged += result.rowcount or 0

    result = cast(
        "CursorResult[Any]",
        await session.execute(
            update(CampaignApproval)
            .where(
                CampaignApproval.product_id == product_id,
                CampaignApproval.brief != _PII_REDACTED,
            )
            .values(brief=_PII_REDACTED)
        ),
    )
    purged += result.rowcount or 0
    return purged


async def enqueue_guided_patch(
    session: AsyncSession, *, product_id: str, guidance: str, interrupt_id: str
) -> str | None:
    """A human's explicit guidance reply to a heal-exhaustion escalation -- exactly
    one more patch attempt, not a reset of the heal budget.

    ``product.heal_attempts`` is left untouched (already at ``MAX_HEAL_CYCLES``): if
    this attempt also fails, ``_heal()`` computes ``attempts > MAX_HEAL_CYCLES`` and
    re-escalates immediately instead of silently burning further autonomous cycles --
    a human has to explicitly ask for each additional try, which matters because each
    one is a real code-generation call, not a free retry. ``interrupt_id`` (unique per
    escalation) makes the idempotency key unique per guided attempt -- a product can
    re-escalate and be guided again more than once, and a fixed key would silently
    swallow every attempt after the first via ON CONFLICT DO NOTHING.
    """
    product = await session.get(Product, product_id)
    if product is None or product.idea_id is None:
        return None
    return await enqueue_task(
        session,
        kind=TaskKind.PATCH_PRODUCT,
        engine=EngineName.DEVELOPMENT,
        idempotency_key=f"patch:{product_id}:human-guided:{interrupt_id}",
        payload={
            "product_id": product_id,
            "idea_id": product.idea_id,
            "healing": guidance[:6000],
            # Routes to the surgical repair path (see `_should_repair_surgically`).
            # A human who has diagnosed the problem and said what to change is the
            # single best case for editing in place: regenerating an entire live
            # product from scratch over one instruction is both the most expensive
            # option and the one most likely to break things that currently work.
            "category": "guided",
        },
        product_id=product_id,
        priority=40,
    )


async def defer_task(session: AsyncSession, task_id: str, *, not_before: datetime) -> None:
    """Return a task to the queue without counting it as a failed attempt.

    Distinct from `fail_task`: a task blocked on an external event it cannot control
    (a pending human approval) has not failed and must never move toward
    `max_task_attempts` exhaustion — it should simply be tried again later, for as
    long as it takes.
    """
    await session.execute(
        update(Task)
        .where(Task.id == task_id)
        .values(
            status=TaskStatus.PENDING.value,
            claimed_by=None,
            lease_expires_at=None,
            not_before=not_before,
        )
    )


async def count_active_dev_tasks(session: AsyncSession) -> int:
    """Products currently building, under review, or being patched.

    Drives the Discovery throttle: at capacity, new ideas wait rather than piling up
    faster than Development can drain them.
    """
    stmt = select(func.count()).select_from(Product).where(Product.status.in_(ACTIVE_DEV_STATUSES))
    return int((await session.execute(stmt)).scalar_one())


async def count_pending_dev_retries(session: AsyncSession) -> int:
    """Pending Development tasks (``BUILD_MVP`` or ``PATCH_PRODUCT``) whose product
    is *already* counted in `count_active_dev_tasks`.

    Generalises what used to be a `PATCH_PRODUCT`-only check: a retried `BUILD_MVP`
    for a product stuck in `BUILDING` after an earlier attempt threw (the status is
    set before the attempt and never reset on failure) is exactly the same shape of
    problem -- claiming it doesn't admit a new product into the pipeline either, so
    the capacity gate must not hold it back the way it holds back a genuinely new
    build. Without this, two failed builds in a row can permanently fill both of
    `max_concurrent_builds`' slots with products whose only pending work is a retry
    the gate never recognises as safe to claim.
    """
    stmt = (
        select(func.count())
        .select_from(Task)
        .join(Product, Product.id == Task.product_id)
        .where(
            Task.status == TaskStatus.PENDING.value,
            Task.engine == EngineName.DEVELOPMENT.value,
            Task.kind.in_((TaskKind.BUILD_MVP.value, TaskKind.PATCH_PRODUCT.value)),
            Product.status.in_(ACTIVE_DEV_STATUSES),
        )
    )
    return int((await session.execute(stmt)).scalar_one())


async def count_active_validations(session: AsyncSession) -> int:
    """Products currently running a validation (landing page + outreach).

    Capped separately from `count_active_dev_tasks`/`max_concurrent_builds`: a
    validation is deliberately much cheaper than a real MVP build and must not
    compete with real builds for the same capacity slots, but it still needs its
    own cap -- otherwise every approved idea spawns an unbounded parallel
    validation, flooding the campaign-approval queue and burning the shared,
    metered lead provider quota before any of them prove out.
    """
    stmt = (
        select(func.count())
        .select_from(Product)
        .where(Product.status == ProductStatus.VALIDATING.value)
    )
    return int((await session.execute(stmt)).scalar_one())


async def count_pending_tasks(
    session: AsyncSession,
    engine: EngineName | None = None,
    *,
    kind: TaskKind | None = None,
) -> int:
    stmt = select(func.count()).select_from(Task).where(Task.status == TaskStatus.PENDING.value)
    if engine is not None:
        stmt = stmt.where(Task.engine == engine.value)
    if kind is not None:
        stmt = stmt.where(Task.kind == kind.value)
    return int((await session.execute(stmt)).scalar_one())


# ------------------------------------------------------------------------- ideas


async def upsert_idea(
    session: AsyncSession,
    idea: ScoredIdea,
    *,
    score: float | None = None,
    evidence_count: int = 0,
) -> str | None:
    """Store a scored idea, de-duplicated by problem + niche fingerprint.

    ``score`` defaults to ``idea.score`` (the fixed historical weights) — pass it
    explicitly only when the caller has fetched tuned weights and wants
    ``idea.weighted_score(weights)`` stored instead.

    ``evidence_count`` defaults to 0 for any caller that predates it (tests
    constructing an idea directly, for instance) -- ``discovery/graph.py`` is the
    only real caller, and it passes the source cluster's signal count.
    """
    stored_score = score if score is not None else idea.score
    key = fingerprint(idea.problem_statement, idea.target_niche)
    stmt = (
        pg_insert(Idea)
        .values(
            problem_statement=idea.problem_statement,
            target_niche=idea.target_niche,
            source_url=str(idea.source_url),
            monetization_hypothesis=idea.monetization_hypothesis,
            score=stored_score,
            evidence_count=evidence_count,
            fingerprint=key,
            status="pending",
        )
        .on_conflict_do_nothing(index_elements=[Idea.fingerprint])
        .returning(Idea.id)
    )
    row = (await session.execute(stmt)).scalar_one_or_none()
    return str(row) if row is not None else None


async def next_validated_idea(session: AsyncSession) -> ValidatedIdea | None:
    stmt = (
        select(Idea)
        .where(Idea.status == "approved")
        .order_by(Idea.score.desc(), Idea.created_at.asc())
        .limit(1)
    )
    idea = (await session.execute(stmt)).scalar_one_or_none()
    if idea is None:
        return None
    return ValidatedIdea(
        id=idea.id,
        problem_statement=idea.problem_statement,
        target_niche=idea.target_niche,
        source_url=idea.source_url,
        monetization_hypothesis=idea.monetization_hypothesis,
        score=idea.score,
        evidence_count=idea.evidence_count,
        created_at=idea.created_at,
    )


async def set_idea_status(session: AsyncSession, idea_id: str, status: str) -> None:
    await session.execute(update(Idea).where(Idea.id == idea_id).values(status=status))


async def pending_ideas(session: AsyncSession, limit: int = 20) -> list[Idea]:
    stmt = (
        select(Idea)
        .where(Idea.status == "pending")
        .order_by(Idea.score.desc(), Idea.created_at.asc())
        .limit(limit)
    )
    return list((await session.execute(stmt)).scalars().all())


# ---------------------------------------------------------------------- products


async def create_product(session: AsyncSession, *, name: str, idea_id: str) -> str:
    product = Product(name=name, idea_id=idea_id, status=ProductStatus.PLANNED.value)
    session.add(product)
    await session.flush()
    return product.id


async def set_product_status(
    session: AsyncSession,
    product_id: str,
    status: ProductStatus,
    **fields: Any,
) -> None:
    await session.execute(
        update(Product).where(Product.id == product_id).values(status=status.value, **fields)
    )


async def get_product(session: AsyncSession, product_id: str) -> Product | None:
    return await session.get(Product, product_id)


async def get_product_by_workspace(session: AsyncSession, workspace: str) -> Product | None:
    """The reverse lookup of `Product.workspace_path` -- workspace directory names
    are a truncated, one-way derivation of the product id (`f"product-{id[:12]}"`),
    so a caller holding only the workspace name (e.g. a sandbox tool invocation)
    cannot recover the owning product any other way."""
    stmt = select(Product).where(Product.workspace_path == workspace)
    return (await session.execute(stmt)).scalar_one_or_none()


async def record_heal_attempt(
    session: AsyncSession,
    *,
    product_id: str,
    task_id: str | None,
    attempt_number: int,
    healing_text: str,
    failed_categories: list[str],
) -> str:
    row = HealAttempt(
        product_id=product_id,
        task_id=task_id,
        attempt_number=attempt_number,
        healing_text=healing_text[:6000],
        failed_categories=failed_categories,
    )
    session.add(row)
    await session.flush()
    return row.id


async def resolve_pending_heal_attempts(
    session: AsyncSession, product_id: str, outcome: str
) -> None:
    """Resolves any heal attempt still ``pending`` for this product — in practice at
    most one, since a new attempt is only ever created after the previous one is
    resolved."""
    await session.execute(
        update(HealAttempt)
        .where(HealAttempt.product_id == product_id, HealAttempt.outcome == "pending")
        .values(outcome=outcome, resolved_at=_now())
    )


async def live_products(session: AsyncSession, limit: int = 20) -> list[Product]:
    stmt = (
        select(Product)
        .where(Product.status == ProductStatus.LIVE.value)
        .order_by(Product.updated_at.desc())
        .limit(limit)
    )
    return list((await session.execute(stmt)).scalars().all())


async def active_products(session: AsyncSession, limit: int = 20) -> list[Product]:
    """Every product still doing something -- everything except RETIRED, not just
    LIVE -- for `/status`'s Telegram report (`hitl/commands.py`), which should show
    a product mid-build or mid-validation too, not only ones already shipped."""
    stmt = (
        select(Product)
        .where(Product.status != ProductStatus.RETIRED.value)
        .order_by(Product.updated_at.desc())
        .limit(limit)
    )
    return list((await session.execute(stmt)).scalars().all())


async def set_growth_reviewed(session: AsyncSession, product_id: str) -> None:
    await _set_product_timestamp(session, product_id, "last_growth_review_at")


async def set_channel_scouted(session: AsyncSession, product_id: str) -> None:
    await _set_product_timestamp(session, product_id, "last_channel_scout_at")


async def _set_product_timestamp(session: AsyncSession, product_id: str, column: str) -> None:
    await session.execute(
        update(Product).where(Product.id == product_id).values(**{column: _now()})
    )


async def products_needing_growth_review(
    session: AsyncSession, *, older_than: timedelta, limit: int = 10
) -> list[Product]:
    """LIVE products GROWTH_ANALYST has never reviewed, or not recently -- what the
    periodic backstop sweep and the `list_products_needing_growth_review` tool both
    read from.

    A never-reviewed product's staleness is measured from ``created_at``, not
    treated as infinitely overdue -- a product that went live five minutes ago has
    no traffic data yet, and reviewing it immediately would compete with
    `HARVEST_LEADS` for the same tick's capacity for zero value. Once eligible,
    ordered oldest-effective-review-first, same idea either way.
    """
    cutoff = _now() - older_than
    effective = func.coalesce(Product.last_growth_review_at, Product.created_at)
    stmt = (
        select(Product)
        .where(Product.status == ProductStatus.LIVE.value, effective < cutoff)
        .order_by(effective.asc())
        .limit(limit)
    )
    return list((await session.execute(stmt)).scalars().all())


async def products_needing_channel_scout(
    session: AsyncSession, *, older_than: timedelta, limit: int = 10
) -> list[Product]:
    """Same reasoning as `products_needing_growth_review`, for CHANNEL_SCOUT."""
    cutoff = _now() - older_than
    effective = func.coalesce(Product.last_channel_scout_at, Product.created_at)
    stmt = (
        select(Product)
        .where(Product.status == ProductStatus.LIVE.value, effective < cutoff)
        .order_by(effective.asc())
        .limit(limit)
    )
    return list((await session.execute(stmt)).scalars().all())


async def stalled_validations(
    session: AsyncSession, *, older_than: timedelta, limit: int = 10
) -> list[Product]:
    """VALIDATING products old enough that their one-shot validation harvest
    (``leads:validate:{id}``, see `development/graph.py::_build_landing_page`)
    has had time to run, but that still have zero leads at all -- the exact
    shape of a silent dead end. `HARVEST_LEADS` is enqueued exactly once per
    validating product; nothing else ever asks again on its own, so a product
    stuck this way (an external lead-provider credential rejected, or a niche
    too narrow for either provider) previously just sat there forever with no
    automatic path forward and nothing telling a human -- observed live.

    Excludes any product that already has an open interrupt -- a human is
    already being asked, so this must not pile on a second automatic action
    (a retry, or another escalation) while that reply is still pending.
    """
    cutoff = _now() - older_than
    no_leads = ~select(Lead.id).where(Lead.product_id == Product.id).exists()
    no_open_interrupt = (
        ~select(Interrupt.id)
        .where(Interrupt.product_id == Product.id, Interrupt.status == "open")
        .exists()
    )
    stmt = (
        select(Product)
        .where(
            Product.status == ProductStatus.VALIDATING.value,
            Product.created_at < cutoff,
            no_leads,
            no_open_interrupt,
        )
        .order_by(Product.created_at.asc())
        .limit(limit)
    )
    return list((await session.execute(stmt)).scalars().all())


async def has_retried_lead_sourcing(session: AsyncSession, product_id: str) -> bool:
    """Whether more than the original one-shot validation harvest has ever run
    for this product -- the signal the stalled-validation sweep
    (`scheduler.py::_schedule_periodic`) uses to decide "try once more"
    (the first time) vs. "ask a human" (every time after), regardless of
    whether that later attempt came from the sweep's own retry or a human's
    `#tag <answer>` reply asking for one.
    """
    stmt = (
        select(func.count())
        .select_from(Task)
        .where(Task.product_id == product_id, Task.kind == TaskKind.HARVEST_LEADS.value)
    )
    return int((await session.execute(stmt)).scalar_one()) >= 2


async def set_vercel_project(session: AsyncSession, product_id: str, project: str) -> None:
    await session.execute(
        update(Product).where(Product.id == product_id).values(vercel_project=project)
    )


async def set_domain_requested(session: AsyncSession, product_id: str) -> None:
    """Called once, right after `development/domain.py::ensure_requested` raises
    the interrupt -- marks the product so a later redeploy never asks again."""
    await session.execute(
        update(Product).where(Product.id == product_id).values(domain_status="pending")
    )


async def set_product_domain(session: AsyncSession, product_id: str, hostname: str) -> None:
    """The human's reply, captured -- `domain_status` stays 'pending' until the
    `ATTACH_DOMAIN` task actually calls Vercel."""
    await session.execute(
        update(Product).where(Product.id == product_id).values(custom_domain=hostname)
    )


async def set_domain_status(
    session: AsyncSession, product_id: str, status: str, **fields: Any
) -> None:
    await session.execute(
        update(Product).where(Product.id == product_id).values(domain_status=status, **fields)
    )


async def products_with_attached_domain(session: AsyncSession, limit: int = 20) -> list[Product]:
    """Domains Vercel has but DNS has not yet confirmed -- what the periodic
    `VERIFY_DOMAIN` sweep (scheduler.py) reads from."""
    stmt = select(Product).where(Product.domain_status == "attached").limit(limit)
    return list((await session.execute(stmt)).scalars().all())


# ------------------------------------------------------------------ product locks


async def acquire_product_lock(
    session: AsyncSession,
    *,
    product_id: str,
    holder: EngineName,
    reason: str,
    ttl_seconds: int = 3600,
    blocks_paid_spend: bool = True,
) -> bool:
    stmt = (
        pg_insert(ProductLock)
        .values(
            product_id=product_id,
            holder=holder.value,
            reason=reason,
            blocks_paid_spend=blocks_paid_spend,
            expires_at=_now() + timedelta(seconds=ttl_seconds),
        )
        .on_conflict_do_nothing(index_elements=[ProductLock.product_id])
        .returning(ProductLock.product_id)
    )
    return (await session.execute(stmt)).scalar_one_or_none() is not None


async def release_product_lock(session: AsyncSession, product_id: str) -> None:
    await session.execute(delete(ProductLock).where(ProductLock.product_id == product_id))


async def is_paid_spend_paused(session: AsyncSession, product_id: str) -> bool:
    """True while a product is unfit to receive paid traffic.

    Two conditions, and both are needed. The lock covers work in flight *right now*.
    The status covers the gap the lock cannot: when a build fails, the lock is
    released as the engine unwinds, but the repair task is only queued — so for the
    minutes or hours until a worker picks it up, a product known to be broken would
    otherwise look spendable. Status is the durable fact; the lock is just the
    in-flight marker on top of it.
    """
    lock_stmt = select(ProductLock).where(
        ProductLock.product_id == product_id,
        ProductLock.blocks_paid_spend.is_(True),
        ProductLock.expires_at > _now(),
    )
    if (await session.execute(lock_stmt)).scalar_one_or_none() is not None:
        return True

    status_stmt = select(Product.status).where(Product.id == product_id)
    status = (await session.execute(status_stmt)).scalar_one_or_none()
    return status in ACTIVE_DEV_STATUSES


async def purge_expired_locks(session: AsyncSession) -> int:
    result = cast(
        "CursorResult[Any]",
        await session.execute(delete(ProductLock).where(ProductLock.expires_at <= _now())),
    )
    return int(result.rowcount or 0)


# -------------------------------------------------------------------------- leads


async def insert_lead(
    session: AsyncSession,
    raw: Mapping[str, Any],
    *,
    lia_passed: bool,
    lia_justification: str | None = None,
    product_id: str | None = None,
    provenance_kind: str = "operator_supplied",
    provider: str = "operator-csv",
) -> LeadRecord:
    """The only sanctioned write path into ``leads``.

    Extraneous scraped keys are dropped by ``minimize()`` before the INSERT, and a
    lead with no ``source_url`` raises rather than being stored.
    """
    record = minimize(raw)
    stmt = (
        pg_insert(Lead)
        .values(
            first_name=record.first_name,
            last_name=record.last_name,
            job_title=record.job_title,
            company=record.company,
            work_email=record.work_email,
            source_url=record.source_url,
            scraped_at=record.scraped_at,
            provenance_kind=provenance_kind,
            provider=provider,
            lia_passed=lia_passed,
            lia_justification=lia_justification,
            product_id=product_id,
        )
        .on_conflict_do_nothing(index_elements=[Lead.work_email])
    )
    await session.execute(stmt)
    return record


_COMPANY_SUFFIX_RE = re.compile(r"\b(inc|llc|ltd|corp|corporation|co)\.?\b")


def _normalize_company_name(company: str) -> str:
    """The same real company can be scraped with different punctuation or a
    different legal suffix on different passes ("Design Electric, Inc." vs
    "Design Electric Inc.") -- comparing normalized text, not ``==``, avoids
    treating them as two different companies. Ported from
    `reference files/lead_scout.py::_normalize_company`, where this exact gap
    was found in production."""
    text = company.lower()
    text = re.sub(r"[.,]", "", text)
    text = _COMPANY_SUFFIX_RE.sub("", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


async def has_lead_for_company(
    session: AsyncSession, *, company: str, name: str | None = None
) -> bool:
    """Pre-enrichment dedup for the web-search lead provider: avoids spending a
    Hunter Finder call on a company+name already known, *before* an email is even
    found -- complementary to (not a replacement for) `insert_lead`'s
    work_email-based ``ON CONFLICT`` at storage time, which only catches
    duplicates once an email already exists.

    Fetches every lead's company/name and compares in Python, matching
    `reference files/lead_scout.py::_already_have`'s own reasoning: the
    normalization isn't expressible as a simple SQL case-fold, and the leads
    table is small at this factory's current scale. Revisit with a narrower SQL
    filter if that stops being true.
    """
    target_company = _normalize_company_name(company)
    stmt = select(Lead.company, Lead.first_name, Lead.last_name)
    rows = (await session.execute(stmt)).all()
    matching_names = [
        f"{first} {last}".strip().lower()
        for existing_company, first, last in rows
        if _normalize_company_name(existing_company) == target_company
    ]
    if not matching_names:
        return False
    if not name:
        return True
    return name.strip().lower() in matching_names


async def recently_processed_urls(session: AsyncSession, *, since: datetime) -> set[str]:
    """Source URLs already scraped since ``since`` -- lets the web-search lead
    provider skip re-scraping the same evergreen directory/team page on every
    harvest call. Ported from
    `reference files/lead_scout.py::_recently_processed_urls`; production proof
    there: two directory URLs alone were independently re-extracted 9 and 2
    separate times within ~36 hours, accounting for 56% of every lead ever found.
    """
    stmt = select(Lead.source_url).where(Lead.scraped_at >= since)
    return set((await session.execute(stmt)).scalars().all())


async def set_lead_qualification_brief(
    session: AsyncSession, product_id: str, brief_json: str
) -> None:
    await session.execute(
        update(Product).where(Product.id == product_id).values(lead_qualification_brief=brief_json)
    )


async def erase_lead(
    session: AsyncSession, email: str, *, settings: Settings | None = None
) -> None:
    """GDPR Art. 17 erasure.

    Deleting the row alone is not enough — the same address would be re-scraped and
    re-contacted tomorrow. The address is also tombstoned into ``blacklist.txt`` so
    ``send_email()`` refuses it permanently.
    """
    settings = settings or get_settings()
    normalised = email.strip().lower()
    await session.execute(delete(Lead).where(Lead.work_email == normalised))

    path = settings.blacklist_path
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    if normalised not in existing:
        stamp = _now().isoformat()
        with path.open("a", encoding="utf-8") as handle:
            handle.write(f"\n{normalised}  # ERASURE requested {stamp}\n")
    log.info("gdpr.erasure", email_domain=normalised.partition("@")[2])


async def leads_for_outreach(
    session: AsyncSession, limit: int = 25, *, product_id: str | None = None
) -> list[Lead]:
    """The oldest LIA-passed leads not yet contacted.

    ``product_id`` scopes to leads sourced for one specific idea -- required for a
    validation run, where the response rate must measure interest in *that* idea,
    not a global pool. Omitted (the default), this is the existing steady-state
    behaviour: the oldest ready leads across every product.

    "Not yet contacted" was previously only a claim in this docstring -- the query
    never actually excluded a lead with prior outreach history, so the same address
    could be re-selected and re-emailed on every subsequent call indefinitely. A
    lead is excluded once it has a ``sent`` ``OutreachLog`` row: a ``blocked``
    (content-policy) or ``suppressed`` (blacklist/transport) attempt never actually
    reached the recipient, so it deliberately remains eligible for a retry.
    """
    already_contacted = (
        select(OutreachLog.lead_id)
        .where(OutreachLog.lead_id == Lead.id, OutreachLog.status == "sent")
        .exists()
    )
    stmt = select(Lead).where(Lead.lia_passed.is_(True), ~already_contacted)
    if product_id is not None:
        stmt = stmt.where(Lead.product_id == product_id)
    stmt = stmt.order_by(Lead.created_at.asc()).limit(limit)
    return list((await session.execute(stmt)).scalars().all())


async def count_leads(session: AsyncSession, product_id: str) -> int:
    stmt = select(func.count()).select_from(Lead).where(Lead.product_id == product_id)
    return int((await session.execute(stmt)).scalar_one())


async def insert_validation_signal(
    session: AsyncSession,
    *,
    product_id: str,
    email: str,
    message: str | None = None,
    variant: str | None = None,
) -> str | None:
    """Record real interest in a product still in its validation stage.

    ``ON CONFLICT DO NOTHING`` on ``(product_id, email)`` -- a repeat submission
    from the same address must never inflate the count a build-or-scrap decision is
    gated on. Returns ``None`` on a duplicate, matching this codebase's existing
    idempotent-insert idiom (`enqueue_task`, `open_interrupt`). ``variant`` is
    whatever AB-test arm the submitting page reported showing -- not verified
    against a currently-running test here, since a stray or late report from a
    test that just concluded is still a real signal worth keeping, just not one
    `tally_ab_test_outcomes` will count once the test's row is no longer `running`.
    """
    stmt = (
        pg_insert(ValidationSignal)
        .values(
            product_id=product_id, email=email.strip().lower(), message=message, variant=variant
        )
        .on_conflict_do_nothing(
            index_elements=[ValidationSignal.product_id, ValidationSignal.email]
        )
        .returning(ValidationSignal.id)
    )
    row = (await session.execute(stmt)).scalar_one_or_none()
    return str(row) if row is not None else None


async def tally_validation_signals(
    session: AsyncSession, product_id: str, *, variant: str | None = None
) -> int:
    stmt = (
        select(func.count())
        .select_from(ValidationSignal)
        .where(ValidationSignal.product_id == product_id)
    )
    if variant is not None:
        stmt = stmt.where(ValidationSignal.variant == variant)
    return int((await session.execute(stmt)).scalar_one())


async def record_page_view(
    session: AsyncSession,
    *,
    product_id: str,
    path: str,
    referrer: str | None = None,
    variant: str | None = None,
) -> None:
    """A page view is anonymous by design -- no dedup, no identity, unlike
    `insert_validation_signal`'s per-email upsert. The caller (the public
    `/analytics/{product_id}` endpoint) checks the product exists first and 404s
    before this is ever reached, matching `/interest/{product_id}`'s pattern --
    this function assumes that check already happened and lets the FK raise if it
    somehow didn't.
    """
    stmt = pg_insert(PageView).values(
        product_id=product_id,
        path=path,
        referrer=referrer,
        variant=variant,
    )
    await session.execute(stmt)


async def tally_page_views(
    session: AsyncSession,
    product_id: str,
    *,
    since: datetime | None = None,
    variant: str | None = None,
) -> int:
    stmt = select(func.count()).select_from(PageView).where(PageView.product_id == product_id)
    if since is not None:
        stmt = stmt.where(PageView.created_at >= since)
    if variant is not None:
        stmt = stmt.where(PageView.variant == variant)
    return int((await session.execute(stmt)).scalar_one())


async def count_page_views_from_referrer(
    session: AsyncSession, product_id: str, *, since: datetime, referrer_contains: str
) -> int:
    """Best-effort measurement for a `ChannelExperiment` -- how many visits since it
    was approved carried a referrer naming the channel's own domain. Best-effort
    because `referrer` is whatever the visitor's browser sent (or omitted); this is
    a growth signal, never something a spend or compliance decision is gated on."""
    stmt = (
        select(func.count())
        .select_from(PageView)
        .where(
            PageView.product_id == product_id,
            PageView.created_at >= since,
            PageView.referrer.ilike(f"%{referrer_contains}%"),
        )
    )
    return int((await session.execute(stmt)).scalar_one())


async def create_ab_test(
    session: AsyncSession, *, product_id: str, variant_a_brief: str, variant_b_brief: str
) -> str | None:
    """Start a live split test. Returns ``None`` if this product already has one
    running -- the partial unique index on ``(product_id) WHERE status='running'``
    (migration `0013`) is what actually enforces this; a caller racing another
    worker to start a test gets a clean no-op here, not a constraint violation.

    ``index_where`` must be a literal ``text()`` clause, not an ORM comparison
    (``ABTest.status == "running"``) -- the latter compiles to a bound parameter
    (``status = $1``), and Postgres only matches an ``ON CONFLICT ... WHERE``
    predicate against a partial index by comparing parsed expression trees: a
    ``Param`` node never structurally matches the index's own ``Const`` node,
    regardless of the runtime value, so every insert raised
    ``InvalidColumnReferenceError`` instead of ever hitting the no-op path. Caught
    by this phase's own integration tests exercising the conflict for the first
    time; the identical pre-existing pattern in `create_marketing_freeze` had the
    same bug, unexercised by any test until fixed alongside this one.
    """
    stmt = (
        pg_insert(ABTest)
        .values(
            product_id=product_id,
            variant_a_brief=variant_a_brief,
            variant_b_brief=variant_b_brief,
        )
        .on_conflict_do_nothing(
            index_elements=[ABTest.product_id],
            index_where=text("status = 'running'"),
        )
        .returning(ABTest.id)
    )
    row = (await session.execute(stmt)).scalar_one_or_none()
    return str(row) if row is not None else None


async def get_running_ab_test(session: AsyncSession, product_id: str) -> ABTest | None:
    stmt = select(ABTest).where(ABTest.product_id == product_id, ABTest.status == "running")
    return (await session.execute(stmt)).scalar_one_or_none()


async def running_ab_tests(session: AsyncSession, *, limit: int = 20) -> list[ABTest]:
    """Every test still open, oldest first -- what a periodic sweep evaluates."""
    stmt = (
        select(ABTest)
        .where(ABTest.status == "running")
        .order_by(ABTest.started_at.asc())
        .limit(limit)
    )
    return list((await session.execute(stmt)).scalars().all())


async def conclude_ab_test(session: AsyncSession, ab_test_id: str, *, winner: str) -> None:
    await session.execute(
        update(ABTest)
        .where(ABTest.id == ab_test_id)
        .values(status="concluded", winner=winner, concluded_at=_now())
    )


async def tally_ab_test_outcomes(
    session: AsyncSession, product_id: str, *, since: datetime
) -> dict[str, tuple[int, int]]:
    """Per-variant ``(views, signals)`` since the test started, for both arms.

    Two tables, not one: `page_views` is every visit (the denominator),
    `validation_signals` is the subset that actually converted (the numerator) --
    the same relationship `_evaluate_validation` already uses for the sequential
    validation gate, just split by arm here instead of summed across the product.
    """
    result: dict[str, tuple[int, int]] = {}
    for variant in ("a", "b"):
        views = await tally_page_views(session, product_id, since=since, variant=variant)
        signals = await tally_validation_signals(session, product_id, variant=variant)
        result[variant] = (views, signals)
    return result


async def count_outreach_sent(session: AsyncSession, product_id: str) -> int:
    stmt = (
        select(func.count())
        .select_from(OutreachLog)
        .where(OutreachLog.product_id == product_id, OutreachLog.status == "sent")
    )
    return int((await session.execute(stmt)).scalar_one())


async def erase_validation_signals(session: AsyncSession, email: str) -> None:
    """GDPR Art. 17 erasure, extended to validation-stage interest submissions --
    a prospect-initiated form fill is still personal data. Called alongside
    `erase_lead`, not as a replacement for it (the two tables are unrelated)."""
    normalised = email.strip().lower()
    await session.execute(delete(ValidationSignal).where(ValidationSignal.email == normalised))


async def record_outreach(
    session: AsyncSession,
    *,
    lead_id: str,
    subject: str,
    status: str,
    product_id: str | None = None,
    suppressed_reason: str | None = None,
    id: str | None = None,
    unsubscribe_token: str | None = None,
) -> str:
    """``id`` may be pre-generated by the caller (see `marketing/outreach.py`) so the
    same id can be handed to the deliverability provider as a tracking variable
    *before* this row exists — its webhook needs something to correlate back to.

    ``unsubscribe_token`` must likewise be pre-generated by the caller whenever an
    email is actually going out: the token embedded in the sent message and the
    token stored here must be the same value, or `GET /u/{token}` can never resolve
    it. Only generate one here when the caller has none to give (e.g. a `blocked`
    row that was never sent to anyone)."""
    entry = OutreachLog(
        id=id or new_id(),
        lead_id=lead_id,
        product_id=product_id,
        subject=subject[:500],
        status=status,
        suppressed_reason=suppressed_reason,
        unsubscribe_token=unsubscribe_token or secrets.token_urlsafe(32),
    )
    session.add(entry)
    await session.flush()
    return entry.id


async def outreach_count_for_domain(session: AsyncSession, domain: str, days: int = 30) -> int:
    since = _now() - timedelta(days=days)
    stmt = (
        select(func.count())
        .select_from(OutreachLog)
        .join(Lead, Lead.id == OutreachLog.lead_id)
        .where(Lead.work_email.like(f"%@{domain}"), OutreachLog.sent_at >= since)
    )
    return int((await session.execute(stmt)).scalar_one())


async def first_revenue_ledger_entries_needing_attribution(
    session: AsyncSession,
) -> list[LedgerEntry]:
    """The earliest ``revenue`` ledger row per attributable product, excluding any
    already claimed by an outreach row. A renewal (a product's *second* revenue row)
    is deliberately never a candidate — it isn't a new "outreach worked" signal."""
    first_per_product = (
        select(
            LedgerEntry.product_id.label("product_id"),
            func.min(LedgerEntry.created_at).label("first_at"),
        )
        .where(LedgerEntry.kind == "revenue", LedgerEntry.product_id.is_not(None))
        .group_by(LedgerEntry.product_id)
        .subquery()
    )
    stmt = (
        select(LedgerEntry)
        .join(
            first_per_product,
            (LedgerEntry.product_id == first_per_product.c.product_id)
            & (LedgerEntry.created_at == first_per_product.c.first_at),
        )
        .outerjoin(OutreachLog, OutreachLog.ledger_entry_id == LedgerEntry.id)
        .where(OutreachLog.id.is_(None))
    )
    return list((await session.execute(stmt)).scalars().all())


async def last_outreach_touch(
    session: AsyncSession, *, product_id: str, before: datetime, window_days: int
) -> OutreachLog | None:
    """The most recent successful send for this product inside the attribution
    window — last touch, not first: with no buyer identity, first-touch would let an
    early send in a multi-touch sequence hog credit for a conversion it may be
    unrelated to."""
    since = before - timedelta(days=window_days)
    stmt = (
        select(OutreachLog)
        .where(
            OutreachLog.product_id == product_id,
            OutreachLog.status == "sent",
            OutreachLog.sent_at >= since,
            OutreachLog.sent_at <= before,
        )
        .order_by(OutreachLog.sent_at.desc())
        .limit(1)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def attribute_outreach(
    session: AsyncSession, *, outreach_log_id: str, ledger_entry_id: str, converted_at: datetime
) -> None:
    await session.execute(
        update(OutreachLog)
        .where(OutreachLog.id == outreach_log_id)
        .values(ledger_entry_id=ledger_entry_id, converted_at=converted_at)
    )


# ------------------------------------------------------------------------- ledger

#: Arbitrary fixed key for a Postgres session-transaction advisory lock (see
#: `append_ledger`) -- any bigint works, it just has to be the same constant every
#: call. Not tied to any row or table, since there is nothing to `SELECT ... FOR
#: UPDATE` ahead of an INSERT that doesn't exist yet.
_LEDGER_LOCK_KEY = 0x4C454447_45_5F31  # "LEDG_1" in hex, arbitrary but stable


async def current_balance(session: AsyncSession) -> tuple[int, int]:
    """(balance_cents, reinvest_pool_cents) as of the latest entry."""
    stmt = select(LedgerEntry).order_by(LedgerEntry.created_at.desc()).limit(1)
    entry = (await session.execute(stmt)).scalar_one_or_none()
    if entry is None:
        return 0, 0
    return entry.balance_cents, entry.reinvest_pool_cents


async def append_ledger(
    session: AsyncSession,
    *,
    external_event_id: str,
    kind: str,
    amount_cents: int,
    reinvest_delta_cents: int = 0,
    product_id: str | None = None,
    memo: str | None = None,
    currency: str = "USD",
) -> bool:
    """Append one immutable money event. Idempotent on the provider's event id.

    Returns False when the event was already recorded, so a replayed Stripe webhook
    can never double-count revenue.

    ``balance_cents``/``reinvest_pool_cents`` are a running total derived by reading
    the latest row and adding this event's delta -- there is no separate counter row
    to ``SELECT ... FOR UPDATE``. Two webhook deliveries handled concurrently (the
    API and the scheduler share one event loop, so this is a real interleaving, not
    just a multi-process race) would otherwise both read the same stale balance and
    both commit from it, silently losing one event's contribution forever. A
    Postgres transaction-scoped advisory lock serializes the read-then-write: the
    second caller blocks here until the first commits (releasing the lock
    automatically) and only then reads the now-correct balance.
    """
    await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _LEDGER_LOCK_KEY})
    balance, pool = await current_balance(session)
    new_balance = balance + amount_cents
    new_pool = pool + reinvest_delta_cents
    if new_balance < 0:
        raise ValueError(
            f"refusing ledger entry: balance would become {new_balance} cents (negative)"
        )
    if new_pool < 0:
        raise ValueError(f"refusing ledger entry: reinvest pool would become {new_pool} cents")

    stmt = (
        pg_insert(LedgerEntry)
        .values(
            external_event_id=external_event_id,
            kind=kind,
            amount_cents=amount_cents,
            balance_cents=new_balance,
            reinvest_pool_cents=new_pool,
            currency=currency,
            product_id=product_id,
            memo=memo,
        )
        .on_conflict_do_nothing(index_elements=[LedgerEntry.external_event_id])
        .returning(LedgerEntry.id)
    )
    return (await session.execute(stmt)).scalar_one_or_none() is not None


# --------------------------------------------------------------------- interrupts


async def open_interrupt(
    session: AsyncSession,
    *,
    reason: InterruptReason,
    subject: str,
    detail: str,
    task_id: str | None = None,
    product_id: str | None = None,
) -> str | None:
    """Raise a human escalation. Duplicate open subjects collapse into one.

    `index_where` targets the partial unique index (`orm.py::Interrupt`,
    scoped to status='open') specifically -- without it, Postgres cannot match
    this INSERT's conflict target to a *partial* index at all, and the
    statement would fail with "there is no unique or exclusion constraint
    matching the ON CONFLICT specification" rather than silently doing the
    wrong thing, but it still has to be told explicitly.
    """
    stmt = (
        pg_insert(Interrupt)
        .values(
            reason=reason.value,
            subject=subject[:200],
            detail=detail,
            task_id=task_id,
            product_id=product_id,
            status="open",
        )
        .on_conflict_do_nothing(
            index_elements=[Interrupt.reason, Interrupt.subject, Interrupt.status],
            index_where=text("status = 'open'"),
        )
        .returning(Interrupt.id)
    )
    row = (await session.execute(stmt)).scalar_one_or_none()
    return str(row) if row is not None else None


async def undelivered_interrupts(session: AsyncSession) -> list[Interrupt]:
    stmt = select(Interrupt).where(Interrupt.status == "open", Interrupt.delivered.is_(False))
    return list((await session.execute(stmt)).scalars().all())


async def count_open_interrupts(session: AsyncSession) -> int:
    stmt = select(func.count()).select_from(Interrupt).where(Interrupt.status == "open")
    return int((await session.execute(stmt)).scalar_one())


async def has_open_interrupt_for_product(session: AsyncSession, product_id: str) -> bool:
    """Whether a human is already being asked about this product -- checked
    before raising a new automatic escalation for it (e.g. a diagnosed
    lead-sourcing credential problem, see marketing/graph.py) so nothing piles
    a second one onto a pending decision, and a persistently broken credential
    does not re-escalate on every subsequent harvest cycle."""
    stmt = (
        select(func.count())
        .select_from(Interrupt)
        .where(Interrupt.product_id == product_id, Interrupt.status == "open")
    )
    return int((await session.execute(stmt)).scalar_one()) > 0


async def list_open_interrupts(session: AsyncSession, limit: int = 10) -> list[Interrupt]:
    """Oldest first -- the ones a human has been waiting longest on are the ones
    `/status` (`hitl/commands.py`) should surface first if the list is capped."""
    stmt = (
        select(Interrupt)
        .where(Interrupt.status == "open")
        .order_by(Interrupt.created_at.asc())
        .limit(limit)
    )
    return list((await session.execute(stmt)).scalars().all())


async def find_open_interrupt_by_tag(session: AsyncSession, tag: str) -> Interrupt | None:
    """Same short-tag scheme as `hitl.interrupts.tag_for` (an interrupt id's first
    8 characters) -- used by `/issue` (`hitl/commands.py`) to look up one specific
    escalation's full detail, independent of `interrupts.py`'s own identical
    reply-matching lookup (`_find_open_by_tag`) so `commands.py` never has to
    import from the module that already imports it."""
    needle = tag.lower()
    stmt = select(Interrupt).where(Interrupt.status == "open")
    for row in (await session.execute(stmt)).scalars().all():
        if row.id.lower().startswith(needle):
            return row
    return None


async def stale_open_interrupts(session: AsyncSession, *, older_than: timedelta) -> list[Interrupt]:
    """Interrupts still waiting on a human past ``older_than`` since they were raised.

    Delivery already happened by the time something qualifies here (see
    ``undelivered_interrupts``) — this is for the case the human missed or forgot the
    original message, not the case the message never arrived.
    """
    cutoff = _now() - older_than
    stmt = (
        select(Interrupt)
        .where(Interrupt.status == "open", Interrupt.created_at < cutoff)
        .order_by(Interrupt.created_at.asc())
    )
    return list((await session.execute(stmt)).scalars().all())


async def mark_interrupt_delivered(session: AsyncSession, interrupt_id: str) -> None:
    await session.execute(
        update(Interrupt).where(Interrupt.id == interrupt_id).values(delivered=True)
    )


async def resolve_interrupt(session: AsyncSession, interrupt_id: str, response: str) -> None:
    await session.execute(
        update(Interrupt)
        .where(Interrupt.id == interrupt_id)
        .values(status="resolved", response=response, resolved_at=_now())
    )


# ---------------------------------------------------------------------- llm calls


# ----------------------------------------------------------- campaign approvals


async def get_or_create_campaign_approval(
    session: AsyncSession,
    *,
    product_id: str | None,
    task_id: str,
    lead_count: int,
    brief: str,
) -> CampaignApproval:
    """Idempotent on ``task_id``: the first call for a given send-task cohort creates
    the row and raises the interrupt (via the caller); every later call — this task
    retried on a subsequent tick, still pending — returns the same row unchanged."""
    stmt = (
        pg_insert(CampaignApproval)
        .values(product_id=product_id, task_id=task_id, lead_count=lead_count, brief=brief)
        .on_conflict_do_nothing(index_elements=[CampaignApproval.task_id])
    )
    await session.execute(stmt)
    existing = (
        await session.execute(select(CampaignApproval).where(CampaignApproval.task_id == task_id))
    ).scalar_one()
    return existing


async def set_campaign_approval_interrupt(
    session: AsyncSession, campaign_approval_id: str, interrupt_id: str
) -> None:
    await session.execute(
        update(CampaignApproval)
        .where(CampaignApproval.id == campaign_approval_id)
        .values(interrupt_id=interrupt_id)
    )


async def pending_campaign_approvals(session: AsyncSession) -> list[CampaignApproval]:
    stmt = select(CampaignApproval).where(
        CampaignApproval.status == "pending", CampaignApproval.interrupt_id.is_not(None)
    )
    return list((await session.execute(stmt)).scalars().all())


async def decide_campaign_approval(
    session: AsyncSession, campaign_approval_id: str, decision: str
) -> None:
    """``decision`` is always ``'approved'`` or ``'rejected'`` — the caller
    (`marketing.campaign.sync_decisions`) fails closed on anything ambiguous before
    reaching here, so this function itself trusts the value rather than re-validating
    it."""
    await session.execute(
        update(CampaignApproval)
        .where(CampaignApproval.id == campaign_approval_id)
        .values(status=decision, decided_at=_now())
    )


# --------------------------------------------------------------- channel experiments


async def create_channel_experiment(
    session: AsyncSession,
    *,
    product_id: str,
    channel: str,
    url: str,
    rationale: str,
    draft_content: str,
) -> str:
    row = ChannelExperiment(
        product_id=product_id,
        channel=channel[:40],
        url=url[:500],
        rationale=rationale[:2000],
        draft_content=draft_content[:6000],
    )
    session.add(row)
    await session.flush()
    return row.id


async def set_channel_experiment_interrupt(
    session: AsyncSession, experiment_id: str, interrupt_id: str
) -> None:
    await session.execute(
        update(ChannelExperiment)
        .where(ChannelExperiment.id == experiment_id)
        .values(interrupt_id=interrupt_id)
    )


async def pending_channel_experiments(session: AsyncSession) -> list[ChannelExperiment]:
    stmt = select(ChannelExperiment).where(
        ChannelExperiment.status == "proposed", ChannelExperiment.interrupt_id.is_not(None)
    )
    return list((await session.execute(stmt)).scalars().all())


async def decide_channel_experiment(
    session: AsyncSession, experiment_id: str, decision: str
) -> None:
    """``decision`` is always ``'approved'`` or ``'rejected'`` -- same fail-closed
    contract as `decide_campaign_approval`, enforced by the caller
    (`marketing.channel.sync_decisions`)."""
    await session.execute(
        update(ChannelExperiment)
        .where(ChannelExperiment.id == experiment_id)
        .values(status=decision, decided_at=_now())
    )


async def approved_channel_experiments_awaiting_conclusion(
    session: AsyncSession, *, older_than: timedelta
) -> list[ChannelExperiment]:
    """Approved experiments whose measurement window has elapsed -- what the
    periodic sweep in scheduler.py turns into a `MEASURE_CHANNEL_EXPERIMENT` task."""
    cutoff = _now() - older_than
    stmt = select(ChannelExperiment).where(
        ChannelExperiment.status == "approved", ChannelExperiment.decided_at < cutoff
    )
    return list((await session.execute(stmt)).scalars().all())


async def conclude_channel_experiment(
    session: AsyncSession, experiment_id: str, *, outcome: str
) -> None:
    await session.execute(
        update(ChannelExperiment)
        .where(ChannelExperiment.id == experiment_id)
        .values(status="concluded", outcome=outcome[:2000], concluded_at=_now())
    )


async def recent_channel_experiments(
    session: AsyncSession, product_id: str, limit: int = 10
) -> list[ChannelExperiment]:
    """What CHANNEL_SCOUT reads before proposing anything new, so it does not
    re-propose a channel already tried (or already awaiting a human decision)."""
    stmt = (
        select(ChannelExperiment)
        .where(ChannelExperiment.product_id == product_id)
        .order_by(ChannelExperiment.created_at.desc())
        .limit(limit)
    )
    return list((await session.execute(stmt)).scalars().all())


# ----------------------------------------------------------------- scout signals


async def record_scout_signal(
    session: AsyncSession,
    *,
    task_id: str,
    title: str,
    body: str,
    url: str,
    source: str,
    engagement: int = 0,
) -> str:
    row = ScoutSignal(
        task_id=task_id,
        title=title[:300],
        body=body[:4000],
        url=url[:500],
        source=source[:60],
        engagement=max(0, engagement),
    )
    session.add(row)
    await session.flush()
    return row.id


async def drain_scout_signals(session: AsyncSession, task_id: str) -> list[ScoutSignal]:
    """Read back every signal DISCOVERY_SCOUT submitted for this task, and delete
    them -- this table is a one-shot hand-off buffer, not a durable record (see
    `state/orm.py::ScoutSignal`)."""
    stmt = select(ScoutSignal).where(ScoutSignal.task_id == task_id)
    rows = list((await session.execute(stmt)).scalars().all())
    if rows:
        await session.execute(delete(ScoutSignal).where(ScoutSignal.task_id == task_id))
    return rows


# ---------------------------------------------------------------- data integrity


async def orphaned_approved_ideas(session: AsyncSession) -> list[Idea]:
    """Every idea marked 'approved' should have at least one product --
    Compliance always creates one in the same pass an idea is approved. An
    approved idea with no product at all means that guarantee broke somewhere.

    Deliberately the mirror image of `orchestrator.py::build_snapshot`'s
    `approved_ideas_waiting` query: that one INNER JOINs and filters on product
    status, which silently drops (undercounts, never flags) any approved idea
    with no matching product row at all. This checks the assumption the other
    query just trusts.
    """
    stmt = (
        select(Idea)
        .outerjoin(Product, Product.idea_id == Idea.id)
        .where(Idea.status == "approved", Product.id.is_(None))
    )
    return list((await session.execute(stmt)).scalars().all())


# --------------------------------------------------------------- entity verdicts


async def get_entity_verdict(session: AsyncSession, email_domain: str) -> EntityVerdictRow | None:
    stmt = select(EntityVerdictRow).where(EntityVerdictRow.email_domain == email_domain)
    return (await session.execute(stmt)).scalar_one_or_none()


async def store_entity_verdict(
    session: AsyncSession,
    *,
    email_domain: str,
    entity_type: str,
    confidence: float,
    evidence: str,
    task_id: str | None = None,
) -> EntityVerdictRow:
    """Idempotent on ``email_domain``: concurrent resolution of the same never-before
    -seen domain (two leads from the same company in one harvest) keeps whichever
    verdict landed first rather than raising or overwriting."""
    stmt = (
        pg_insert(EntityVerdictRow)
        .values(
            email_domain=email_domain,
            entity_type=entity_type,
            confidence=confidence,
            evidence=evidence[:1500],
            task_id=task_id,
        )
        .on_conflict_do_nothing(index_elements=[EntityVerdictRow.email_domain])
    )
    await session.execute(stmt)
    existing = await get_entity_verdict(session, email_domain)
    assert existing is not None
    return existing


# ------------------------------------------------------------ deliverability


async def outreach_log_by_id(session: AsyncSession, outreach_log_id: str) -> OutreachLog | None:
    return await session.get(OutreachLog, outreach_log_id)


async def record_bounce(session: AsyncSession, outreach_log_id: str) -> None:
    await session.execute(
        update(OutreachLog).where(OutreachLog.id == outreach_log_id).values(bounced_at=_now())
    )


async def record_complaint(session: AsyncSession, outreach_log_id: str) -> None:
    await session.execute(
        update(OutreachLog).where(OutreachLog.id == outreach_log_id).values(complained_at=_now())
    )


async def deliverability_counts(
    session: AsyncSession, *, window_days: int = 30
) -> tuple[int, int, int]:
    """(sent, complained, bounced) over the trailing window. The denominator is
    every send attempt regardless of later outcome — a bounce or complaint is
    still a send."""
    since = _now() - timedelta(days=window_days)
    sent_stmt = (
        select(func.count())
        .select_from(OutreachLog)
        .where(OutreachLog.status == "sent", OutreachLog.sent_at >= since)
    )
    complained_stmt = (
        select(func.count())
        .select_from(OutreachLog)
        .where(OutreachLog.complained_at.is_not(None), OutreachLog.sent_at >= since)
    )
    bounced_stmt = (
        select(func.count())
        .select_from(OutreachLog)
        .where(OutreachLog.bounced_at.is_not(None), OutreachLog.sent_at >= since)
    )
    sent = int((await session.execute(sent_stmt)).scalar_one())
    complained = int((await session.execute(complained_stmt)).scalar_one())
    bounced = int((await session.execute(bounced_stmt)).scalar_one())
    return sent, complained, bounced


async def active_marketing_freeze(session: AsyncSession) -> MarketingFreeze | None:
    stmt = select(MarketingFreeze).where(MarketingFreeze.status == "active")
    return (await session.execute(stmt)).scalar_one_or_none()


async def create_marketing_freeze(
    session: AsyncSession, *, reason: str, complaint_rate: float
) -> MarketingFreeze | None:
    """Idempotent: the partial-unique index on ``status='active'`` means a second
    trip while one is already active is a no-op, not a duplicate freeze.

    ``index_where`` is required, not optional, here: Postgres only matches an
    ``ON CONFLICT`` target against a partial index when the predicate is repeated
    verbatim. Without it, ``index_elements=[status]`` alone matches nothing (this
    table has no plain unique constraint on ``status``, only the partial one) and
    Postgres raises ``InvalidColumnReferenceError`` instead of silently ignoring
    the conflict.

    ``index_where`` must be a literal ``text()`` clause, not an ORM comparison --
    the latter compiles to a bound parameter, which Postgres's ON CONFLICT
    inference never matches against the index's own literal predicate (a ``Param``
    node cannot structurally equal a ``Const`` node), so every call raised
    instead of no-opping. See `create_ab_test` for the same bug, caught there
    first by a test that actually exercised the conflict path.
    """
    stmt = (
        pg_insert(MarketingFreeze)
        .values(reason=reason[:4000], complaint_rate=complaint_rate, status="active")
        .on_conflict_do_nothing(
            index_elements=[MarketingFreeze.status],
            index_where=text("status = 'active'"),
        )
        .returning(MarketingFreeze.id)
    )
    row = (await session.execute(stmt)).scalar_one_or_none()
    if row is None:
        return None
    return await session.get(MarketingFreeze, row)


async def set_marketing_freeze_interrupt(
    session: AsyncSession, freeze_id: str, interrupt_id: str
) -> None:
    await session.execute(
        update(MarketingFreeze)
        .where(MarketingFreeze.id == freeze_id)
        .values(interrupt_id=interrupt_id)
    )


async def clear_marketing_freeze(session: AsyncSession, freeze_id: str) -> None:
    await session.execute(
        update(MarketingFreeze)
        .where(MarketingFreeze.id == freeze_id)
        .values(status="cleared", cleared_at=_now())
    )


async def record_llm_call(
    session: AsyncSession,
    *,
    provider: str,
    model: str,
    task_type: str,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
    latency_ms: int = 0,
    outcome: str = "ok",
    engine: str | None = None,
    task_id: str | None = None,
    cost_usd: float = 0.0,
    tunable_parameter_id: str | None = None,
) -> None:
    session.add(
        LLMCall(
            provider=provider,
            model=model,
            task_type=task_type,
            engine=engine,
            task_id=task_id,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read_tokens,
            cache_write_tokens=cache_write_tokens,
            latency_ms=latency_ms,
            outcome=outcome,
            cost_usd=cost_usd,
            tunable_parameter_id=tunable_parameter_id,
        )
    )
