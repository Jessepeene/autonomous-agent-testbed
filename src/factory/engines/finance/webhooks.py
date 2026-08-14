"""Stripe webhook ingestion.

Two properties matter more than anything else here:

* **Signatures are verified.** An unsigned "payment succeeded" callback is an
  unauthenticated instruction to raise the spending limit.
* **Processing is idempotent.** Stripe retries deliveries, so the ledger keys on the
  Stripe event id and a replay is a no-op rather than double-counted revenue.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
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

#: Pure state notifications -- none of these carry a charge, an amount, or
#: anything representing money actually moving, and none of them may ever call
#: append_ledger (see state/repository.py's own section header and
#: test_subscription_lifecycle.py's enforced invariant). `.updated` fires for
#: many reasons (a plan change, cancel_at_period_end being toggled, Stripe
#: normalising something) and always refreshes state; only `.deleted` is
#: allowed to set churn_kind -- see `_churn_kind`'s own docstring for why
#: `.updated` alone is not enough evidence that a subscription has churned.
SUBSCRIPTION_LIFECYCLE_EVENTS = frozenset(
    {
        "customer.subscription.created",
        "customer.subscription.updated",
        "customer.subscription.deleted",
    }
)


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
        event = stripe.Webhook.construct_event(payload, signature, secret)
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


def _stripe_customer_id(event: dict[str, Any]) -> str | None:
    """`customer` is a plain top-level string id on every object relevant here
    (Checkout Session, Invoice, Charge, Subscription) when a customer is
    attached -- absent for a Checkout Session with no customer at all, which is
    routine, not an error."""
    obj = event.get("data", {}).get("object", {})
    value = obj.get("customer")
    return value if isinstance(value, str) and value else None


def _stripe_subscription_id(event: dict[str, Any]) -> str | None:
    """Same shape as `_stripe_customer_id` -- present on a Checkout Session
    (mode=subscription) and an Invoice generated from a subscription, absent
    for a one-time payment."""
    obj = event.get("data", {}).get("object", {})
    value = obj.get("subscription")
    return value if isinstance(value, str) and value else None


def _subscription_price_cents(event: dict[str, Any]) -> int:
    """Summed across every line item, since Stripe allows more than one Price
    per subscription (a base fee plus a metered add-on, for instance).

    UNVERIFIED AGAINST A LIVE STRIPE SUBSCRIPTION: this assumes the current
    `items.data[].price.unit_amount` shape. No generated product creates a
    Stripe Subscription yet (Phase 5 only wired up a one-time Checkout amount),
    so this has never run against a real payload -- confirm the exact shape
    against a real test-mode subscription before trusting this in production,
    same posture as `deploy.py`'s own noted Vercel-env uncertainty.
    """
    obj = event.get("data", {}).get("object", {})
    items = obj.get("items", {}).get("data", []) if isinstance(obj.get("items"), dict) else []
    total = 0
    for item in items:
        if not isinstance(item, dict):
            continue
        price = item.get("price")
        unit_amount = price.get("unit_amount") if isinstance(price, dict) else None
        quantity = item.get("quantity", 1)
        if isinstance(unit_amount, int) and isinstance(quantity, int):
            total += unit_amount * quantity
    return total


def _current_period_end(event: dict[str, Any]) -> datetime | None:
    """Checks both the legacy top-level field and the current per-item
    location -- Stripe has moved subscription period fields between API
    versions; see `_subscription_price_cents`'s same unverified-shape note."""
    obj = event.get("data", {}).get("object", {})
    top_level = obj.get("current_period_end")
    if isinstance(top_level, int):
        return datetime.fromtimestamp(top_level, tz=UTC)
    items = obj.get("items", {}).get("data", []) if isinstance(obj.get("items"), dict) else []
    if items and isinstance(items[0], dict):
        item_level = items[0].get("current_period_end")
        if isinstance(item_level, int):
            return datetime.fromtimestamp(item_level, tz=UTC)
    return None


