"""Credential catalog + open usage-limit alerts for the dashboard's
Integrations page.

Three concerns live here, all read-mostly: what every known integration
credential *is* (a static catalog -- there is no DB table of "known
integrations," these are just `Settings` fields), whether it is currently
configured, without ever exposing the real secret value over the API (see
`_preview`), and -- since `CustomCredential` (`state/orm.py`) -- the same for
free-form, dashboard-added keys this factory has no adapter code for yet.
Both write paths (`POST /dashboard/integrations/credentials/{key}` for a
catalog key, `POST /dashboard/integrations/credentials` for a custom one)
live in `api/dashboard.py`, not here -- this module only describes state and
validates a proposed custom key, it never mutates anything.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime

from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession

from factory.config_writer import read_env_value
from factory.settings import Settings
from factory.state import repository
from factory.state.orm import CustomCredential


@dataclass(frozen=True, slots=True)
class CredentialField:
    key: str  # lowercase Settings field name -- also the lowercased .env key
    label: str
    group: str
    is_secret: bool = True
    #: False only for values too dangerous to let a dashboard write blind
    #: (DATABASE_URL: a wrong value bricks every engine with no recovery path
    #: through the dashboard itself, which depends on the same database to
    #: even authenticate the request that would fix it).
    editable: bool = True


#: Every credential-shaped `Settings` field, grouped to match the section
#: comments in `settings.py`/`.env.example`. "Add new ones" (the user's own
#: phrase) means filling in a value for one of these already-known-but-
#: unconfigured slots -- there is no code path anywhere for an integration
#: this factory does not already have adapter code for, so an arbitrary new
#: key is not something this page can make useful.
CREDENTIAL_CATALOG: tuple[CredentialField, ...] = (
    CredentialField("database_url", "Database URL", "Database", editable=False),
    CredentialField("gemini_api_key", "Gemini API key", "Core AI providers"),
    CredentialField("anthropic_api_key", "Anthropic API key", "Core AI providers"),
    CredentialField("deepseek_api_key", "DeepSeek API key", "Core AI providers"),
    CredentialField("telegram_bot_token", "Telegram bot token", "Human escalation (Telegram)"),
    CredentialField(
        "telegram_chat_id", "Telegram chat ID", "Human escalation (Telegram)", is_secret=False
    ),
    CredentialField("stripe_api_key", "Stripe API key", "Payments (Stripe)"),
    CredentialField("stripe_webhook_secret", "Stripe webhook secret", "Payments (Stripe)"),
    CredentialField(
        "stripe_issuing_card_id", "Stripe issuing card ID", "Payments (Stripe)", is_secret=False
    ),
    CredentialField("vercel_token", "Vercel token", "Deploy targets"),
    CredentialField("cloudflare_api_token", "Cloudflare API token", "Deploy targets"),
    CredentialField("github_token", "GitHub token", "Deploy targets"),
    CredentialField("reddit_client_id", "Reddit client ID", "Discovery sources"),
    CredentialField("reddit_client_secret", "Reddit client secret", "Discovery sources"),
    CredentialField("producthunt_token", "Product Hunt token", "Discovery sources"),
    CredentialField("hunter_api_key", "Hunter API key", "B2B lead providers"),
    CredentialField("apollo_api_key", "Apollo API key", "B2B lead providers"),
    CredentialField(
        "lead_provider_monthly_quota",
        "Lead provider monthly quota",
        "B2B lead providers",
        is_secret=False,
    ),
    CredentialField("serper_api_key", "Serper (Google search) API key", "Search"),
    CredentialField("scrape_proxy_url", "Scrape proxy URL", "Scraping proxy"),
    CredentialField(
        "outbound_from_address", "Outbound from address", "Outbound email identity", is_secret=False
    ),
    CredentialField(
        "outbound_postal_address",
        "Outbound postal address",
        "Outbound email identity",
        is_secret=False,
    ),
    CredentialField(
        "unsubscribe_base_url", "Unsubscribe base URL", "Outbound email identity", is_secret=False
    ),
    CredentialField("metrics_auth_token", "Metrics auth token", "Dashboard & metrics auth"),
    CredentialField("dashboard_auth_token", "Dashboard auth token", "Dashboard & metrics auth"),
    CredentialField("mailgun_api_key", "Mailgun API key", "Deliverability (Mailgun)"),
    CredentialField(
        "mailgun_domain", "Mailgun domain", "Deliverability (Mailgun)", is_secret=False
    ),
    CredentialField(
        "mailgun_base_url", "Mailgun base URL", "Deliverability (Mailgun)", is_secret=False
    ),
    CredentialField(
        "mailgun_webhook_signing_key", "Mailgun webhook signing key", "Deliverability (Mailgun)"
    ),
    CredentialField("abuse_ip_hash_secret", "Abuse IP hash secret", "Security"),
)

_CATALOG_BY_KEY: dict[str, CredentialField] = {field.key: field for field in CREDENTIAL_CATALOG}


def catalog_field(key: str) -> CredentialField | None:
    """Looked up by the write endpoint to enforce the allowlist -- an unknown
    or non-editable key must never reach the filesystem."""
    return _CATALOG_BY_KEY.get(key.lower())


_CUSTOM_KEY_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")

#: `.env` feeds straight into the running container's real process environment
#: (docker-compose's `env_file:` directive) -- a free-form key here isn't
#: cosmetic, so a short denylist of OS/interpreter-significant names is a
#: real guardrail, not a theoretical one.
_RESERVED_ENV_KEYS = frozenset(
    {
        "PATH",
        "HOME",
        "SHELL",
        "USER",
        "PWD",
        "TERM",
        "IFS",
        "LD_PRELOAD",
        "LD_LIBRARY_PATH",
        "PYTHONPATH",
        "PYTHONSTARTUP",
    }
)


def validate_custom_key(key: str) -> str | None:
    """Returns an error message if `key` cannot be added as a free-form
    credential, else `None`. Checked in this order: shape, then collision
    with a key that already has a managed catalog path, then a reserved name.
    """
    if not _CUSTOM_KEY_PATTERN.match(key):
        return (
            "key must be uppercase letters, digits, and underscores, "
            "starting with a letter (2-64 characters)"
        )
    if catalog_field(key) is not None:
        return f"{key} is already a known credential -- edit it above instead"
    if key in _RESERVED_ENV_KEYS:
        return f"{key} is a reserved name and cannot be used"
    return None


def _preview(value: object, *, is_secret: bool) -> str | None:
    if isinstance(value, SecretStr):
        raw = value.get_secret_value()
    elif value is None:
        raw = ""
    else:
        raw = str(value)
    if not raw:
        return None
    if not is_secret:
        return raw
    return f"••••{raw[-4:]}" if len(raw) > 4 else "••••"


@dataclass(slots=True)
class CredentialStatus:
    key: str
    label: str
    group: str
    is_secret: bool
    editable: bool
    configured: bool
    preview: str | None  # masked for secrets, plain for non-secrets, None if unconfigured


def credential_statuses(settings: Settings) -> list[CredentialStatus]:
    statuses = []
    for spec in CREDENTIAL_CATALOG:
        preview = _preview(getattr(settings, spec.key, None), is_secret=spec.is_secret)
        statuses.append(
            CredentialStatus(
                key=spec.key,
                label=spec.label,
                group=spec.group,
                is_secret=spec.is_secret,
                editable=spec.editable,
                configured=preview is not None,
                preview=preview,
            )
        )
    return statuses


def _custom_credential_status(row: CustomCredential, *, settings: Settings) -> CredentialStatus:
    """Same `CredentialStatus` shape as a catalog entry, but the current value
    has to come from the raw `.env` file -- a custom key is never a typed
    `Settings` attribute, so `getattr(settings, key)` isn't an option here."""
    raw = read_env_value(settings.env_file_path, row.key) if settings.env_file_path else None
    preview = _preview(raw, is_secret=True)
    return CredentialStatus(
        key=row.key,
        label=row.key,
        group="Custom",
        is_secret=True,
        editable=True,
        configured=preview is not None,
        preview=preview,
    )


@dataclass(slots=True)
class IntegrationAlertSection:
    id: str
    provider: str
    message: str
    created_at: datetime


@dataclass(slots=True)
class IntegrationsSnapshot:
    credentials: list[CredentialStatus]
    alerts: list[IntegrationAlertSection]
    env_write_configured: bool
    generated_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


async def collect(session: AsyncSession, *, settings: Settings) -> IntegrationsSnapshot:
    alerts = await repository.list_open_integration_alerts(session)
    custom = await repository.list_custom_credentials(session)
    return IntegrationsSnapshot(
        credentials=[
            *credential_statuses(settings),
            *[_custom_credential_status(row, settings=settings) for row in custom],
        ],
        alerts=[
            IntegrationAlertSection(
                id=alert.id,
                provider=alert.provider,
                message=alert.message,
                created_at=alert.created_at,
            )
            for alert in alerts
        ],
        env_write_configured=settings.env_file_path is not None,
    )
