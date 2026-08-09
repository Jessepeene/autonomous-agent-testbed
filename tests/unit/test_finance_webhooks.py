"""Stripe webhook signature verification and event conversion.

`verify()` was never exercised with a real, signed Stripe payload before this --
every existing test drives `process_event()` directly with a hand-built plain
dict, bypassing `stripe.Webhook.construct_event()` and the real `StripeObject`
entirely. That hid a real bug: `StripeObject` only implements `__getitem__(str)`,
not the mapping/iterable protocol, so `dict(event)` fell back to Python's legacy
sequence-iteration protocol (`event[0]`, `event[1]`, ...) and crashed with
`KeyError: 0` on every single real webhook delivery.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time

import pytest

from factory.engines.finance.webhooks import SignatureInvalid, verify
from factory.settings import Settings


def _signed_header(payload: bytes, secret: str, *, timestamp: int | None = None) -> str:
    ts = timestamp if timestamp is not None else int(time.time())
    signed_payload = f"{ts}.".encode() + payload
    signature = hmac.new(secret.encode(), signed_payload, hashlib.sha256).hexdigest()
    return f"t={ts},v1={signature}"


def _settings(secret: str | None) -> Settings:
    return Settings(
        _env_file=None,  # hermetic: a real developer .env must not leak in here
        database_url="postgresql+asyncpg://x/y",
        stripe_webhook_secret=secret,
    )


def test_verify_returns_a_fully_plain_nested_dict_for_a_real_signed_payload() -> None:
    secret = "whsec_test_secret"
    payload = json.dumps(
        {
            "id": "evt_test123",
            "object": "event",
            "type": "payment_intent.succeeded",
            "data": {
                "object": {
                    "id": "pi_test",
                    "object": "payment_intent",
                    "amount_received": 4900,
                    "metadata": {"product_id": "prod_1"},
                }
            },
        }
    ).encode()
    header = _signed_header(payload, secret)

    event = verify(payload, header, _settings(secret))

    assert event["id"] == "evt_test123"
    assert event["type"] == "payment_intent.succeeded"
    # The exact regression: a nested StripeObject left un-converted (or a crash
    # before ever getting here) means process_event()'s .get()-based reads below
    # would silently see nothing rather than the real amount/metadata.
    assert isinstance(event["data"], dict)
    assert isinstance(event["data"]["object"], dict)
    assert event["data"]["object"]["amount_received"] == 4900
    assert event["data"]["object"]["metadata"]["product_id"] == "prod_1"


def test_verify_rejects_a_signature_signed_with_the_wrong_secret() -> None:
    payload = b'{"id": "evt_1", "type": "payment_intent.succeeded"}'
    bad_header = _signed_header(payload, "whsec_wrong_secret")

    with pytest.raises(SignatureInvalid):
        verify(payload, bad_header, _settings("whsec_real_secret"))


def test_verify_raises_a_clear_error_when_no_webhook_secret_is_configured() -> None:
    with pytest.raises(SignatureInvalid, match="STRIPE_WEBHOOK_SECRET"):
        verify(b"{}", "t=1,v1=x", _settings(None))