def _churn_kind(event: dict[str, Any]) -> str:
    """Only ever read from `customer.subscription.deleted`'s own object --
    `cancellation_details.reason == "payment_failed"` means dunning exhausted
    every retry (involuntary). Anything else -- `cancellation_requested`,
    `duplicate`, or Stripe omitting the reason entirely -- is treated as
    voluntary, the conservative default when the evidence does not clearly say
    otherwise."""
    obj = event.get("data", {}).get("object", {})
    details = obj.get("cancellation_details")
    reason = details.get("reason") if isinstance(details, dict) else None
    return "involuntary" if reason == "payment_failed" else "voluntary"


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
    stripe_subscription_id = _stripe_subscription_id(event)
    stripe_customer_id = _stripe_customer_id(event)

    if event_type in REVENUE_EVENTS:
        # A Checkout session using an async payment method (SEPA/ACH debit)
        # completes with payment_status="unpaid" -- the money has not actually
        # arrived yet and may still fail (checkout.session.async_payment_failed).
        # Only book it once it actually settles: either a *different* event type
        # (async_payment_succeeded) that carries payment_status="paid", or this one
        # when the payment method was synchronous to begin with. "no_payment_required"
        # covers a $0 Checkout session (never reaches here; amount<=0 below).
        payment_status = obj.get("payment_status")
        if isinstance(payment_status, str) and payment_status not in (
            "paid",
            "no_payment_required",
        ):
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
                stripe_subscription_id=stripe_subscription_id,
                stripe_customer_id=stripe_customer_id,
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
        # invoice.paid also recovers a subscription from dunning, if it was in
        # it -- most paid invoices never failed at all, so close_dunning_by_invoice
        # returning False (nothing open to close) is the routine case.
        if event_type == "invoice.paid" and isinstance(obj.get("id"), str):
            await repository.close_dunning_by_invoice(
                session, stripe_invoice_id=obj["id"], reason="invoice.paid"
            )
        log.info(
            "finance.revenue",
            stripe_event=event_type,
            amount_cents=amount,
            reinvest_cents=split.reinvest_cents,
            product_id=product_id,
        )
        return WebhookOutcome(True, event_type, amount, "revenue recorded")

    if event_type in REFUND_EVENTS:
        refund_amount = (
            _refund_increment_cents(event) if event_type == "charge.refunded" else amount
        )
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
                stripe_subscription_id=stripe_subscription_id,
                stripe_customer_id=stripe_customer_id,
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
                stripe_subscription_id=stripe_subscription_id,
                stripe_customer_id=stripe_customer_id,
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

    if event_type in SUBSCRIPTION_LIFECYCLE_EVENTS:
        # Pure state notifications -- append_ledger is never called anywhere in
        # this branch. See the frozenset's own comment and
        # test_subscription_lifecycle.py's enforced invariant.
        stripe_subscription = obj.get("id")
        if not isinstance(stripe_subscription, str) or not stripe_subscription:
            return WebhookOutcome(False, event_type, detail="event carried no subscription id")

        if event_type == "customer.subscription.deleted":
            status = obj.get("status")
            await repository.mark_subscription_churned(
                session,
                stripe_subscription_id=stripe_subscription,
                status=status if isinstance(status, str) and status else "canceled",
                churn_kind=_churn_kind(event),
            )
            subscription = await repository.get_subscription_by_stripe_id(
                session, stripe_subscription
            )
            closed = 0
            if subscription is not None:
                closed = await repository.close_open_dunning_for_subscription(
                    session,
                    subscription_id=subscription.id,
                    reason="customer.subscription.deleted",
                )
            log.info(
                "finance.subscription_churned",
                stripe_subscription_id=stripe_subscription,
                churn_kind=_churn_kind(event),
                dunning_closed=closed,
            )
            return WebhookOutcome(True, event_type, detail="subscription churn recorded")

        # created and updated share the same upsert -- see upsert_subscription's
        # own docstring for why .updated refreshing price/status on every
        # delivery is deliberate, not incidental.
        if stripe_customer_id is None:
            return WebhookOutcome(
                False, event_type, detail="subscription event carried no customer id"
            )
        customer_id = await repository.upsert_customer(
            session, stripe_customer_id=stripe_customer_id, product_id=product_id
        )
        status = obj.get("status")
        await repository.upsert_subscription(
            session,
            stripe_subscription_id=stripe_subscription,
            customer_id=customer_id,
            product_id=product_id,
            status=status if isinstance(status, str) and status else "unknown",
            price_cents=_subscription_price_cents(event),
            cancel_at_period_end=bool(obj.get("cancel_at_period_end", False)),
            current_period_end=_current_period_end(event),
        )
        log.info(
            "finance.subscription_upserted",
            stripe_event=event_type,
            stripe_subscription_id=stripe_subscription,
            product_id=product_id,
        )
        return WebhookOutcome(True, event_type, detail="subscription state recorded")

    if event_type == "invoice.payment_failed":
        # Also a pure state notification -- append_ledger is never called here
        # either. Fires once per Stripe retry attempt for the same invoice;
        # open_or_bump_dunning upserts the same row rather than creating a new
        # one per delivery.
        if stripe_subscription_id is None:
            return WebhookOutcome(
                False, event_type, detail="failed invoice carried no subscription id"
            )
        subscription = await repository.get_subscription_by_stripe_id(
            session, stripe_subscription_id
        )
        if subscription is None:
            # The subscription.created event for this subscription has not been
            # processed yet -- a benign delivery-order race, same reasoning as
            # the ledger's own FK avoidance. Nothing to attach this dunning
            # sequence to yet; the next retry's payment_failed delivery (Stripe
            # will send one) will find the subscription row by then.
            return WebhookOutcome(
                False, event_type, detail="subscription not yet known, will retry"
            )
        invoice_id = obj.get("id")
        if not isinstance(invoice_id, str) or not invoice_id:
            return WebhookOutcome(False, event_type, detail="event carried no invoice id")
        await repository.open_or_bump_dunning(
            session, stripe_invoice_id=invoice_id, subscription_id=subscription.id
        )
        log.info(
            "finance.dunning_opened",
            stripe_subscription_id=stripe_subscription_id,
            stripe_invoice_id=invoice_id,
        )
        return WebhookOutcome(True, event_type, detail="dunning recorded")

    return WebhookOutcome(False, event_type, detail="event type not handled")
