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

#: Exactly one canonical event per money-movement flow. Stripe fires *several*
#: events for the same underlying payment -- `invoice.paid` and
#: `invoice.payment_succeeded` both fire for the same successful invoice payment,
#: and a one-time Checkout payment fires both `checkout.session.completed` and a
#: `payment_intent.succeeded` for its underlying PaymentIntent. Subscribing to more
#: than one member of the same flow silently double- (or triple-) books the same
#: sale; `external_event_id` dedup does not help, since each of these carries its
#: own distinct Stripe event id. `checkout.session.async_payment_succeeded` is
#: included alongside `checkout.session.completed` because they are *not*
#: duplicates of each other -- a Checkout session using an async payment method
#: (SEPA/ACH debit) completes with `payment_status="unpaid"` and only actually
#: settles later, signalled by this second event; see the `payment_status` gate in
#: `process_event` below.
REVENUE_EVENTS = frozenset(
    {
        "checkout.session.completed",
        "checkout.session.async_payment_succeeded",
        "invoice.paid",
    }
)
REFUND_EVENTS = frozenset({"charge.refunded", "charge.dispute.created"})
#: A previously-booked `charge.dispute.created` debit reversed because the dispute
#: was decided in the merchant's favour -- money that was never actually lost.
DISPUTE_WON_EVENTS = frozenset({"charge.dispute.closed"})


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


def _refund_increment_cents(event: dict[str, Any]) -> int:
    """`charge.refunded`'s top-level `amount_refunded` is the charge's *cumulative*
    total refunded to date, not this event's increment -- a charge refunded twice
    ($30 then the remaining $70) fires `amount_refunded=3000` then
    `amount_refunded=10000`; summing both would claw back $130 against a $100
    charge. The specific Refund that triggered this delivery is the newest entry in
    the embedded `refunds` list (Stripe orders it newest-first), and that object's
    own `amount` is the real, non-cumulative increment. Falls back to the
    (potentially wrong, but better than nothing) cumulative field only if Stripe
    ever omits the nested list.
    """
    obj = event.get("data", {}).get("object", {})
    refunds = obj.get("refunds")
    if isinstance(refunds, dict):
        data = refunds.get("data")
        if isinstance(data, list) and data:
            latest = data[0]
            if isinstance(latest, dict) and isinstance(latest.get("amount"), int):
                return int(latest["amount"])
    value = obj.get("amount_refunded")
    return int(value) if isinstance(value, int) else 0


