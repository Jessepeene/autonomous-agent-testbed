"""Operational snapshot.

One query set that answers "is the factory healthy and doing anything", used by both
the /metrics endpoint and the maintenance agent.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from factory.observability.costs import total_usd
from factory.state.orm import Idea, Interrupt, Lead, OutreachLog, Product, Task


@dataclass(slots=True)
class FactoryMetrics:
    tasks_by_status: dict[str, int] = field(default_factory=dict)
    products_by_status: dict[str, int] = field(default_factory=dict)
    ideas_by_status: dict[str, int] = field(default_factory=dict)
    leads_total: int = 0
    outreach_sent: int = 0
    outreach_suppressed: int = 0
    open_interrupts: int = 0
    balance_cents: int = 0
    reinvest_pool_cents: int = 0
    llm_spend_usd_24h: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


async def _counts(session: AsyncSession, column: Any, model: Any) -> dict[str, int]:
    stmt = select(column, func.count()).select_from(model).group_by(column)
    return {str(key): int(count) for key, count in (await session.execute(stmt)).all()}


async def collect(session: AsyncSession) -> FactoryMetrics:
    from factory.state import repository

    balance, pool = await repository.current_balance(session)

    sent = int(
        (
            await session.execute(
                select(func.count()).select_from(OutreachLog).where(OutreachLog.status == "sent")
            )
        ).scalar_one()
    )
    suppressed = int(
        (
            await session.execute(
                select(func.count()).select_from(OutreachLog).where(OutreachLog.status != "sent")
            )
        ).scalar_one()
    )

    return FactoryMetrics(
        tasks_by_status=await _counts(session, Task.status, Task),
        products_by_status=await _counts(session, Product.status, Product),
        ideas_by_status=await _counts(session, Idea.status, Idea),
        leads_total=int(
            (await session.execute(select(func.count()).select_from(Lead))).scalar_one()
        ),
        outreach_sent=sent,
        outreach_suppressed=suppressed,
        open_interrupts=int(
            (
                await session.execute(
                    select(func.count()).select_from(Interrupt).where(Interrupt.status == "open")
                )
            ).scalar_one()
        ),
        balance_cents=balance,
        reinvest_pool_cents=pool,
        llm_spend_usd_24h=await total_usd(session),
    )
