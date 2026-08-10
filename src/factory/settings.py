"""Twelve-Factor configuration.

Every secret is read from the environment. No secret has a code default: an unset
credential must surface in preflight, never silently become an empty string that
fails at 3am inside an engine.
"""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]


class TaskType(StrEnum):
    """What a model call is *for*. Determines which provider serves it."""

    ORCHESTRATION = "orchestration"
    DISCOVERY = "discovery"
    ANALYSIS = "analysis"
    COMPLIANCE = "compliance"
    MARKETING_COPY = "marketing_copy"
    CODE_GENERATION = "code_generation"
    CODE_REVIEW = "code_review"


class EngineName(StrEnum):
    SUPERVISOR = "supervisor"
    DISCOVERY = "discovery"
    DEVELOPMENT = "development"
    MARKETING = "marketing"
    FINANCE = "finance"
    COMPLIANCE = "compliance"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Hard-block credentials -------------------------------------------------
    database_url: SecretStr | None = None
    gemini_api_key: SecretStr | None = None
    anthropic_api_key: SecretStr | None = None

    # --- Degrading credentials --------------------------------------------------
    telegram_bot_token: SecretStr | None = None
    telegram_chat_id: str | None = None
    stripe_api_key: SecretStr | None = None
    stripe_webhook_secret: SecretStr | None = None
    stripe_issuing_card_id: str | None = None
    vercel_token: SecretStr | None = None
    cloudflare_api_token: SecretStr | None = None
    github_token: SecretStr | None = None
    reddit_client_id: SecretStr | None = None
    reddit_client_secret: SecretStr | None = None
    producthunt_token: SecretStr | None = None

    # B2B lead providers. Metered per lookup, so the quota below is a hard ceiling.
    hunter_api_key: SecretStr | None = None
    apollo_api_key: SecretStr | None = None
    lead_provider_monthly_quota: int = Field(default=500, ge=0)

    outbound_from_address: str | None = None
    outbound_postal_address: str | None = None
    unsubscribe_base_url: str | None = None

    # Deliverability API (replaces raw SMTP for cold outreach — see
    # `engines/marketing/email.py`). Raw SMTP from a primary domain has no IP
    # rotation, no warmup, and no bounce/complaint feedback loop; a burst of cold
    # sends burns the domain's sender reputation in days. `mailgun_base_url` is
    # region-specific — the EU endpoint is `https://api.eu.mailgun.net/v3`, relevant
    # since this factory operates from the Netherlands.
    mailgun_api_key: SecretStr | None = None
    mailgun_domain: str | None = None
    mailgun_base_url: str = "https://api.mailgun.net/v3"
    mailgun_webhook_signing_key: SecretStr | None = None
    # Below this sample size a complaint rate is statistically meaningless (1
    # complaint in 3 sends is not a 33% problem) — the circuit breaker never trips
    # on volume this low, however bad the rate looks.
    deliverability_min_sample_size: int = Field(default=100, ge=1)
    # 0.1%: the industry-standard spam-complaint threshold major mailbox providers
    # (Gmail, Yahoo, Microsoft) use as their own blocking bar.
    spam_complaint_rate_threshold: float = Field(default=0.001, ge=0.0, le=1.0)

    # --- Operational ------------------------------------------------------------
    factory_dry_run: bool = True
    log_level: str = "INFO"
    api_host: str = "0.0.0.0"  # noqa: S104 - bound inside a container, published by compose
    api_port: int = 8000
    max_concurrent_builds: int = Field(default=2, ge=1, le=32)
    compliance_min_confidence: float = Field(default=0.85, ge=0.0, le=1.0)
    # Hard ceiling on aggregate LLM spend in any trailing 24h window. `None` (the
    # default) leaves the breaker disabled — an operator opts in deliberately, since
    # tripping it pauses every generative task in the factory. Rolling rather than
    # calendar-day: it self-clears as old calls age out of the window, needing no
    # midnight reset job and no manual unpause.
    daily_llm_spend_cap_usd: float | None = Field(default=None, ge=0.0)
    # Cloud-hosted Postgres (RDS, Cloud SQL, Neon, Supabase) enforces its own idle
    # connection timeout, commonly well under an hour. Both `max_idle` below must be
    # set well under whatever the server enforces, so the client recycles a
    # connection on its own schedule instead of discovering the server already
    # dropped it via a mid-query `psycopg.OperationalError`.
    checkpointer_pool_min_size: int = Field(default=1, ge=0)
    checkpointer_pool_max_size: int = Field(default=10, ge=1)
    checkpointer_pool_max_idle_seconds: float = Field(default=300.0, gt=0)
    checkpointer_pool_max_lifetime_seconds: float = Field(default=1800.0, gt=0)
    # Applied to the SQLAlchemy engine (`state/db.py`) for the same reason — recycle
    # a pooled connection proactively before a cloud provider's idle timeout closes it
    # underneath an in-flight transaction. Distinct from `pool_pre_ping`, which is
    # reactive (checks liveness right before handing a connection out); this is
    # proactive (never hands out a connection older than this regardless of health).
    db_pool_recycle_seconds: int = Field(default=1700, gt=0)
    # Below this, an entity-resolution verdict is treated as unresolved and the lead
    # is dropped — GDPR Art. 5(1)(d) accuracy applies to the entity-type judgement
    # exactly as it does to the email address itself.
    entity_resolution_min_confidence: float = Field(default=0.7, ge=0.0, le=1.0)
    # 1 = off: every lead gets its own LIA call (today's behaviour, unchanged). The
    # LIA is output-dominated (three limbs + a justification per lead), so batching
    # cuts call count sharply but total cost only modestly — a poor trade for a legal
    # gate until a differential check plus a human spot-check of real verdicts have
    # confirmed batched judgement quality matches the per-lead path.
    compliance_batch_size: int = Field(default=1, ge=1, le=10)
    max_task_attempts: int = Field(default=3, ge=1, le=10)
    task_lease_seconds: int = Field(default=900, ge=30)
    discovery_interval_seconds: int = Field(default=6 * 3600, ge=60)
    supervisor_tick_seconds: int = Field(default=15, ge=1)
    # The model is consulted on this cadence even when the deterministic route is
    # unambiguous — 40 ticks (10 min) is 6x faster than the hourly finance reconcile
    # bucket and still gives at least one look inside every 15-min task lease.
    supervisor_deliberation_ticks: int = Field(default=40, ge=1)
    # Consecutive identical ticks before a "nothing is changing" state is itself
    # treated as worth the model's attention.
    supervisor_stall_ticks: int = Field(default=4, ge=2)

    # --- Models (validated against the live APIs by preflight) ------------------
    model_general: str = "gemini-2.5-pro"
    model_fast: str = "gemini-2.5-flash"
    model_code: str = "claude-opus-5"
    # "xhigh" was observed live to make Claude Sonnet 5's adaptive thinking consume
    # an entire max_tokens budget without ever converging on an answer -- confirmed
    # by reproducing the identical generate_scaffold() call at "high" instead, which
    # completed normally (15 files, ~37k output tokens) where "xhigh" failed
    # identically at both 32_000 and 64_000 tokens. effort_review is lowered
    # preventively, not from its own separate reproduction: the mechanism (adaptive
    # thinking, no budget_tokens cap available on this model, an open-ended
    # "review and decide" task) is the same one that just failed for code
    # generation, and Bug Council hasn't run yet in this deployment to have hit it.
    effort_code: str = "high"
    effort_review: str = "high"
    use_claude_code: bool = False

    # --- Sandbox ----------------------------------------------------------------
    sandbox_image: str = "python:3.13-slim"
    sandbox_memory_mb: int = Field(default=1024, ge=128)
    sandbox_cpus: float = Field(default=1.0, gt=0)
    sandbox_pids_limit: int = Field(default=256, ge=16)
    sandbox_timeout_seconds: int = Field(default=600, ge=5)

    # --- Paths ------------------------------------------------------------------
    blacklist_path: Path = REPO_ROOT / "blacklist.txt"
    workspace_root: Path = REPO_ROOT / "workspaces"

    @field_validator("log_level")
    @classmethod
    def _upper(cls, value: str) -> str:
        return value.upper()

    @property
    def has_gemini(self) -> bool:
        return self.gemini_api_key is not None and bool(self.gemini_api_key.get_secret_value())

    @property
    def has_anthropic(self) -> bool:
        return self.anthropic_api_key is not None and bool(
            self.anthropic_api_key.get_secret_value()
        )

    @property
    def has_telegram(self) -> bool:
        return (
            self.telegram_bot_token is not None
            and bool(self.telegram_bot_token.get_secret_value())
            and bool(self.telegram_chat_id)
        )

    @property
    def has_stripe(self) -> bool:
        return self.stripe_api_key is not None and bool(self.stripe_api_key.get_secret_value())

    @property
    def has_mailgun(self) -> bool:
        return (
            self.mailgun_api_key is not None
            and bool(self.mailgun_api_key.get_secret_value())
            and bool(self.mailgun_domain)
        )

    def secret(self, name: str) -> str | None:
        """Read a credential by env-var name without importing it into logs."""
        value = getattr(self, name.lower(), None)
        if isinstance(value, SecretStr):
            raw = value.get_secret_value()
            return raw or None
        if isinstance(value, str):
            return value or None
        return None


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
