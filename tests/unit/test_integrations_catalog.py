"""`observability.integrations`'s static credential catalog and masking --
the dashboard's Integrations page shows real configured/not-configured state
without ever exposing a real secret value.
"""

from __future__ import annotations

from factory.observability.integrations import (
    CREDENTIAL_CATALOG,
    catalog_field,
    credential_statuses,
    validate_custom_key,
)
from factory.settings import Settings


def _settings(**overrides: object) -> Settings:
    return Settings(database_url="postgresql+asyncpg://u:p@h/db", **overrides)  # type: ignore[arg-type]


def test_every_catalog_key_is_a_real_settings_field() -> None:
    settings = _settings()
    for spec in CREDENTIAL_CATALOG:
        assert hasattr(settings, spec.key), f"{spec.key} is not a real Settings field"


def test_catalog_field_looks_up_case_insensitively() -> None:
    assert catalog_field("HUNTER_API_KEY") is not None
    assert catalog_field("hunter_api_key") is not None


def test_catalog_field_returns_none_for_an_unknown_key() -> None:
    assert catalog_field("not_a_real_credential") is None


def test_database_url_is_not_editable() -> None:
    """A wrong DATABASE_URL bricks every engine with no recovery path through
    the dashboard itself -- see the module docstring."""
    spec = catalog_field("database_url")
    assert spec is not None
    assert spec.editable is False


def test_an_unconfigured_secret_reports_not_configured_with_no_preview() -> None:
    settings = _settings(hunter_api_key=None)

    statuses = {s.key: s for s in credential_statuses(settings)}

    assert statuses["hunter_api_key"].configured is False
    assert statuses["hunter_api_key"].preview is None


def test_a_configured_secret_is_masked_to_its_last_four_characters() -> None:
    settings = _settings(hunter_api_key="sk-live-abcd1234")

    statuses = {s.key: s for s in credential_statuses(settings)}

    status = statuses["hunter_api_key"]
    assert status.configured is True
    assert status.preview == "••••1234"
    assert "sk-live" not in (status.preview or "")


def test_a_short_configured_secret_is_fully_masked_not_partially_leaked() -> None:
    settings = _settings(hunter_api_key="ab")

    status = {s.key: s for s in credential_statuses(settings)}["hunter_api_key"]

    assert status.preview == "••••"
    assert "ab" not in status.preview


def test_a_non_secret_field_shows_its_real_value_not_a_mask() -> None:
    """telegram_chat_id, mailgun_domain, etc. carry no secret -- masking them
    would just make the page less useful for no privacy benefit."""
    settings = _settings(telegram_chat_id="123456789")

    status = {s.key: s for s in credential_statuses(settings)}["telegram_chat_id"]

    assert status.configured is True
    assert status.preview == "123456789"


def test_validate_custom_key_accepts_a_well_formed_new_key() -> None:
    assert validate_custom_key("MY_NEW_INTEGRATION_KEY") is None


def test_validate_custom_key_rejects_lowercase() -> None:
    assert validate_custom_key("my_new_key") is not None


def test_validate_custom_key_rejects_a_key_starting_with_a_digit() -> None:
    assert validate_custom_key("1KEY") is not None


def test_validate_custom_key_rejects_punctuation() -> None:
    assert validate_custom_key("MY-KEY") is not None
    assert validate_custom_key("MY KEY") is not None


def test_validate_custom_key_rejects_a_key_already_in_the_catalog() -> None:
    """That key already has a managed, catalog-backed write path -- a second,
    divergent way to write the same env var would be confusing, not useful."""
    error = validate_custom_key("HUNTER_API_KEY")
    assert error is not None
    assert "already a known credential" in error


def test_validate_custom_key_rejects_a_reserved_os_env_var_name() -> None:
    """.env feeds straight into the container's real process environment --
    this is a real guardrail, not a theoretical one."""
    error = validate_custom_key("PATH")
    assert error is not None
    assert "reserved" in error

    error = validate_custom_key("LD_PRELOAD")
    assert error is not None
