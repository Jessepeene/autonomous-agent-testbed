"""Finance Engine runner.

Reconciles the ledger and pushes the resulting spend limit outward to the virtual
card, so the Orchestrator's view of available funds and the card's actual ceiling
cannot drift apart.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from factory.engines.finance import anomaly, cards
from factory.hitl import interrupts
from factory.logging import get_logger
from factory.settings import EngineName
from factory.state import repository
from factory.state.orm import Task
from factory.state.schemas import InterruptReason, TaskKind
from factory.supervisor import budget
from factory.supervisor.context import FactoryContext

log = get_logger(__name__)

#: One extra day beyond `anomaly.MIN_DAYS_FOR_BASELINE`'s window so "today" is
#: never itself counted as part of its own trailing baseline.
_TRAILING_WINDOW_DAYS = 14
#: The two real ledger kinds this factory ever writes (`webhooks.py`) --
#: revenue and refunds are the two figures a founder would actually notice a
#: strange swing in; balance is a derived running total of both, not an
#: independent signal worth checking a third time.
_WATCHED_LEDGER_KINDS = ("revenue", "refund")


async def _check_for_ledger_anomalies(session: AsyncSession) -> None:
    """Deterministic outlier check, not an LLM judgement -- see
    `anomaly.py`'s own module docstring for why this engine stays model-free
    even for its Tier 0 "does this look right" instinct."""
    now = datetime.now(UTC)
    today = now.date()
    since = now - timedelta(days=_TRAILING_WINDOW_DAYS + 1)
    for kind in _WATCHED_LEDGER_KINDS:
        rows = await repository.ledger_amounts_by_kind(session, kind=kind, since=since)
        totals = anomaly.daily_totals(rows)
        today_cents = totals.pop(today, 0)
        reason = anomaly.detect_anomaly(list(totals.values()), today_cents)
        if reason is None:
            continue
        log.warning("finance.ledger_anomaly", kind=kind, reason=reason)
        await interrupts.request(
            session,
            reason=InterruptReason.DATA_INTEGRITY,
            subject=f"Unusual {kind} today ({today.isoformat()})",
            detail=(
                f"Today's {kind} total looks statistically unusual compared to the "
                f"trailing {_TRAILING_WINDOW_DAYS}-day pattern: {reason}.\n\n"
                "This may be entirely legitimate (a real spike in sales, a large "
                "one-off refund) -- it is flagged for visibility, not because "
                "anything is known to be wrong."
            ),
            headline="💰 Unusual ledger activity",
            reply_hint="No reply needed — informational only, but worth a look when you have time.",
            needs_reply=False,
        )


async def run(ctx: FactoryContext, session: AsyncSession, task: Task) -> str:
    if TaskKind(task.kind) is TaskKind.ANSWER_QUESTION:
        # A question from another department, arriving as ordinary work.
        from factory.answering import answer_question_task

        return await answer_question_task(ctx, session, task)

    kind = TaskKind(task.kind)
    if kind is not TaskKind.RECONCILE_REVENUE:
        return f"finance: unsupported task kind {task.kind}"

    await _check_for_ledger_anomalies(session)

    balance, pool = await repository.current_balance(session)
    result = await cards.sync_spend_limit(
        pool_cents=pool,
        card_id=ctx.settings.stripe_issuing_card_id,
        settings=ctx.settings,
        dry_run=ctx.dry_run,
    )

    log.info(
        "finance.reconciled",
        balance_cents=balance,
        reinvest_pool_cents=pool,
        card_synced=result.ok,
    )
    return (
        f"finance: balance {balance}c, reinvestment pool {pool}c, "
        f"card {'synced' if result.ok else 'not synced'} ({result.detail})"
    )


async def schedule_reconciliation(session: AsyncSession, bucket: str) -> str | None:
    """Queue one reconciliation per window; called after revenue lands."""
    return await repository.enqueue_task(
        session,
        kind=TaskKind.RECONCILE_REVENUE,
        engine=EngineName.FINANCE,
        idempotency_key=f"reconcile:{bucket}",
        payload={},
        priority=10,  # money first
    )


async def spend_limit(session: AsyncSession) -> int:
    return await budget.get_spend_limit(session)
