"""All database access.

Engines never write SQL. They call these functions, which own the invariants:
idempotent enqueue, leased claims that survive a crash, the never-negative ledger,
and the minimisation boundary for leads.
"""

from __future__ import annotations

import hashlib
import secrets
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlalchemy import CursorResult, delete, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from factory.engines.compliance.minimize import minimize
from factory.logging import get_logger
from factory.settings import EngineName, Settings, get_settings
from factory.state.orm import (
    CampaignApproval,
    EntityVerdictRow,
    HealAttempt,
    Idea,
    Interrupt,
    Lead,
    LedgerEntry,
    LLMCall,
    MarketingFreeze,
    OutreachLog,
    Product,
    ProductLock,
    Task,
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


async def retire_product(session: AsyncSession, product_id: str) -> bool:
    """A human's explicit REJECT to a heal-exhaustion escalation. Terminal -- nothing
    else in this codebase ever moves a product out of ``retired``."""
    product = await session.get(Product, product_id)
    if product is None:
        return False
    product.status = ProductStatus.RETIRED.value
    await session.flush()
    return True


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
        payload={"product_id": product_id, "idea_id": product.idea_id, "healing": guidance[:6000]},
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
    session: AsyncSession, idea: ScoredIdea, *, score: float | None = None
) -> str | None:
    """Store a scored idea, de-duplicated by problem + niche fingerprint.

    ``score`` defaults to ``idea.score`` (the fixed historical weights) — pass it
    explicitly only when the caller has fetched tuned weights and wants
    ``idea.weighted_score(weights)`` stored instead.
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


async def leads_for_outreach(session: AsyncSession, limit: int = 25) -> list[Lead]:
    stmt = (
        select(Lead).where(Lead.lia_passed.is_(True)).order_by(Lead.created_at.asc()).limit(limit)
    )
    return list((await session.execute(stmt)).scalars().all())


async def record_outreach(
    session: AsyncSession,
    *,
    lead_id: str,
    subject: str,
    status: str,
    product_id: str | None = None,
    suppressed_reason: str | None = None,
    id: str | None = None,
) -> str:
    """``id`` may be pre-generated by the caller (see `marketing/outreach.py`) so the
    same id can be handed to the deliverability provider as a tracking variable
    *before* this row exists — its webhook needs something to correlate back to."""
    entry = OutreachLog(
        id=id or new_id(),
        lead_id=lead_id,
        product_id=product_id,
        subject=subject[:500],
        status=status,
        suppressed_reason=suppressed_reason,
        unsubscribe_token=secrets.token_urlsafe(32),
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
    """
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
    """Raise a human escalation. Duplicate open subjects collapse into one."""
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
            index_elements=[Interrupt.reason, Interrupt.subject, Interrupt.status]
        )
        .returning(Interrupt.id)
    )
    row = (await session.execute(stmt)).scalar_one_or_none()
    return str(row) if row is not None else None


async def undelivered_interrupts(session: AsyncSession) -> list[Interrupt]:
    stmt = select(Interrupt).where(Interrupt.status == "open", Interrupt.delivered.is_(False))
    return list((await session.execute(stmt)).scalars().all())


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


async def deliverability_counts(session: AsyncSession, *, window_days: int = 30) -> tuple[int, int]:
    """(sent, complained) over the trailing window. The denominator is every send
    attempt regardless of later outcome — a bounce or complaint is still a send."""
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
    sent = int((await session.execute(sent_stmt)).scalar_one())
    complained = int((await session.execute(complained_stmt)).scalar_one())
    return sent, complained


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
    """
    stmt = (
        pg_insert(MarketingFreeze)
        .values(reason=reason[:4000], complaint_rate=complaint_rate, status="active")
        .on_conflict_do_nothing(
            index_elements=[MarketingFreeze.status],
            index_where=(MarketingFreeze.status == "active"),
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
