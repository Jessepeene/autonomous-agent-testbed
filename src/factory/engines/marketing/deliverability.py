"""The spam-complaint circuit breaker: bounce/complaint tracking and the freeze.

Two independent halves:

* **Webhook ingestion** (`verify`, `process_event`) — Mailgun reports what happened
  to a message after `email.py` handed it off: delivered, bounced, complained.
  Correlated back to a specific `OutreachLog` row via the `v:outreach_log_id`
  user-variable set at send time (see `email.py::_build_payload`).
* **The circuit breaker** (`_check_and_trip`, `ensure_not_frozen`,
  `sync_freeze_clearance`) — every complaint event re-evaluates the trailing
  complaint rate; crossing the threshold freezes all outbound sending immediately,
  via a Telegram interrupt. Unlike the daily LLM spend cap, this never self-clears —
  a reputation-damaging event needs a human to actually look at it before sending
  resumes, so the only way out is an explicit ``RESUME`` reply.

Caveat worth stating plainly, the same way the Vercel env-injection contract is
flagged elsewhere in this codebase: the exact shape of Mailgun's webhook JSON body
implemented below (``event-data.event``, ``event-data.user-variables``, etc.) matches
Mailgun's long-documented "signed webhooks" format, but has not been verified against
a live payload from a real Mailgun account in this environment. The signature
verification scheme (HMAC-SHA256 of ``timestamp + token``, compared against
``signature.signature`` in the body) is independently confirmed against current
Mailgun documentation and is the part that matters most to get right. Before relying
on this in production, send a test webhook from the Mailgun dashboard (Sending →
Webhooks → pick an event → "Test webhook") and confirm `process_event` actually reads
the fields it expects — `_event_data` falls back to a flat top-level shape if
`event-data` is absent, specifically to reduce the blast radius of that uncertainty.
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from factory.hitl import interrupts
from factory.logging import get_logger
from factory.settings import Settings, get_settings
from factory.state import repository
from factory.state.orm import Interrupt
from factory.state.schemas import InterruptReason

log = get_logger(__name__)

#: How long to wait before re-checking a still-frozen state. Cheap (no LLM call, no
#: send attempt) — matches `campaign.RECHECK_SECONDS`'s reasoning exactly.
RECHECK_SECONDS = 3600

_COMPLAINT_EVENTS = frozenset({"complained"})
_BOUNCE_EVENTS = frozenset({"failed", "bounced"})

_SUBJECT = "Marketing frozen: spam complaint rate exceeded"


class SignatureInvalid(ValueError):
    """The payload did not come from Mailgun."""


class MarketingFrozen(RuntimeError):
    """Raised by `ensure_not_frozen` while the spam-complaint circuit breaker is
    tripped. Caught in `supervisor/graph.py::_run_engine`, which defers the task
    (the same mechanism as `campaign.CampaignApprovalPending`) rather than treating
    it as a failure — a freeze is a standing state, not an error this specific task
    caused."""


@dataclass(slots=True)
class WebhookOutcome:
    handled: bool
    event: str
    detail: str = ""


def verify(payload: dict[str, Any], settings: Settings | None = None) -> None:
    """Raises `SignatureInvalid` unless ``payload["signature"]`` was produced with
    our webhook signing key.

    Mailgun signs ``timestamp + token`` (concatenated, no separator) with
    HMAC-SHA256 keyed on the webhook signing key, and puts the result in the
    request *body* — ``signature.timestamp``, ``signature.token``,
    ``signature.signature`` — never in a header, unlike most webhook providers.
    """
    settings = settings or get_settings()
    key = settings.secret("MAILGUN_WEBHOOK_SIGNING_KEY")
    if not key:
        raise SignatureInvalid("MAILGUN_WEBHOOK_SIGNING_KEY is not configured")

    block = payload.get("signature")
    if not isinstance(block, dict):
        raise SignatureInvalid("payload carries no signature block")
    timestamp = str(block.get("timestamp") or "")
    token = str(block.get("token") or "")
    provided = str(block.get("signature") or "")
    if not (timestamp and token and provided):
        raise SignatureInvalid("signature block is incomplete")

    expected = hmac.new(
        key.encode("utf-8"), f"{timestamp}{token}".encode(), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(expected, provided):
        raise SignatureInvalid("signature does not match")


def _event_data(payload: dict[str, Any]) -> dict[str, Any]:
    """Mailgun's modern webhook format nests everything under ``event-data``; the
    legacy format was flat. Accept either shape defensively — see the module
    docstring's verification caveat."""
    nested = payload.get("event-data")
    return nested if isinstance(nested, dict) else payload


async def process_event(
    session: AsyncSession, payload: dict[str, Any], *, settings: Settings | None = None
) -> WebhookOutcome:
    settings = settings or get_settings()
    data = _event_data(payload)
    event = str(data.get("event") or "")

    variables = data.get("user-variables")
    outreach_log_id = variables.get("outreach_log_id") if isinstance(variables, dict) else None
    if not isinstance(outreach_log_id, str) or not outreach_log_id:
        return WebhookOutcome(False, event, detail="no outreach_log_id in user-variables")

    row = await repository.outreach_log_by_id(session, outreach_log_id)
    if row is None:
        return WebhookOutcome(False, event, detail="unknown outreach_log_id")

    if event in _BOUNCE_EVENTS:
        await repository.record_bounce(session, outreach_log_id)
        await _check_and_trip(session, settings)
        return WebhookOutcome(True, event, detail="bounce recorded")

    if event in _COMPLAINT_EVENTS:
        await repository.record_complaint(session, outreach_log_id)
        await _check_and_trip(session, settings)
        return WebhookOutcome(True, event, detail="complaint recorded")

    return WebhookOutcome(False, event, detail="event type not tracked")


