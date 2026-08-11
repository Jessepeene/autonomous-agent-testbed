"""Virtual card limits.

The spending limit on the factory's virtual card is set to the reinvestment pool and
nothing more, so the card itself is a second, external ceiling on autonomous spend:
even a bug in the budget module cannot charge money the factory has not earned.

That claim depends on the spending-limit `interval` being ``all_time``, not
``monthly``: a *monthly* Stripe Issuing limit is a per-calendar-month counter, so
the full pool-derived amount becomes chargeable again every month regardless of
what was spent the month before -- an internal-budget-module bug (or simply no
internal debit ever being recorded, which is the case today: nothing in this
codebase yet calls `supervisor.budget.authorize()`) would let the card be charged
its full limit again and again, indefinitely. ``all_time`` makes Stripe track
cumulative spend against this card for its whole life, so the limit really is a
hard ceiling on total money ever charged to it -- independent of whether the
internal ledger's own spend-tracking is ever wired up.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import stripe

from factory.logging import get_logger
from factory.settings import Settings, get_settings

log = get_logger(__name__)


@dataclass(slots=True)
class CardSyncResult:
    ok: bool
    limit_cents: int
    detail: str


def _update_card(card_id: str, limit_cents: int, api_key: str) -> None:
    stripe.issuing.Card.modify(
        card_id,
        spending_controls={"spending_limits": [{"amount": limit_cents, "interval": "all_time"}]},
        api_key=api_key,
    )


async def sync_spend_limit(
    *,
    pool_cents: int,
    card_id: str | None,
    settings: Settings | None = None,
    dry_run: bool = True,
) -> CardSyncResult:
    settings = settings or get_settings()
    limit = max(pool_cents, 0)

    if dry_run:
        return CardSyncResult(True, limit, "dry run: card limit not changed")
    if not card_id:
        return CardSyncResult(False, limit, "no STRIPE_ISSUING_CARD_ID configured")
    api_key = settings.secret("STRIPE_API_KEY")
    if not api_key:
        return CardSyncResult(False, limit, "STRIPE_API_KEY is not configured")

    try:
        await asyncio.to_thread(_update_card, card_id, limit, api_key)
    except stripe.StripeError as exc:
        log.warning("finance.card_sync_failed", detail=str(exc)[:200])
        return CardSyncResult(False, limit, f"stripe error: {exc}")

    log.info("finance.card_limit_synced", limit_cents=limit)
    return CardSyncResult(True, limit, "card limit updated")
