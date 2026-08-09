"""Finance Engine runner.

Reconciles the ledger and pushes the resulting spend limit outward to the virtual
card, so the Orchestrator's view of available funds and the card's actual ceiling
cannot drift apart.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from factory.engines.finance import cards
from factory.logging import get_logger
from factory.settings import EngineName
from factory.state import repository
from factory.state.orm import Task
from factory.state.schemas import TaskKind
from factory.supervisor import budget
from factory.supervisor.context import FactoryContext

log = get_logger(__name__)


async def run(ctx: FactoryContext, session: AsyncSession, task: Task) -> str:
    kind = TaskKind(task.kind)
    if kind is not TaskKind.RECONCILE_REVENUE:
        return f"finance: unsupported task kind {task.kind}"

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