async def _check_and_trip(session: AsyncSession, settings: Settings) -> None:
    """Re-evaluated on every bounce or complaint event, not on a schedule — the
    whole point of a circuit breaker is that it reacts the moment a threshold is
    crossed, not on the next maintenance tick.

    Two independent thresholds, either one trips the same freeze: `bounced_at` was
    previously recorded on every bounce webhook but never read anywhere -- a
    sustained high bounce rate (dead/invalid addresses, a burned sending domain) is
    exactly the kind of reputation damage this circuit breaker exists to catch, and
    it had no backstop at all.
    """
    sent, complained, bounced = await repository.deliverability_counts(session)
    if sent < settings.deliverability_min_sample_size:
        return

    complaint_rate = complained / sent
    bounce_rate = bounced / sent
    if (
        complaint_rate < settings.spam_complaint_rate_threshold
        and bounce_rate < settings.bounce_rate_threshold
    ):
        return

    if complaint_rate >= settings.spam_complaint_rate_threshold:
        reason = (
            f"Spam complaint rate {complaint_rate:.4%} ({complained}/{sent} over the "
            f"trailing window) is at or above the "
            f"{settings.spam_complaint_rate_threshold:.4%} threshold major mailbox "
            "providers use as their own blocking bar."
        )
    else:
        reason = (
            f"Bounce rate {bounce_rate:.4%} ({bounced}/{sent} over the trailing "
            f"window) is at or above the {settings.bounce_rate_threshold:.4%} "
            "threshold major mailbox providers use as their own sender-reputation bar."
        )
    freeze = await repository.create_marketing_freeze(
        session, reason=reason, complaint_rate=complaint_rate
    )
    if freeze is None:
        return  # already tripped; the partial-unique index made this a no-op

    interrupt_id = await interrupts.request(
        session,
        reason=InterruptReason.DELIVERABILITY_FROZEN,
        subject=_SUBJECT,
        detail=(
            f"{reason}\n\nAll outbound email is paused — SEND_OUTREACH tasks will "
            "keep deferring until this is cleared. This does not self-resolve: "
            "review the recent campaign content and recipient list, then reply "
            "RESUME to this message. Any other reply leaves sending frozen."
        ),
    )
    if interrupt_id is not None:
        await repository.set_marketing_freeze_interrupt(session, freeze.id, interrupt_id)
    log.warning(
        "deliverability.frozen",
        complaint_rate=complaint_rate,
        bounce_rate=bounce_rate,
        sent=sent,
        complained=complained,
        bounced=bounced,
    )


async def ensure_not_frozen(session: AsyncSession) -> None:
    """Raises `MarketingFrozen` while the circuit breaker is tripped. Call before
    opening the transport for any cohort — see `marketing/graph.py::_outreach`."""
    freeze = await repository.active_marketing_freeze(session)
    if freeze is not None:
        raise MarketingFrozen(freeze.reason)


def _is_resume(response: str) -> bool:
    """Fail-closed, mirroring `campaign._parse_decision`: only the exact word RESUME
    as the reply's first token clears a reputation-damaging freeze. A plain prefix
    check would also match "resuming", "resumed" — hedged or past-tense language
    that is not an unambiguous decision to resume sending *now*. Silence or a vague
    reply must never be read as authorisation to keep sending."""
    words = response.strip().split(maxsplit=1)
    return bool(words) and words[0].upper() == "RESUME"


async def sync_freeze_clearance(session: AsyncSession) -> int:
    """Run once per tick from `Scheduler.housekeeping`, after replies are collected.

    Returns 1 if a freeze was actually cleared this call, else 0 — including the
    case where a reply arrived but wasn't an unambiguous RESUME, which stays frozen.
    """
    freeze = await repository.active_marketing_freeze(session)
    if freeze is None or freeze.interrupt_id is None:
        return 0
    interrupt = await session.get(Interrupt, freeze.interrupt_id)
    if interrupt is None or interrupt.status != "resolved":
        return 0

    if _is_resume(interrupt.response or ""):
        await repository.clear_marketing_freeze(session, freeze.id)
        log.info("deliverability.freeze_cleared", freeze_id=freeze.id)
        return 1

    # Not an unambiguous RESUME: stays frozen. `interrupts.collect_replies` resolves
    # an interrupt on *any* tagged reply, correct or not, so the tag that reply used
    # is now consumed — raise a fresh interrupt so the operator still has a live tag
    # to reply to, rather than being stuck with no way to clear it.
    new_interrupt_id = await interrupts.request(
        session,
        reason=InterruptReason.DELIVERABILITY_FROZEN,
        subject=_SUBJECT,
        detail=(
            f"{freeze.reason}\n\nStill frozen — the last reply was not an "
            "unambiguous RESUME. Reply RESUME to this message to resume sending."
        ),
    )
    if new_interrupt_id is not None:
        await repository.set_marketing_freeze_interrupt(session, freeze.id, new_interrupt_id)
    return 0
