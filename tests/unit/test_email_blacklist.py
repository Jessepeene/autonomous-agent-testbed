"""The blacklist gate.

The most important tests in the repository. Each one asserts that no transport was
touched, not merely that the return value was False — a function that returns False
*after* sending has still sent the email.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from pydantic import SecretStr

from factory.engines.marketing import email as email_module
from factory.engines.marketing.email import blacklist_match, matches_rule, send_email
from factory.settings import Settings


@pytest.fixture
def live_settings(settings: Settings) -> Settings:
    """Dry run OFF and Mailgun configured, so only the blacklist can stop a send."""
    return settings.model_copy(
        update={
            "factory_dry_run": False,
            "mailgun_api_key": SecretStr("key-test"),
            "mailgun_domain": "mg.example.com",
        }
    )


@pytest.mark.parametrize(
    ("address", "expected_rule"),
    [
        ("blocked@example.com", "blocked@example.com"),
        ("BLOCKED@EXAMPLE.COM", "blocked@example.com"),
        ("  blocked@example.com  ", "blocked@example.com"),
        ("anyone@spam-domain.test", "@spam-domain.test"),
        ("someone@subs.test", "*.subs.test"),
        ("someone@deep.nested.subs.test", "*.subs.test"),
        ("erased@example.com", "erased@example.com"),
    ],
)
def test_blacklist_matches(address: str, expected_rule: str, settings: Settings) -> None:
    assert blacklist_match(address, settings) == expected_rule


@pytest.mark.parametrize(
    "address",
    [
        "allowed@example.com",
        "someone@notsubs.test",
        "someone@example.org",
        "blocked@example.com.evil.test",
    ],
)
def test_blacklist_allows_everything_else(address: str, settings: Settings) -> None:
    assert blacklist_match(address, settings) is None


def test_inline_comment_is_stripped_from_erasure_entries(settings: Settings) -> None:
    """erase_lead() appends `address  # ERASURE ...`; the comment must not break matching."""
    assert blacklist_match("erased@example.com", settings) == "erased@example.com"


def test_wildcard_does_not_match_unrelated_suffix() -> None:
    assert not matches_rule("someone@evilsubs.test", "*.subs.test")
    assert matches_rule("someone@subs.test", "*.subs.test")


async def test_send_refuses_blacklisted_address_without_touching_transport(
    live_settings: Settings,
) -> None:
    with patch.object(email_module, "_mailgun_send") as transport:
        result = await send_email(
            to="blocked@example.com",
            subject="hello",
            body="body",
            settings=live_settings,
        )
    assert result.sent is False
    assert "blacklist" in result.reason
    transport.assert_not_called()


async def test_send_refuses_blacklisted_domain_without_touching_transport(
    live_settings: Settings,
) -> None:
    with patch.object(email_module, "_mailgun_send") as transport:
        result = await send_email(
            to="anyone@spam-domain.test",
            subject="hello",
            body="body",
            settings=live_settings,
        )
    assert result.sent is False
    transport.assert_not_called()


async def test_send_refuses_wildcard_subdomain_without_touching_transport(
    live_settings: Settings,
) -> None:
    with patch.object(email_module, "_mailgun_send") as transport:
        result = await send_email(
            to="someone@deep.nested.subs.test",
            subject="hello",
            body="body",
            settings=live_settings,
        )
    assert result.sent is False
    transport.assert_not_called()


async def test_missing_blacklist_fails_closed(live_settings: Settings, tmp_path: Path) -> None:
    """A deleted suppression list must suppress everyone, not nobody."""
    missing = live_settings.model_copy(update={"blacklist_path": tmp_path / "gone.txt"})
    with patch.object(email_module, "_mailgun_send") as transport:
        result = await send_email(
            to="definitely-not-blacklisted@example.org",
            subject="hello",
            body="body",
            settings=missing,
        )
    assert result.sent is False
    assert "blacklist unavailable" in result.reason
    transport.assert_not_called()


async def test_directory_in_place_of_blacklist_fails_closed(
    live_settings: Settings, tmp_path: Path
) -> None:
    """An unreadable path is as dangerous as a missing one."""
    directory = tmp_path / "blacklist-dir"
    directory.mkdir()
    broken = live_settings.model_copy(update={"blacklist_path": directory})
    with patch.object(email_module, "_mailgun_send") as transport:
        result = await send_email(to="someone@example.org", subject="s", body="b", settings=broken)
    assert result.sent is False
    transport.assert_not_called()


async def test_dry_run_never_sends(settings: Settings) -> None:
    with patch.object(email_module, "_mailgun_send") as transport:
        result = await send_email(
            to="allowed@example.org", subject="hello", body="body", settings=settings
        )
    assert result.sent is False
    assert "DRY_RUN" in result.reason
    transport.assert_not_called()


async def test_allowed_address_reaches_transport_when_live(live_settings: Settings) -> None:
    """The negative tests only mean something if the positive path actually sends."""
    with patch.object(email_module, "_mailgun_send") as transport:
        result = await send_email(
            to="allowed@example.org",
            subject="hello",
            body="body",
            settings=live_settings,
            unsubscribe_url="https://example.com/u/tok",
            tracking_id="outreach-123",
        )
    assert result.sent is True
    transport.assert_called_once()
    payload = transport.call_args.args[0]
    assert payload["to"] == "allowed@example.org"
    assert payload["h:List-Unsubscribe"] == "<https://example.com/u/tok>"
    assert payload["v:outreach_log_id"] == "outreach-123"


async def test_send_refuses_without_sender_identity(live_settings: Settings) -> None:
    anonymous = live_settings.model_copy(update={"outbound_from_address": None})
    with patch.object(email_module, "_mailgun_send") as transport:
        result = await send_email(
            to="allowed@example.org", subject="s", body="b", settings=anonymous
        )
    assert result.sent is False
    transport.assert_not_called()


async def test_send_refuses_without_mailgun_configured(settings: Settings) -> None:
    """Dry run off but no Mailgun credentials — must refuse, not raise."""
    unconfigured = settings.model_copy(update={"factory_dry_run": False})
    with patch.object(email_module, "_mailgun_send") as transport:
        result = await send_email(
            to="allowed@example.org", subject="s", body="b", settings=unconfigured
        )
    assert result.sent is False
    assert "Mailgun" in result.reason
    transport.assert_not_called()
