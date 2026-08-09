"""Cross-engine mutual exclusion.

The rule this file exists for: **while Development is patching a live product,
Marketing must not spend money driving traffic to it.** Paying for clicks into a
broken checkout flow is worse than spending nothing, so the lock is checked before
any paid campaign — and organic outreach deliberately keeps running, because it
costs nothing and pausing it would idle the Marketing Engine for no benefit.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncSession

from factory.logging import get_logger
from factory.settings import EngineName
from factory.state import repository

log = get_logger(__name__)


class LockUnavailable(RuntimeError):
    """Another engine already holds this product."""


@asynccontextmanager
async def product_lock(
    session: AsyncSession,
    *,
    product_id: str,
    holder: EngineName,
    reason: str,
    ttl_seconds: int = 3600,
    blocks_paid_spend: bool = True,
) -> AsyncIterator[None]:
    """Hold a product for the duration of a build or patch.

    The lock carries a TTL so a crashed worker cannot freeze a product's marketing
    forever — the next ``purge_expired_locks`` sweep releases it.
    """
    acquired = await repository.acquire_product_lock(
        session,
        product_id=product_id,
        holder=holder,
        reason=reason,
        ttl_seconds=ttl_seconds,
        blocks_paid_spend=blocks_paid_spend,
    )
    if not acquired:
        raise LockUnavailable(f"product {product_id} is already locked")
    log.info("lock.acquired", product_id=product_id, holder=str(holder), reason=reason)
    try:
        yield
    finally:
        await repository.release_product_lock(session, product_id)
        log.info("lock.released", product_id=product_id, holder=str(holder))


async def paid_spend_allowed(session: AsyncSession, product_id: str) -> bool:
    """False while Development holds this product."""
    paused = await repository.is_paid_spend_paused(session, product_id)
    if paused:
        log.info("marketing.paid_spend_paused", product_id=product_id, reason="product_locked")
    return not paused
