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
from datetime import UTC, datetime, timedelta

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


#: A rail, not a tuned value. Nothing measured produced 120, and nothing should read
#: it as "how long a build takes": it is only "do not spin". The real release is the
#: holder finishing, and the real ceiling is the lock's own TTL, which
#: `purge_expired_locks` enforces within the hour -- so this can never wait forever on
#: a lock nobody still holds, whatever number is here.
LOCK_RETRY_SECONDS = 120


async def defer_for_lock(
    session: AsyncSession, *, task_id: str, product_id: str, engine: EngineName
) -> str:
    """Put work back on the queue because somebody else holds the product.

    Losing a lock race is not finishing. Every handler used to `return` a note here,
    and a runner that returns is completed by `_run_engine` -- so a build, patch or
    deploy that never ran was recorded as done, the product stayed in whatever status
    it was mid-transition to, and nothing retried because nothing knew anything had
    been skipped. The note said "locked by another worker" and went to a log line
    nobody reads.

    Deferred, not failed: the other worker holding the product is not this task's
    fault and must not consume its attempts. Deferred, not completed: the work still
    needs doing.

    **This path has never executed in production** -- 55 lock acquisitions, zero
    conflicts -- so read it as correctness by construction, not as a fix for anything
    observed. It is written now because the failure it prevents is silent: the first
    time two workers do race, the loser would be recorded as having finished.

    Deliberately *not* fixed at the obvious source. `claim_next_task` hands out the
    highest-priority ready task without consulting product locks, so in principle it
    can give two workers the same product -- and filtering claims by lock would stop
    that at the assignment step, the way a person checks the board before picking
    something up. With a single factory process there is nobody to race, so building
    that would be engineering for a problem this system does not have, and it would
    complicate the hottest query in the queue to do it. Revisit if a second worker
    ever runs; the evidence to justify it would be conflicts appearing here.
    """
    await repository.defer_task(
        session,
        task_id,
        not_before=datetime.now(UTC) + timedelta(seconds=LOCK_RETRY_SECONDS),
        reason=f"product {product_id} locked by another worker",
    )
    log.info("lock.deferred", product_id=product_id, task_id=task_id, engine=str(engine))
    return (
        f"{engine.value}: product {product_id} is locked by another worker; "
        f"deferred for {LOCK_RETRY_SECONDS}s"
    )


async def paid_spend_allowed(session: AsyncSession, product_id: str) -> bool:
    """False while Development holds this product."""
    paused = await repository.is_paid_spend_paused(session, product_id)
    if paused:
        log.info("marketing.paid_spend_paused", product_id=product_id, reason="product_locked")
    return not paused
