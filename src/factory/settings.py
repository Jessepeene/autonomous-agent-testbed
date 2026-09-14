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

from factory.logging import get_logger

log = get_logger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[2]


class TaskType(StrEnum):
    """What a model call is *for*. Determines which provider serves it."""

    ORCHESTRATION = "orchestration"
    DISCOVERY = "discovery"
    ANALYSIS = "analysis"
    COMPLIANCE = "compliance"
    MARKETING_COPY = "marketing_copy"
    #: A judgement about how something *looks*, made by looking at it. Its own type
    #: rather than ANALYSIS because the routing has to guarantee a model with eyes:
    #: a visual review served by a text-only provider answers confidently from the
    #: markup, which is precisely the failure it exists to prevent -- a page built
    #: from a framework's stock palette with no typeface of its own passed every
    #: source-reading check and only a human noticed.
    VISUAL_REVIEW = "visual_review"
    #: Building a landing/validation page from nothing, or regenerating one. A page
    #: with a form and a pricing section, not an application -- split out from
    #: CODE_GENERATION because conflating them meant a landing page was priced,
    #: budgeted and routed as if it were a complete MVP.
    LANDING_PAGE = "landing_page"
    #: Producing a complete working MVP from a validated idea, from nothing. The
    #: single most consequential generation the factory does and the one where model
    #: quality shows most: it is one shot at an entire product.
    CODE_GENERATION = "code_generation"
    #: Regenerating an MVP that already exists, given its current files -- adding a
    #: feature, or rebuilding after a repair could not be done in place. A different
    #: job from CODE_GENERATION despite sharing a prompt: the model is editing a
    #: known product rather than inventing one, which is materially easier and does
    #: not warrant the same model.
    CODE_ITERATION = "code_iteration"
    CODE_REVIEW = "code_review"
    #: Agentic tool-calling repair/healing work (WORKSPACE_SURGEON, SEO_OPTIMIZER,
    #: REPAIR_ENGINEER) -- split out from CODE_GENERATION because it goes through
    #: `LLMClient.converse()` (tool_use blocks), never `parse()`'s strict
    #: JSON-schema-constrained output. That distinction is what makes this task
    #: type routable to DeepSeek (see router.py) while CODE_GENERATION/CODE_REVIEW,
    #: both parse()-based, stay on Claude -- DeepSeek's Anthropic-compatible
    #: endpoint does not reliably honor output_config.format=json_schema
    #: (confirmed empirically: returns prose instead of the requested JSON).
    CODE_REPAIR = "code_repair"


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
    # Unlike anthropic_api_key, this is not a hard-block credential: it only serves
    # TaskType.CODE_REPAIR (see router.py), never blocks boot. Missing it means
    # CODE_REPAIR calls fail with NoProviderAvailable (it's in NO_FAILOVER, so no
    # cross-vendor fallback) -- AgentRuntime.run() already handles that gracefully
    # (stopped_because="no_provider"), so this deliberately has no preflight
    # CredentialSpec disabling the whole DEVELOPMENT engine over it the way
    # ANTHROPIC_API_KEY's does; only repair/healing attempts are affected.
    deepseek_api_key: SecretStr | None = None
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

    # Google-results search API (serper.dev). Backs both the web-search-driven lead
    # provider and the general-purpose `web_search` agent tool.
    serper_api_key: SecretStr | None = None

    # Datacenter-class egress proxy for `WebsiteContactProvider`'s page fetches (see
    # `integrations/outbound.py`). A *different* IP, not a disguise -- no residential
    # or rotating proxies, no User-Agent spoofing, and a 429 never triggers a retry
    # through it (that would be evading a rate limit, exactly what
    # `IDEA_SYSTEM_PROMPT` rejects the factory building for someone else). Unset
    # means every fetch is direct-only, same as today.
    scrape_proxy_url: SecretStr | None = None

    #: A bare mailbox (e.g. "alex@mail.example.com"), never a "Display Name <email>"
    #: string — the display name is a learning-loop tunable
    #: (`factory.engines.marketing.outreach:SENDER_IDENTITY`) composed around this
    #: value at send time, not baked into it. See `get_settings`'s startup check.
    outbound_from_address: str | None = None
    outbound_postal_address: str | None = None
    unsubscribe_base_url: str | None = None

    # Bearer token for `GET /metrics`, which exposes the factory's full financial and
    # operational state. Unset means the endpoint refuses every request (503) rather
    # than serving openly -- it was previously unauthenticated and publicly reachable.
    # Degrading, not hard-block: the factory runs fine without metrics being readable
    # over HTTP, and the Caddyfile already 404s the path from the public interface, so
    # this is the second of two layers rather than the only one.
    metrics_auth_token: SecretStr | None = None

    # Bearer token for `GET /dashboard/state`, the read-only API behind the operator
    # dashboard's login. Deliberately a *separate* secret from metrics_auth_token:
    # that one guards a loopback-only, Caddy-404'd endpoint never meant to be
    # reachable from a browser; this one is the credential the dashboard's own
    # Next.js server holds and presents on every poll, from a genuinely
    # internet-facing (if login-gated) subdomain. Same fail-closed shape: unset
    # means every request gets 503, never an open endpoint.
    dashboard_auth_token: SecretStr | None = None

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
    # 5%: the industry-standard hard-bounce threshold major mailbox providers use as
    # their own sender-reputation bar (same shape as the complaint threshold above,
    # two orders of magnitude looser -- a wrong/dead address bouncing is expected at
    # some background rate; a sustained high rate is what damages sender reputation).
    bounce_rate_threshold: float = Field(default=0.05, ge=0.0, le=1.0)

    # --- Operational ------------------------------------------------------------
    factory_dry_run: bool = True
    # Orthogonal to factory_dry_run, not a variant of it: dry_run stubs *outward*
    # side effects (deploy, email send, card-limit sync) but never stubs an LLM
    # call, in either mode. CODE_GENERATION/CODE_REVIEW (MVP scaffolds, landing
    # pages, Bug Council) are 100% Anthropic-served with no failover and are the
    # large majority of real spend -- this swaps that one provider for a
    # deterministic, always-passing fixture (see llm/providers/staging.py) so the
    # rest of the pipeline (real sandbox install/test, real deploy, real lead
    # sourcing, real outreach composition) can be exercised repeatedly without
    # paying Opus prices on every run. Gemini-routed calls are never touched by
    # this flag -- Discovery, Compliance, lead planning, and outreach copy still
    # run for real.
    staging_mock_expensive_calls: bool = False
    # The domain Caddy terminates TLS for (see Caddyfile's `{$PUBLIC_DOMAIN}`).
    # Not read anywhere else in Python until validation: a generated landing page
    # is static HTML/JS with no build step, so it cannot read a server-side env var
    # the way the backend can (PRODUCT_ID) -- the factory's own public base URL has
    # to be baked into the page's fetch() target at generation time instead.
    public_domain: str | None = None
    log_level: str = "INFO"
    #: Durable JSON log file, in addition to stdout. Point this at a bind-mounted
    #: host path (see docker-compose.yml) so the record survives a container
    #: replacement -- `docker compose up -d` destroys the old container's Docker
    #: logs on every deploy, taking the history of any incident with it. Unset
    #: means stdout only, exactly as before.
    log_file_path: str | None = None
    api_host: str = "0.0.0.0"  # noqa: S104 - bound inside a container, published by compose
    api_port: int = 8000
    max_concurrent_builds: int = Field(default=2, ge=1, le=32)
    # Separate from max_concurrent_builds: a validation (landing page + outreach) is
    # deliberately much cheaper than a real MVP build and must not compete with real
    # builds for the same capacity slots, but it still needs *some* cap -- otherwise
    # every approved idea spawns an unbounded parallel validation, flooding the
    # campaign-approval queue and burning the shared, metered lead provider quota
    # before any of them prove out.
    max_concurrent_validations: int = Field(default=2, ge=1, le=32)
    # How long a validation's landing page stays up gathering interest signals
    # before EVALUATE_VALIDATION judges it. Anchored to outreach-send completion,
    # not landing-page deploy time (campaign approval is a human-paced, unbounded
    # wait that would otherwise silently shrink the real observation window).
    validation_window_days: int = Field(default=7, ge=1)
    # Below this many leads contacted, a response rate is statistically meaningless
    # -- same reasoning as deliverability_min_sample_size just above.
    validation_min_sample_size: int = Field(default=10, ge=1)
    validation_min_response_rate: float = Field(default=0.05, ge=0.0, le=1.0)
    # A validation signal only counts once the submitter clicks the emailed
    # confirmation link -- see api/app.py's /interest and /confirm/{token}.
    # Proven exploitable live: six unconfirmed emails alone cleared
    # validation_min_response_rate and carried a product to a real build. Off only
    # under factory_dry_run, where there is no real inbox to click a link from --
    # see insert_validation_signal's require_confirmation wiring.
    validation_require_double_opt_in: bool = True
    validation_confirmation_ttl_hours: int = Field(default=72, ge=1)
    # If one submitter IP accounts for more than this share of a product's
    # confirmed signals (checked once at least 5 have confirmed), the gate refuses
    # to pass rather than treat concentrated submissions as broad real interest.
    validation_max_ip_concentration: float = Field(default=0.5, ge=0.0, le=1.0)
    # HMAC key for hashing a submitter's IP before it's stored (api/abuse.py). Unset
    # means no IP is ever persisted, not that a reversible hash is -- plain sha256
    # of an IPv4 address is reversible in seconds (2^32 preimages), so this must be
    # a real secret, not a convenience toggle.
    abuse_ip_hash_secret: SecretStr | None = None
    interest_rate_limit_per_ip_hour: int = Field(default=5, ge=1)
    interest_rate_limit_per_product_hour: int = Field(default=60, ge=1)
    analytics_rate_limit_per_ip_hour: int = Field(default=300, ge=1)
    # Deterministic slopsquatting defense (engines/development/dependencies.py): a
    # package name a generated product's requirements.txt names must actually
    # exist on PyPI, and -- since a hallucinated name that happens to collide with
    # a real, recently-registered typosquat is exactly the attack -- must not have
    # been registered suspiciously recently either. Off switch exists because this
    # calls out to a real third-party API on every build; a PyPI outage already
    # fails open (see unresolvable_requirements), this is the harder kill switch.
    pypi_check_enabled: bool = True
    pypi_min_package_age_days: int = Field(default=90, ge=0)
    # Separate from marketing/graph.py's steady-state per-tick _OUTREACH_BATCH (15):
    # a validation run needs a batch sized for one real decision, not the ongoing
    # per-tick cap a live product's outreach uses.
    validation_outreach_batch_size: int = Field(default=30, ge=1)
    # Below this many page views on either arm, a signal rate is statistically
    # meaningless -- same reasoning as validation_min_sample_size, just per-arm.
    ab_test_min_views_per_arm: int = Field(default=30, ge=1)
    # One-sided significance threshold for declaring a winner -- matches
    # learning/trials.py's ROLLBACK_ALPHA (0.05), the same statistical bar this
    # factory already uses everywhere else it judges a Fisher's-exact result.
    ab_test_alpha: float = Field(default=0.05, gt=0.0, lt=1.0)
    # Safety cap: a test that never reaches min_views_per_arm on both sides would
    # otherwise run forever, permanently splitting traffic instead of ever
    # converging on a single, presumably-better page.
    ab_test_max_days: int = Field(default=14, ge=1)
    # Cadence of the GROWTH_ANALYST periodic backstop -- a live product not
    # reviewed within this many days gets one queued regardless of whether the
    # Orchestrator ever chose to spawn one itself. See scheduler.py.
    growth_review_interval_days: int = Field(default=7, ge=1)
    # Same idea for CHANNEL_SCOUT -- a longer cadence since channel discovery is a
    # heavier, rarer activity than a growth-data read.
    channel_scout_interval_days: int = Field(default=14, ge=1)
    # Cadence of AUDITOR's live-product walkthrough (scheduler.py ->
    # development/graph.py::_review_live_product). Daily by default: this is the
    # check that catches a shipped defect nobody has looked at, and the three-week
    # miss that motivated it argues for tight rather than thorough. One review is a
    # single capped agent run (AgentRole.cost_cap_usd), so the cost is bounded per
    # product per day, not per tick.
    live_review_interval_days: int = Field(default=1, ge=1)
    # How long an approved channel_experiment waits before the measurement sweep
    # tallies referrer hits and concludes it. Long enough that a human has had time
    # to actually post the draft and for any resulting traffic to show up.
    channel_experiment_measure_days: int = Field(default=14, ge=1)
    # How long a VALIDATING product with zero leads waits before the stalled-
    # validation sweep (scheduler.py) tries one automatic re-harvest, and how long
    # after *that* before it escalates to a human if still empty. Short relative to
    # the day-scale settings above: a stalled validation blocks everything
    # downstream for that product, so catching it sooner has real value, and a
    # provider outage or a bad credential is worth surfacing quickly, not after a
    # week of silent nothing. See repository.stalled_validations.
    validation_stalled_hours: int = Field(default=24, ge=1)
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
    # Ceiling on one surgical-repair agent run. Unlike a single code-generation call,
    # an agent re-sends its whole transcript every step, so spend grows superlinearly
    # in step count -- `max_steps` alone is a poor proxy for cost when tool results
    # are large. Set below the ~$1.15 a full regeneration costs today, because a
    # repair that costs more than regenerating has no reason to exist. The run stops
    # cleanly and its edits are rolled back; the heal cycle is not consumed.
    repair_agent_cost_cap_usd: float = Field(default=0.75, gt=0.0)
    # How many heal cycles a product gets before it stops and asks for a human.
    #
    # A counter, and an arbitrary one -- nothing measured says three is right. It is
    # a rule invented at a desk, which is the category this factory keeps getting
    # wrong: the real question is not "how many attempts" but "is this converging",
    # and until the `issues` table existed there was no way to ask that. A run that
    # closes two blockers and opens none is progress at attempt five; a run that
    # re-finds the same fingerprint with times_seen climbing is circling at attempt
    # two. Replacing this with that judgement is the intended direction.
    #
    # Configurable in the meantime so an observation run is not cut short by the
    # number itself, and so raising it is a deliberate operator act with a value in
    # the environment rather than an edit to a constant.
    max_heal_cycles: int = Field(default=3, ge=1)
    # Refuse to *start* a repair agent unless this much of the daily LLM budget is
    # still unspent. The global breaker is only consulted between ticks, so without
    # this a run starting just under the cap could carry it well past.
    repair_agent_min_budget_headroom_usd: float = Field(default=1.0, ge=0.0)
    # How long an open interrupt waits with no reply before the "remind-stale-
    # interrupts" maintenance job nudges again. Deliberately longer than the
    # maintenance item's own 6h cadence, so a fresh escalation gets its normal
    # delivery first and this only fires once the human has clearly had time to see it.
    stale_interrupt_reminder_hours: int = Field(default=12, ge=1)
    discovery_interval_seconds: int = Field(default=6 * 3600, ge=60)
    # Stops new idea cycles being scheduled at all, leaving Compliance, Development
    # and Marketing running normally.
    #
    # A real switch rather than a very large `discovery_interval_seconds`, which is
    # how this was done before: that works only by arithmetic accident (the
    # idempotency bucket `now // interval` stops advancing), reads as a typo to the
    # next person, and still lets one cycle through on the first tick after the
    # change. The blunt alternative -- unsetting GEMINI_API_KEY -- disables
    # Compliance and Marketing along with Discovery, which is exactly what you do
    # not want when the point is to watch one existing product move through them.
    #
    # Deliberately does not touch work already queued or in flight; it only stops
    # new cycles being created.
    discovery_enabled: bool = True
    # DISCOVERY_SCOUT (see agents/roles.py) runs once per Discovery cycle alongside
    # the fixed HN/GitHub/Reddit/Product Hunt sources. Off switch for operational
    # control -- e.g. if it turns out too costly or too noisy -- without a code change.
    discovery_scout_enabled: bool = True
    # Temporary diagnostic aid, added while investigating why Discovery produced
    # zero surviving ideas in its first days live: logs the actual top-12 clusters
    # handed to the generator and every scored idea's niche/score/rejection_reason
    # each cycle (see `discovery/graph.py::run`), so the pipeline's real behavior
    # can be read straight from `docker compose logs` instead of re-running live
    # reproduction scripts by hand. Off by default -- flip on in `.env` only while
    # actively watching a stretch of cycles; meant to come back out once Discovery's
    # behavior is understood, not to become a permanent log line.
    discovery_verbose_logging: bool = False
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
    # Only TaskType.CODE_REPAIR is routed to this (see router.py) -- agentic
    # repair/healing work, proven live against a real broken-test-suite fixture
    # via the real WORKSPACE_SURGEON role/AgentRuntime tool-calling loop before
    # this routing was wired in. Deliberately never used for CODE_GENERATION/
    # CODE_REVIEW's parse()-based structured output, which DeepSeek's
    # Anthropic-compatible endpoint does not reliably produce.
    model_deepseek: str = "deepseek-v4-pro"
    #: The model for the two DeepSeek-served code paths that are not initial MVP
    #: generation: building/regenerating a landing page, and iterating on an MVP
    #: that already exists. Pro rather than flash because both write real code that
    #: ships, and flash rather than Claude because neither is the one-shot
    #: whole-product build that justifies Opus.
    model_deepseek_pro: str = "deepseek-v4-pro"
    #: POC mode: route EVERY task type to DeepSeek on this one model, overriding the
    #: routing table entirely (see `llm/router.py::model_for` for why, and why it
    #: does not weaken NO_FAILOVER). `deepseek-flash` is DeepSeek-V4.1-Flash: 1M
    #: context, native tool calls, JSON output, Anthropic-format endpoint -- so the
    #: existing DeepSeek provider serves it without a new adapter.
    #:
    #: Unset (the default) means the routing table applies exactly as written. A
    #: wrong id here is unusually costly: CODE_REPAIR cannot fail over, so a typo
    #: takes repair offline entirely -- `preflight` verifies it against the
    #: provider's own model list rather than trusting the string.
    poc_model_override: str | None = None
    # Used only when a Gemini-routed call (orchestration/discovery/analysis/
    # compliance/marketing copy -- all normally cheap, high-frequency work) fails
    # over to Anthropic. CODE_GENERATION/CODE_REVIEW are in `router.NO_FAILOVER` and
    # never reach this path, so `model_code` (Opus 5, priced for code-quality work)
    # is the wrong default here: every Gemini outage previously meant every one of
    # these calls -- including the orchestration tick that runs every 15s -- silently
    # ran on Opus-5 pricing (~10-25x the input price) with no alarm anywhere.
    model_fallback_anthropic: str = "claude-haiku-4-5"
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
    # Writable path to the real .env file docker-compose reads for this service's
    # `env_file: .env` directive (bind-mounted there in docker-compose.yml, same
    # pattern as BLACKLIST_PATH above). Lets the dashboard's credential-write
    # endpoint (api/dashboard.py) persist a new value to the file docker-compose
    # itself reads at container (re)creation. Unset means that endpoint refuses
    # every write with a clear error rather than silently no-op'ing -- same
    # fail-closed posture as every other optional credential in this file.
    # Writing here changes nothing for the *running* process: Settings is an
    # lru_cache'd singleton read once at boot, so a new value only takes effect
    # after the next `docker compose up -d factory`, which the write endpoint's
    # response always says explicitly.
    env_file_path: Path | None = None

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
    def has_deepseek(self) -> bool:
        return self.deepseek_api_key is not None and bool(
            self.deepseek_api_key.get_secret_value()
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
    settings = Settings()
    if settings.outbound_from_address and "<" in settings.outbound_from_address:
        # OUTBOUND_FROM_ADDRESS must be a bare mailbox now that the display name is
        # composed at send time (see the field's own comment) -- a value that
        # already carries one would double-wrap into a broken header, e.g.
        # "Alex Reyes <Alex Reyes <alex@mail.example.com>>".
        log.warning(
            "settings.outbound_from_address_looks_wrapped",
            detail="OUTBOUND_FROM_ADDRESS should be a bare mailbox, not a display-name string",
        )
    return settings
