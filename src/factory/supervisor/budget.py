"""Spend authorisation.

Only the reinvestment pool is spendable — 50% of net revenue, per the constitution.
The remaining balance is never touched by an engine.

The never-negative rule is enforced three times over: here, in
``repository.append_ledger``, and by a database CHECK constraint. Redundant on
purpose, because the failure mode is spending money the factory does not have.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from factory.logging import get_logger
from factory.state import repository

log = get_logger(__name__)


class InsufficientFunds(RuntimeError):
    pass


async def available_cents(session: AsyncSession) -> int:
    _, pool = await repository.current_balance(session)
    return pool


async def get_spend_limit(session: AsyncSession) -> int:
    """What the Orchestrator reads before considering any paid action."""
    return await available_cents(session)


async def can_afford(session: AsyncSession, amount_cents: int) -> bool:
    if amount_cents <= 0:
        return True
    return await available_cents(session) >= amount_cents


async def authorize(
    session: AsyncSession,
    *,
    amount_cents: int,
    memo: str,
    external_event_id: str,
    product_id: str | None = None,
) -> None:
    """Debit the reinvestment pool, or refuse.

    Raises rather than clamping: an engine that asked to spend more than exists has
    a bug, and silently spending less would hide it.
    """
    if amount_cents <= 0:
        raise ValueError("spend amount must be positive")
    pool = await available_cents(session)
    if pool < amount_cents:
        raise InsufficientFunds(f"requested {amount_cents} cents, reinvestment pool holds {pool}")
    await repository.append_ledger(
        session,
        external_event_id=external_event_id,
        kind="spend",
        amount_cents=-amount_cents,
        reinvest_delta_cents=-amount_cents,
        product_id=product_id,
        memo=memo,
    )
    log.info(
        "budget.authorized",
        amount_cents=amount_cents,
        memo=memo,
        remaining_cents=pool - amount_cents,
    )