def _currency(event: dict[str, Any]) -> str:
    """Stripe's ISO currency code is always lowercase; the ledger stores it
    uppercase to match the ISO 4217 convention `append_ledger`'s callers/readers
    otherwise assume. Defaults to USD only when the event genuinely carries none --
    not as a silent 1:1 FX assumption for a real non-USD sale."""
    obj = event.get("data", {}).get("object", {})
    value = obj.get("currency")
    return value.upper() if isinstance(value, str) and value else "USD"


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

    obj = event.get("data", {}).get("object", {})
    amount = _amount_cents(event)
    product_id = _product_id(event)

    if event_type in REVENUE_EVENTS:
        # A Checkout session using an async payment method (SEPA/ACH debit)
        # completes with payment_status="unpaid" -- the money has not actually
        # arrived yet and may still fail (checkout.session.async_payment_failed).
        # Only book it once it actually settles: either a *different* event type
        # (async_payment_succeeded) that carries payment_status="paid", or this one
        # when the payment method was synchronous to begin with. "no_payment_required"
        # covers a $0 Checkout session (never reaches here; amount<=0 below).
        payment_status = obj.get("payment_status")
        if isinstance(payment_status, str) and payment_status not in ("paid", "no_payment_required"):
            return WebhookOutcome(
                False, event_type, detail=f"payment not yet settled ({payment_status})"
            )
        if amount <= 0:
            return WebhookOutcome(False, event_type, detail="zero-value revenue event")
        split = split_revenue(amount)
        try:
            recorded = await repository.append_ledger(
                session,
                external_event_id=event_id,
                kind="revenue",
                amount_cents=amount,
                reinvest_delta_cents=split.reinvest_cents,
                product_id=product_id,
                memo=f"{event_type}: reinvest {split.reinvest_cents} of {amount}",
                currency=_currency(event),
            )
        except ValueError as exc:
            log.error(
                "finance.ledger_rejected",
                stripe_event=event_type,
                event_id=event_id,
                amount_cents=amount,
                detail=str(exc),
            )
            return WebhookOutcome(False, event_type, amount, f"ledger rejected: {exc}")
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
        refund_amount = _refund_increment_cents(event) if event_type == "charge.refunded" else amount
        if refund_amount <= 0:
            return WebhookOutcome(False, event_type, detail="zero-value refund event")
        _, pool = await repository.current_balance(session)
        split = claw_back(refund_amount, pool)
        try:
            recorded = await repository.append_ledger(
                session,
                external_event_id=event_id,
                kind="refund",
                amount_cents=-refund_amount,
                reinvest_delta_cents=split.reinvest_cents,
                product_id=product_id,
                memo=f"{event_type}: clawed {abs(split.reinvest_cents)} from the pool",
                currency=_currency(event),
            )
        except ValueError as exc:
            # A refund for a charge whose original payment was never recorded (e.g.
            # the endpoint was down/misconfigured when it succeeded) would otherwise
            # drive the balance negative, raise here, propagate as a 500, and after
            # Stripe's retries exhaust, get the whole endpoint auto-disabled --
            # silently stopping *all* future revenue recording, not just this event.
            # Logging and declining to record is the safe failure mode; this needs a
            # human to reconcile, but must never take the endpoint down.
            log.error(
                "finance.ledger_rejected",
                stripe_event=event_type,
                event_id=event_id,
                amount_cents=refund_amount,
                detail=str(exc),
            )
            return WebhookOutcome(False, event_type, refund_amount, f"ledger rejected: {exc}")
        if not recorded:
            return WebhookOutcome(
                False, event_type, refund_amount, "duplicate event, already recorded"
            )
        log.info("finance.refund", stripe_event=event_type, amount_cents=refund_amount)
        return WebhookOutcome(True, event_type, refund_amount, "refund recorded")

    if event_type in DISPUTE_WON_EVENTS:
        if obj.get("status") != "won":
            return WebhookOutcome(False, event_type, detail=f"dispute status: {obj.get('status')}")
        if amount <= 0:
            return WebhookOutcome(False, event_type, detail="zero-value dispute event")
        # Reverses the balance-side debit exactly (the original charge.dispute.created
        # subtracted this same `amount`). The pool-side split is *not* guaranteed
        # symmetric with whatever claw_back() actually removed at dispute-creation
        # time (that call clamps to the pool available then, which may have been
        # less than the full proportional share) -- re-splitting the returned amount
        # like fresh revenue is a deliberate, documented simplification: it always
        # keeps the balance correct, and at worst slightly over-credits the pool
        # (a bounded, rare, safe-direction error) rather than never crediting it at
        # all, which is the bug this fixes.
        split = split_revenue(amount)
        try:
            recorded = await repository.append_ledger(
                session,
                external_event_id=event_id,
                kind="revenue",
                amount_cents=amount,
                reinvest_delta_cents=split.reinvest_cents,
                product_id=product_id,
                memo=f"{event_type}: dispute won, reversing {amount}",
                currency=_currency(event),
            )
        except ValueError as exc:
            log.error(
                "finance.ledger_rejected",
                stripe_event=event_type,
                event_id=event_id,
                amount_cents=amount,
                detail=str(exc),
            )
            return WebhookOutcome(False, event_type, amount, f"ledger rejected: {exc}")
        if not recorded:
            return WebhookOutcome(False, event_type, amount, "duplicate event, already recorded")
        log.info("finance.dispute_won", stripe_event=event_type, amount_cents=amount)
        return WebhookOutcome(True, event_type, amount, "dispute reversal recorded")

    return WebhookOutcome(False, event_type, detail="event type not handled")
