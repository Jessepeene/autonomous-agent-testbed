"""The spam-complaint circuit breaker — signature verification and the pure,
DB-free parsing logic.

`_check_and_trip`/`ensure_not_frozen`/`sync_freeze_clearance` read and write
`marketing_freezes`/`outreach_log` via `ON CONFLICT DO NOTHING`, which is
Postgres-specific and covered by an integration test. What matters most here is the
part with real security consequence — a forged webhook must never be accepted — and
the fail-closed RESUME parsing, mirroring the same property already tested for the
campaign-approval gate.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from typing import Any

import pytest

from factory.engines.marketing.deliverability import (
    SignatureInvalid,
    _event_data,
    _is_resume,
    process_event,
    verify,
)
from factory.settings import Settings


def _signed_payload(key: str, *, event: str = "complained", **event_data: object) -> dict[str, Any]:
    timestamp = str(int(time.time()))
    token = "t" * 50
    signature = hmac.new(
        key.encode("utf-8"), f"{timestamp}{token}".encode(), hashlib.sha256
    ).hexdigest()
    return {
        "signature": {"timestamp": timestamp, "token": token, "signature": signature},
        "event-data": {"event": event, **event_data},
    }


@pytest.fixture
def mailgun_settings(settings: Settings) -> Settings:
    from pydantic import SecretStr

    return settings.model_copy(update={"mailgun_webhook_signing_key": SecretStr("whsec_test")})


# --- signature verification ---------------------------------------------------


def test_a_correctly_signed_payload_verifies(mailgun_settings: Settings) -> None:
    payload = _signed_payload("whsec_test")
    verify(payload, mailgun_settings)  # must not raise


def test_a_forged_signature_is_rejected(mailgun_settings: Settings) -> None:
    payload = _signed_payload("wrong-key")
    with pytest.raises(SignatureInvalid):
        verify(payload, mailgun_settings)


def test_a_payload_with_no_signature_block_is_rejected(mailgun_settings: Settings) -> None:
    with pytest.raises(SignatureInvalid):
        verify({"event-data": {"event": "complained"}}, mailgun_settings)


def test_an_incomplete_signature_block_is_rejected(mailgun_settings: Settings) -> None:
    payload = {"signature": {"timestamp": "123"}, "event-data": {}}
    with pytest.raises(SignatureInvalid):
        verify(payload, mailgun_settings)


def test_verification_is_refused_when_no_signing_key_is_configured(settings: Settings) -> None:
    """No key configured must fail closed (refuse everything), never fail open."""
    payload = _signed_payload("whsec_test")
    with pytest.raises(SignatureInvalid):
        verify(payload, settings)


def test_a_tampered_event_type_does_not_invalidate_an_unrelated_signature(
    mailgun_settings: Settings,
) -> None:
    """The signature covers only timestamp+token, not the event body — this is
    Mailgun's actual documented scheme, not a flaw introduced here. Confirms the
    implementation matches that scheme rather than (incorrectly) also hashing the
    body, which would silently reject every legitimate webhook."""
    payload = _signed_payload("whsec_test", event="complained")
    payload["event-data"]["event"] = "delivered"  # tampered after signing
    verify(payload, mailgun_settings)  # still verifies — this is expected and correct


# --- event-data shape fallback --------------------------------------------------


def test_event_data_prefers_the_nested_modern_shape() -> None:
    payload = {"event-data": {"event": "complained"}, "event": "wrong"}
    assert _event_data(payload)["event"] == "complained"


def test_event_data_falls_back_to_a_flat_legacy_shape() -> None:
    payload = {"event": "complained", "recipient": "a@b.test"}
    assert _event_data(payload) == payload


# --- process_event: DB-free early returns ---------------------------------------


async def test_an_event_with_no_tracking_variable_is_ignored_without_touching_the_db() -> None:
    """`session=None` would crash on any real query — if this reaches the DB at
    all, the test fails with a crash rather than a clean assertion."""
    outcome = await process_event(None, {"event-data": {"event": "complained"}})  # type: ignore[arg-type]
    assert outcome.handled is False
    assert "outreach_log_id" in outcome.detail


async def test_an_untracked_event_type_is_ignored() -> None:
    outcome = await process_event(
        None,  # type: ignore[arg-type]
        {"event-data": {"event": "opened", "user-variables": {}}},
    )
    assert outcome.handled is False
    assert outcome.event == "opened"


# --- fail-closed RESUME parsing --------------------------------------------------


@pytest.mark.parametrize("reply", ["RESUME", "resume", "Resume sending please", "  RESUME  "])
def test_unambiguous_resume_clears(reply: str) -> None:
    assert _is_resume(reply) is True


@pytest.mark.parametrize(
    "reply",
    ["REJECT", "not yet", "why is this frozen?", "", "acknowledged", "resuming soon maybe"],
)
def test_anything_else_stays_frozen(reply: str) -> None:
    assert _is_resume(reply) is False
