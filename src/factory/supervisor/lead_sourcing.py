"""What to do about a product that does not have enough reach to learn from yet.

**Retire only ever happens on a human's explicit word.** Try again; try a different
channel; then ask -- never conclude on your own that the market has spoken when the
real fact is that this company has not yet reached enough of it to know.

That principle already existed, for exactly one trigger: a validation stuck at zero
leads (`supervisor/scheduler.py`'s stalled-validation sweep). It retries once, tries a
different distribution channel once, and only then raises `LEAD_SOURCING_STALLED` --
which a human resolves with REJECT (retire) or anything else (try again). Nothing in
that ladder ever calls `retire_product` itself.

Jesse, asked whether it was right to auto-retire a product whose validation sent too
few emails to read anything from: *"unless they have concrete evidence the product
will not work they won't retire it, right?"* He is right, and the codebase already
agreed with him for the zero-leads case. It disagreed with itself for the adjacent
one -- some leads, but fewer than `validation_min_sample_size` -- which is the same
underlying fact (not enough reach for a valid read) observed one week later, at the
end of the validation window instead of by an hourly sweep. That path called
`retire_product` directly.

So this is the shared mechanism, not a new one: the same three-rung ladder, callable
from wherever "not enough reach yet" is first noticed, so there is exactly one place
that knows retire is not an automatic response to insufficient data.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from factory.hitl import interrupts
from factory.logging import get_logger
from factory.settings import EngineName
from factory.state import repository
from factory.state.orm import Product, new_id
from factory.state.schemas import InterruptReason, TaskKind

log = get_logger(__name__)


async def pursue_or_escalate(session: AsyncSession, product: Product, *, context: str) -> str:
    """Try to get more reach for `product`; escalate only once that is exhausted.

    Guarded by an open interrupt first: if a human is already being asked, calling
    this again (because another inconclusive read came in while they had not yet
    replied) must not pursue another rung or raise a second, near-identical
    escalation -- `interrupts.request`'s own dedup is keyed on subject text, and the
    final rung deliberately appends a fresh id specifically so a *resolved* prior
    escalation never blocks a new one, which would otherwise also block this one.

    `context` is the one thing that varies by caller: what "not enough reach" means
    concretely for the specific way it was noticed (zero leads sourced at all, versus
    some leads but too few sends to read a rate). The ladder itself -- retry, try a
    channel, then ask -- does not vary with why it was triggered.
    """
    if await repository.has_open_interrupt_for_product(session, product.id):
        return "already_escalated"

    if not await repository.has_retried_lead_sourcing(session, product.id):
        # First strike: try once more, exactly like a person finding the internet
        # down would first check whether it is back up before bothering anyone.
        await repository.enqueue_task(
            session,
            kind=TaskKind.HARVEST_LEADS,
            engine=EngineName.MARKETING,
            idempotency_key=f"leads:validate:{product.id}:retry",
            payload={"product_id": product.id},
            product_id=product.id,
            priority=120,
        )
        return "retry_queued"

    if not await repository.has_attempted_channel_scout(session, product.id):
        # Second strike: the retry didn't help either, but that is not yet a reason
        # to ask a human -- try a different distribution channel first.
        await repository.enqueue_task(
            session,
            kind=TaskKind.CHANNEL_SCOUT_REVIEW,
            engine=EngineName.MARKETING,
            idempotency_key=f"channel-scout:stall:{product.id}",
            payload={"product_id": product.id},
            product_id=product.id,
            priority=100,
        )
        return "channel_scout_queued"

    # Third strike: a retry and an automatic channel-scout review both failed to
    # unstick this. Not something more waiting or another automatic channel can fix
    # on its own -- but still not evidence the idea is bad, only that this company's
    # current channels cannot reach this niche at volume. A fresh id, not
    # product.id[:8]: a resolved earlier escalation for this same product must never
    # block raising a new one (0017_interrupt_open_subject_partial.py).
    await interrupts.request(
        session,
        reason=InterruptReason.LEAD_SOURCING_STALLED,
        subject=f"{product.name[:80]} can't get enough reach ({new_id()[:8]})",
        detail=(
            f"{context}\n\n"
            "A retry and an automatic channel-scout review have both already run for "
            "this product. Retiring it now would be treating 'we could not reach "
            "enough people' as 'people do not want this' -- they are not the same "
            "fact, and only the second one is evidence about the idea."
        ),
        headline="\U0001f4e1 Can't get enough reach to test this",
        reply_hint=(
            "Reply anything to try sourcing leads again, or REJECT to retire this product."
        ),
        product_id=product.id,
    )
    return "escalated"
