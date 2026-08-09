"""Stripe webhook ingestion.

Two properties matter more than anything else here:

* **Signatures are verified.** An unsigned "payment succeeded" callback is an
  unauthenticated instruction to raise the spending limit.
* **Processing is idempotent.** Stripe retries deliveries, so the ledger keys on the
  Stripe event id and a replay is a no-op rather than double-counted revenue.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import stripe
from sqlalchemy.ext.asyncio import AsyncSession

from factory.engines.finance.reinvest import claw_back, split_revenue
from factory.logging import get_logger
from factory.settings import Settings, get_settings
from factory.state import repository

log = get_logger(__name__)

REVENUE_EVENTS = frozenset(
    {
        "checkout.session.completed",
        "invoice.paid",
        "invoice.payment_succeeded",
        "payment_intent.succeeded",
    }
)
REFUND_EVENTS = frozenset({"charge.refunded", "charge.dispute.created"})


class SignatureInvalid(ValueError):
    """The payload did not come from Stripe."""


@dataclass(slots=True)
class WebhookOutcome:
    handled: bool
    kind: str
    amount_cents: int = 0
    detail: str = ""


def verify(payload: bytes, signature: str, settings: Settings | None = None) -> dict[str, Any]:
    settings = settings or get_settings()
    secret = settings.secret("STRIPE_WEBHOOK_SECRET")
    if not secret:
        raise SignatureInvalid("STRIPE_WEBHOOK_SECRET is not configured")
    try:
        event = stripe.Webhook.construct_event(payload, signature, secret)  # type: ignore[no-untyped-call]
    except (ValueError, stripe.SignatureVerificationError) as exc:
        raise SignatureInvalid(str(exc)) from exc
    # NOT dict(event): StripeObject only implements __getitem__(str), not the
    # mapping/iterable protocol (no keys()/__iter__). dict() falls back to Python's
    # legacy sequence-iteration protocol, calling event[0], event[1], ... which
    # raises KeyError (not the IndexError that protocol expects to signal the end)
    # the moment it hits the backing dict's string keys -- crashing on every real
    # webhook delivery. to_dict() is the SDK's own recursive conversion, also
    # turning nested objects like data.object into plain dicts, which every
    # downstream .get() call here already assumes.
    return event.to_dict()  # type: ignore[no-any-return]


def _amount_cents(event: dict[str, Any]) -> int:
    obj = event.get("data", {}).get("object", {})
    for field in ("amount_total", "amount_paid", "amount_received", "amount_refunded", "amount"):
        value = obj.get(field)
        if isinstance(value, int):
            return value
    return 0


def _product_id(event: dict[str, Any]) -> str | None:
    """The factory never creates the Checkout Session — each generated product does,
    against the one shared platform Stripe account — so `client_reference_id` (set by
    the codegen contract, see `scaffold/generator.py`) is the only link back to a
    specific product row. A product deployed before that contract existed, or an event
    type that carries neither field, yields `None`: routine, not an error."""
    obj = event.get("data", {}).get("object", {})
    reference = obj.get("client_reference_id")
    if isinstance(reference, str) and reference:
        return reference
    metadata = obj.get("metadata") or {}
    product_id = metadata.get("product_id") if isinstance(metadata, dict) else None
    return product_id if isinstance(product_id, str) and product_id else None


async def process_event(
    session: AsyncSession, event: dict[str, Any], *, settings: Settings | None = None
) -> WebhookOutcome:
    settings = settings or get_settings()
    event_id = str(event.get("id") or "")
    event_type = str(event.get("type") or "")
    if not event_id:
        return WebhookOutcome(False, event_type, detail="event carried no id")

    amount = _amount_cents(event)
    product_id = _product_id(event)

    if event_type in REVENUE_EVENTS:
        if amount <= 0:
            return WebhookOutcome(False, event_type, detail="zero-value revenue event")
        split = split_revenue(amount)
        recorded = await repository.append_ledger(
            session,
            external_event_id=event_id,
            kind="revenue",
            amount_cents=amount,
            reinvest_delta_cents=split.reinvest_cents,
            product_id=product_id,
            memo=f"{event_type}: reinvest {split.reinvest_cents} of {amount}",
        )
        if not recorded:
            return WebhookOutcome(False, event_type, amount, "duplicate event, already recorded")
        if product_id is None:
            log.info("finance.unattributed_revenue", stripe_event=event_type, amount_cents=amount)
        log.info(
            "finance.revenue",
            stripe_event=event_type,
            amount_cents=amount,
            reinvest_cents=split.reinvest_cents,
            product_id=product_id,
        )
        return WebhookOutcome(True, event_type, amount, "revenue recorded")

    if event_type in REFUND_EVENTS:
        if amount <= 0:
            return WebhookOutcome(False, event_type, detail="zero-value refund event")
        _, pool = await repository.current_balance(session)
        split = claw_back(amount, pool)
        recorded = await repository.append_ledger(
            session,
            external_event_id=event_id,
            kind="refund",
            amount_cents=-amount,
            reinvest_delta_cents=split.reinvest_cents,
            product_id=product_id,
            memo=f"{event_type}: clawed {abs(split.reinvest_cents)} from the pool",
        )
        if not recorded:
            return WebhookOutcome(False, event_type, amount, "duplicate event, already recorded")
        log.info("finance.refund", stripe_event=event_type, amount_cents=amount)
        return WebhookOutcome(True, event_type, amount, "refund recorded")

    return WebhookOutcome(False, event_type, detail="event type not handled")
