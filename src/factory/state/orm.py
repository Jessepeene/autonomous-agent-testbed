"""Database schema.

The tables *are* the inter-engine protocol. Nothing is passed in memory between
engines, so every handoff survives a reboot by construction.

Two constraints here are load-bearing compliance controls rather than hygiene:

* ``leads`` has exactly five personal columns. Data minimisation is impossible to
  bypass because there is nowhere to put extra scraped fields.
* ``leads.source_url`` / ``leads.scraped_at`` are ``NOT NULL``. Proof-of-origin
  cannot be omitted.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Text,
    UniqueConstraint,
    false,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


def new_id() -> str:
    return uuid.uuid4().hex


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def _pk() -> Mapped[str]:
    return mapped_column(String(32), primary_key=True, default=new_id)


class Task(Base):
    """Durable unit of work. Leased, never held in memory."""

    __tablename__ = "tasks"

    id: Mapped[str] = _pk()
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    engine: Mapped[str] = mapped_column(String(32), nullable=False)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)

    idea_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    product_id: Mapped[str | None] = mapped_column(String(32), nullable=True)

    # Idempotency: re-enqueuing the same logical work is a no-op, so a crash between
    # "decided to do X" and "recorded that X is queued" cannot duplicate X.
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)

    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    claimed_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    not_before: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_tasks_idempotency_key"),
        Index("ix_tasks_ready", "status", "priority", "not_before"),
        Index("ix_tasks_engine_status", "engine", "status"),
    )


class Idea(Base):
    __tablename__ = "ideas"

    id: Mapped[str] = _pk()
    problem_statement: Mapped[str] = mapped_column(Text, nullable=False)
    target_niche: Mapped[str] = mapped_column(String(200), nullable=False)
    source_url: Mapped[str] = mapped_column(String(2000), nullable=False)
    monetization_hypothesis: Mapped[str] = mapped_column(Text, nullable=False)
    score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    evidence_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    #: The demand judgement and audience brief, as `DemandAssessment` JSON. Kept on
    #: the idea rather than derived on demand because the evidence it was read from
    #: is expensive to re-read and the judgement is what downstream work is written
    #: against -- the landing page copy and the acceptance criteria should both come
    #: from the audience brief rather than from the idea's own generated prose.
    demand_assessment: Mapped[str | None] = mapped_column(Text)
    #: Per-dimension breakdown behind the blended `score` above -- added in
    #: 0023_idea_defensibility so a future weights change can be backtested against
    #: real historical ideas instead of only the single number that survived every
    #: weight vector that ever scored it. Nullable: every idea scored before this
    #: migration has no per-dimension data at all, and backfilling it would mean
    #: fabricating numbers no discriminator call ever actually produced.
    pain_severity: Mapped[float | None] = mapped_column(Float)
    willingness_to_pay: Mapped[float | None] = mapped_column(Float)
    build_feasibility: Mapped[float | None] = mapped_column(Float)
    adoption_ease: Mapped[float | None] = mapped_column(Float)
    defensibility: Mapped[float | None] = mapped_column(Float)
    #: Informational only -- see `schemas.PlatformDependency`/`ReplacementMode`.
    #: Plain strings (matching `status` above), not a native Postgres enum, so a
    #: future member addition is a Python-side change only.
    platform_dependency: Mapped[str | None] = mapped_column(String(16))
    replacement_mode: Mapped[str | None] = mapped_column(String(16))
    #: The discriminator's self-serve price target, carried through to codegen
    #: (`deploy.py::_runtime_env`) so the real Checkout amount and the pitch agree.
    #: Defaulted 0.0, matching `ScoredIdea.self_serve_price_usd`'s own default.
    self_serve_price_usd: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("fingerprint", name="uq_ideas_fingerprint"),
        Index("ix_ideas_queue", "status", "score"),
        CheckConstraint(
            "platform_dependency IN ('unknown','none','optional','required') "
            "OR platform_dependency IS NULL",
            name="ideas_platform_dependency_valid",
        ),
        CheckConstraint(
            "replacement_mode IN ('unknown','replaces','augments','observes') "
            "OR replacement_mode IS NULL",
            name="ideas_replacement_mode_valid",
        ),
    )


class Product(Base):
    __tablename__ = "products"

    id: Mapped[str] = _pk()
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    idea_id: Mapped[str] = mapped_column(ForeignKey("ideas.id"), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="planned")
    workspace_path: Mapped[str | None] = mapped_column(String(1000))
    deploy_url: Mapped[str | None] = mapped_column(String(1000))
    heal_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: Whether this is still a validation landing page or a real MVP.
    #:
    #: Lives here rather than being passed to `_verify_and_ship`, because as a
    #: parameter two of its four call sites forgot it -- both repair paths -- so a
    #: landing page was re-verified as an MVP the moment it needed a fix, and the
    #: codegen contract then demanded Stripe wiring its own requirements forbade.
    #: A fact four call sites must remember is a fact waiting to be dropped.
    build_kind: Mapped[str] = mapped_column(String(16), nullable=False, default="validation")
    #: Which Learning-Loop version of LANDING_PAGE_SYSTEM built this product's
    #: validation page, if it went through validation. Set once, read by
    #: EVALUATE_VALIDATION (run after HARVEST_LEADS/SEND_OUTREACH have both
    #: intervened) to credit the right tunable_parameter_id.
    landing_page_prompt_version: Mapped[str | None] = mapped_column(String(32))
    #: The shell command that runs this product's test suite, captured from the
    #: `ScaffoldPlan` that built it. Persisted because the agentic repair path edits
    #: the workspace in place instead of regenerating it, so nothing reproduces the
    #: plan -- both the repair agent and the verification gate read it from here.
    #: NULL for products built before this was recorded; the repair path treats that
    #: as "fall back to full regeneration" rather than guessing a default.
    test_command: Mapped[str | None] = mapped_column(String(400))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
    #: Set by `development/graph.py::_growth_review` after GROWTH_ANALYST finishes
    #: looking at this product, whatever it decided to do -- what the periodic
    #: backstop sweep (scheduler.py) and the `list_products_needing_growth_review`
    #: tool both order by, so a product nobody has looked at recently surfaces first.
    last_growth_review_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: Same idea, for CHANNEL_SCOUT's own periodic backstop -- a separate cadence
    #: (channel discovery is a heavier, rarer activity than a growth-data read).
    last_channel_scout_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: The exact Vercel project name/slug `deploy.py` used for this product's most
    #: recent successful deploy -- domain attachment needs this to target the right
    #: project, and re-deriving it from `product_name` risks drift if that name
    #: ever changes between builds (a repair pass can rename it).
    vercel_project: Mapped[str | None] = mapped_column(String(64))
    #: `IngestReport.summary()`'s own human-readable text, written every time
    #: `marketing/graph.py::_harvest` completes -- the report object itself is
    #: ephemeral (built fresh per call, consumed once by the credential-problem
    #: escalation, then discarded), so without this, a *later*, separate sweep
    #: (`scheduler.py`'s stalled-validation third strike) has no way to tell
    #: CHANNEL_SCOUT *why* lead sourcing stalled -- it would just re-propose
    #: another digital channel blind to whatever already failed.
    last_lead_sourcing_summary: Mapped[str | None] = mapped_column(String(500))
    #: A human-supplied hostname, once asked for and given -- see `development/domain.py`.
    custom_domain: Mapped[str | None] = mapped_column(String(253))
    #: NULL (never asked) -> 'pending' (asked, no reply yet) -> 'attached' (Vercel
    #: has the domain, DNS not yet verified) -> 'verified' (live on the custom
    #: domain). Never regresses once 'verified'.
    domain_status: Mapped[str | None] = mapped_column(String(16))
    #: A `QualificationBrief` (JSON: positive_criteria/strict_exclusions/
    #: borderline_guidance), generated once by `leads.py::build_qualification_brief`
    #: and cached here -- the web-search lead provider's ICP definition doesn't
    #: change call to call, so it's generated once per product rather than on
    #: every harvest. NULL until the first harvest that needs it.
    lead_qualification_brief: Mapped[str | None] = mapped_column(Text)
    #: A `DesignBrief` (JSON: palette/typography/tone), generated once by
    #: `development/design_brief.py::build_design_brief` from the real deployed
    #: page (not a guess from the idea text) and cached here -- surfaced on the
    #: dashboard's product detail view. NULL until generated: automatically the
    #: moment a build first ships (`development/graph.py::_verify_and_ship`), or
    #: on demand for a product that was already live before this existed.
    design_brief: Mapped[str | None] = mapped_column(Text)
    #: True while the workspace holds repaired files that have not reached the
    #: customer. The repair agent judges the workspace and its brief describes the
    #: deployed page; those agree only while everything has shipped, and they
    #: diverge exactly when a fix was applied and the deploy never happened. Without
    #: this, the next repair sees the work already done, says so honestly, and the
    #: handler closes the issues while the defects are still live.
    workspace_undeployed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )
    #: SHA-256 over the exact bundle the last successful deploy uploaded. Makes
    #: "is the workspace shipped?" a question that can be *asked* -- hash the
    #: workspace the same way and compare -- rather than a flag two code paths have
    #: to keep honest between them. `workspace_undeployed` above is the fallback for
    #: products deployed before this existed, where the answer is genuinely unknown.
    deployed_content_hash: Mapped[str | None] = mapped_column(String(64))

    __table_args__ = (Index("ix_products_status", "status"),)


class ValidationSignal(Base):
    """Real-world interest captured by a validation-stage landing page, before any
    MVP is built. Deliberately not lead-attributed -- same product-level-only
    scoping as revenue attribution (see `OutreachLog.converted_at`); a landing-page
    form submission has the identical "we can't prove which lead this was" problem.
    """

    __tablename__ = "validation_signals"

    id: Mapped[str] = _pk()
    product_id: Mapped[str] = mapped_column(ForeignKey("products.id"), nullable=False)
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    message: Mapped[str | None] = mapped_column(Text)
    #: Which AB-test arm this visitor was assigned, if a test was running when they
    #: submitted -- NULL outside a test. See `ABTest`.
    variant: Mapped[str | None] = mapped_column(String(32))
    #: NULL until the submitter clicks the emailed confirmation link -- an
    #: unconfirmed row is not real evidence of interest. See api/app.py's
    #: /interest and /confirm/{token}, and repository.tally_validation_signals'
    #: confirmed_only default.
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    confirmation_token: Mapped[str | None] = mapped_column(String(64))
    #: A submission from the reviewer's reserved address. Recorded like any other
    #: signal and excluded from every demand count -- see
    #: `repository.tally_validation_signals`.
    #:
    #: BUG_LOG 36 fixed a reviewer's invented signup inflating the build-or-scrap
    #: count by making that address leave no trace at all, which conflated "must not
    #: corrupt the count" with "must not be recorded". A submission that records
    #: nothing composes no confirmation email and issues no token, so everything past
    #: the form became invisible to review by construction -- and a plain-text
    #: confirmation page served from the factory's own domain sat in front of real
    #: visitors for weeks with no employee able to look at it.
    is_self_test: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    #: 'double_opt_in' | 'dry_run_auto' | 'operator' -- why this row counts, kept
    #: even after confirmation so an audit can tell a real click from a
    #: dry-run/manual bypass without cross-referencing anything else.
    confirmation_method: Mapped[str | None] = mapped_column(String(16))
    #: HMAC-SHA256 of the submitter's IP, never the IP itself -- see
    #: Settings.abuse_ip_hash_secret. NULL whenever that secret isn't configured;
    #: this column existing at all is not a promise every row has one.
    submitter_ip_hash: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("product_id", "email", name="uq_validation_signals_product_email"),
        Index("ix_validation_signals_product", "product_id"),
        Index(
            "ix_validation_signals_confirmation_token",
            "confirmation_token",
            unique=True,
            postgresql_where=(confirmation_token.isnot(None)),
        ),
    )


class RateLimitBucket(Base):
    """A fixed-window request counter for the two unauthenticated public write
    endpoints (`/interest`, `/analytics`) -- see `api/abuse.py::hit`.

    One row per (scope, key, window), upserted with `hits = hits + 1` on every
    request in that window; a request is refused once `hits` exceeds the
    configured ceiling. Stored in Postgres rather than in-process memory
    deliberately: this factory can run more than one API worker, and an
    in-memory counter would let a caller bypass the limit just by landing on a
    different worker.
    """

    __tablename__ = "rate_limit_buckets"

    bucket_key: Mapped[str] = mapped_column(String(200), primary_key=True)
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    hits: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    __table_args__ = (Index("ix_rate_limit_buckets_window", "window_start"),)


class PageView(Base):
    """A self-hosted analytics beacon, for every LIVE or VALIDATING product.

    Not lead-attributed and not deduplicated by visitor -- a page view is
    anonymous by design, unlike `ValidationSignal` (an email) or a `Lead` (a real
    contact). `variant` records which AB-test arm (if any) was showing -- see
    `ABTest`.
    """

    __tablename__ = "page_views"

    id: Mapped[str] = _pk()
    product_id: Mapped[str] = mapped_column(ForeignKey("products.id"), nullable=False)
    path: Mapped[str] = mapped_column(String(400), nullable=False)
    referrer: Mapped[str | None] = mapped_column(String(400))
    variant: Mapped[str | None] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        Index("ix_page_views_product", "product_id"),
        Index("ix_page_views_product_created", "product_id", "created_at"),
    )


class ABTest(Base):
    """A live, concurrent split test of a product's landing-page copy.

    Genuinely different from the Learning Loop's prompt trials
    (`learning/trials.py`): those are sequential (one version fully live, then
    compared to *historical* episodes over weeks); this serves two variants to
    different visitors of the *same* product over the *same* window, compared to
    each other directly (`development/ab_testing.py`). At most one `running` row
    per product, enforced by a partial unique index (migration `0013`) rather than
    application logic, so a race can never start two concurrent tests.
    """

    __tablename__ = "ab_tests"

    id: Mapped[str] = _pk()
    product_id: Mapped[str] = mapped_column(ForeignKey("products.id"), nullable=False)
    variant_a_brief: Mapped[str] = mapped_column(Text, nullable=False)
    variant_b_brief: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="running")
    #: "a" | "b" | "inconclusive", set only once status == "concluded".
    winner: Mapped[str | None] = mapped_column(String(16))
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    concluded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        Index("ix_ab_tests_product", "product_id"),
        # At most one running test per product. Declared here, not only in the
        # raw SQL of migration 0013, because `Base.metadata.create_all()` (every
        # test's `session` fixture -- see tests/integration/conftest.py) builds
        # its schema from this ORM metadata, not from Alembic; without a matching
        # declaration here, that index silently never existed under test, and
        # `create_ab_test`'s ON CONFLICT clause had nothing to match against --
        # see `MarketingFreeze` for the pattern this mirrors.
        Index(
            "uq_ab_tests_one_running_per_product",
            "product_id",
            unique=True,
            postgresql_where=(status == "running"),
        ),
    )


class ProductLock(Base):
    """Held by Development while building or patching.

    Marketing consults this before authorising paid spend: money is never spent
    driving traffic at a product that is mid-patch.
    """

    __tablename__ = "product_locks"

    product_id: Mapped[str] = mapped_column(
        ForeignKey("products.id"), primary_key=True, nullable=False
    )
    holder: Mapped[str] = mapped_column(String(64), nullable=False)
    reason: Mapped[str] = mapped_column(String(200), nullable=False)
    blocks_paid_spend: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    acquired_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class Lead(Base):
    """GDPR-minimised prospect record.

    Adding a personal column here is a deliberate schema change that must be argued
    for, not something a scraper can do by returning extra keys.
    """

    __tablename__ = "leads"

    id: Mapped[str] = _pk()

    first_name: Mapped[str] = mapped_column(String(100), nullable=False)
    last_name: Mapped[str] = mapped_column(String(100), nullable=False)
    job_title: Mapped[str] = mapped_column(String(200), nullable=False)
    company: Mapped[str] = mapped_column(String(200), nullable=False)
    work_email: Mapped[str] = mapped_column(String(320), nullable=False)

    source_url: Mapped[str] = mapped_column(String(2000), nullable=False)
    scraped_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # Which disclosure this record is entitled to under GDPR Art. 14(2)(f): a primary
    # URL lets us name the page, a broker record must name the broker.
    provenance_kind: Mapped[str] = mapped_column(
        String(32), nullable=False, default="operator_supplied"
    )
    provider: Mapped[str] = mapped_column(String(64), nullable=False, default="operator-csv")

    lia_passed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    lia_justification: Mapped[str | None] = mapped_column(Text)
    product_id: Mapped[str | None] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("work_email", name="uq_leads_work_email"),
        CheckConstraint("length(source_url) > 7", name="source_url_present"),
        Index("ix_leads_domain", "company"),
    )


class MarketingFreeze(Base):
    """The spam-complaint circuit breaker's persisted state.

    At most one row may be ``'active'`` at a time (enforced the same way as
    ``ux_tunable_parameters_active`` — a partial unique index, not application
    discipline). Unlike the daily LLM spend cap, this never self-clears: a
    reputation-damaging event requires a human to actually look at it, so the only
    way out of ``'active'`` is an explicit ``RESUME`` reply to the linked interrupt
    (see ``engines/marketing/deliverability.py::sync_freeze_clearance``).
    """

    __tablename__ = "marketing_freezes"

    id: Mapped[str] = _pk()
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    complaint_rate: Mapped[float] = mapped_column(Float, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    interrupt_id: Mapped[str | None] = mapped_column(String(32), ForeignKey("interrupts.id"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    cleared_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint("status IN ('active','cleared')", name="marketing_freeze_status_valid"),
        Index(
            "ux_marketing_freezes_active",
            "status",
            unique=True,
            postgresql_where=(status == "active"),
        ),
    )


class EntityVerdictRow(Base):
    """A cached judgement of whether an email domain belongs to a registered
    corporate entity, produced by an ephemeral agent (see
    ``compliance/entity.py::resolve_entity_type``) and never a person.

    Named distinctly from ``schemas.EntityVerdict`` (the Pydantic contract the LLM
    call actually produces) so the two are never one accidental unqualified import
    away from shadowing each other.

    Cached by domain because the entity type is a property of the company, not of
    any one lead: without this, every lead sharing a domain within a harvest — the
    common case — would re-spend an agent call re-answering the same question.
    """

    __tablename__ = "entity_verdicts"

    id: Mapped[str] = _pk()
    email_domain: Mapped[str] = mapped_column(String(255), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(32), nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    evidence: Mapped[str] = mapped_column(Text, nullable=False)
    task_id: Mapped[str | None] = mapped_column(String(32))
    resolved_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        CheckConstraint(
            "entity_type IN ('corporate','sole_proprietor_or_individual','unknown')",
            name="entity_verdict_type_valid",
        ),
        UniqueConstraint("email_domain", name="uq_entity_verdicts_email_domain"),
    )


class OutreachLog(Base):
    __tablename__ = "outreach_log"

    id: Mapped[str] = _pk()
    #: Nullable, ON DELETE SET NULL: GDPR Art. 17 erasure (`repository.erase_lead`)
    #: deletes the `Lead` row outright, and this send record must survive that as
    #: an anonymous audit/attribution entry rather than block the erasure with a
    #: foreign-key violation or cascade-delete the send history itself.
    lead_id: Mapped[str | None] = mapped_column(
        ForeignKey("leads.id", ondelete="SET NULL"), nullable=True
    )
    product_id: Mapped[str | None] = mapped_column(String(32))
    channel: Mapped[str] = mapped_column(String(32), nullable=False, default="email")
    subject: Mapped[str] = mapped_column(String(500), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    suppressed_reason: Mapped[str | None] = mapped_column(String(200))
    unsubscribe_token: Mapped[str] = mapped_column(String(64), nullable=False)
    sent_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    #: Product-level attribution, not lead-level — Stripe never reveals which lead
    #: paid. Set by `engines/marketing/attribution.py`'s deterministic reconciliation
    #: job, never by the send path itself.
    converted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ledger_entry_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("ledger.id"), unique=True
    )
    #: Set from the deliverability provider's webhook (see
    #: `engines/marketing/deliverability.py`), correlated back to this row via the
    #: provider's user-defined-variable echo — never by the send path itself, which
    #: only knows whether the transport *accepted* the message, not what happened
    #: to it afterward.
    bounced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    complained_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: Which SENDER_IDENTITY version was active when this send went out, so its real
    #: outcome (converted / timed out unconverted) can be scored back to it later --
    #: see `marketing/attribution.py::reconcile`. Never set for a blocked/suppressed
    #: send (`status != "sent"`), since those never had a chance to convert.
    sender_identity_version_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("tunable_parameters.id")
    )
    #: Stamped only by the delayed timeout path (a converted send is already
    #: excluded from that path by `converted_at IS NOT NULL`) -- without this, a
    #: send that ages past the attribution window would be re-scored as a fresh
    #: failure every time `reconcile` runs.
    identity_outcome_recorded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        Index("ix_outreach_lead", "lead_id", "sent_at"),
        Index("ix_outreach_product_sent", "product_id", "sent_at"),
    )


class LedgerEntry(Base):
    """Append-only money log.

    ``balance_cents >= 0`` is a database constraint rather than an assertion in
    Python: no code path, present or future, can drive the factory negative.
    """

    __tablename__ = "ledger"

    id: Mapped[str] = _pk()
    external_event_id: Mapped[str] = mapped_column(String(200), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    amount_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    balance_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    reinvest_pool_cents: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="USD")
    product_id: Mapped[str | None] = mapped_column(String(32), ForeignKey("products.id"))
    #: Plain columns, not foreign keys -- Stripe webhook delivery order across
    #: *different* event types is not guaranteed, so a revenue event (this row)
    #: can arrive before the corresponding `customer.subscription.created` event
    #: has been processed. A FK would reject real money on a benign race; these
    #: are populated when available and left NULL otherwise, exactly like
    #: `product_id` above already is.
    stripe_subscription_id: Mapped[str | None] = mapped_column(String(255))
    stripe_customer_id: Mapped[str | None] = mapped_column(String(255))
    memo: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("external_event_id", name="uq_ledger_external_event_id"),
        CheckConstraint("balance_cents >= 0", name="balance_non_negative"),
        CheckConstraint("reinvest_pool_cents >= 0", name="reinvest_pool_non_negative"),
        Index("ix_ledger_created", "created_at"),
        Index("ix_ledger_product", "product_id"),
    )


class Customer(Base):
    """An opaque Stripe customer -- deliberately no email, no name.

    Mirrors `Lead`'s own minimisation stance (see its docstring): MRR and churn
    math need only the Stripe customer id, and the actual personal data stays
    inside Stripe, which is where GDPR erasure for that person already has to
    happen regardless of anything this factory does. One less place personal
    data can leak from.
    """

    __tablename__ = "customers"

    id: Mapped[str] = _pk()
    stripe_customer_id: Mapped[str] = mapped_column(String(255), nullable=False)
    #: Nullable, matching `LedgerEntry.product_id` -- attribution may not be
    #: available (no `subscription_data.metadata.product_id` contract exists yet
    #: for generated products; this table is ready for the moment one does),
    #: and that must never block recording the underlying fact of the customer.
    product_id: Mapped[str | None] = mapped_column(String(32), ForeignKey("products.id"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("stripe_customer_id", name="uq_customers_stripe_customer_id"),
        Index("ix_customers_product", "product_id"),
    )


class Subscription(Base):
    """One Stripe subscription's current state -- upserted on every
    `customer.subscription.*` event, never touched by `append_ledger`.

    `churn_kind`/`churned_at` are set exclusively by the `customer.subscription.
    deleted` handler (`finance/webhooks.py`) -- never by `.updated`, which merely
    reflects `cancel_at_period_end=true` when a customer schedules a future
    cancellation Stripe still allows them to reverse before the period ends.
    """

    __tablename__ = "subscriptions"

    id: Mapped[str] = _pk()
    stripe_subscription_id: Mapped[str] = mapped_column(String(255), nullable=False)
    customer_id: Mapped[str] = mapped_column(String(32), ForeignKey("customers.id"), nullable=False)
    product_id: Mapped[str | None] = mapped_column(String(32), ForeignKey("products.id"))
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    price_cents: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cancel_at_period_end: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    current_period_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: NULL until the subscription actually ends -- see the class docstring for
    #: exactly which event is allowed to set these.
    churn_kind: Mapped[str | None] = mapped_column(String(16))
    churned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: Set once `finance/churn.py::write_churn_lessons` has written this churn's
    #: memory -- the dedup marker that stops the same churn being remembered
    #: again on every future `review-churn` tick (`remember()`'s own near-duplicate
    #: reinforcement would otherwise just keep bumping the same row's hit count
    #: forever, which is harmless but pointless work every 24h indefinitely).
    memory_written_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("stripe_subscription_id", name="uq_subscriptions_stripe_subscription_id"),
        CheckConstraint(
            "churn_kind IN ('voluntary','involuntary') OR churn_kind IS NULL",
            name="subscriptions_churn_kind_valid",
        ),
        Index("ix_subscriptions_customer", "customer_id"),
        Index("ix_subscriptions_product", "product_id"),
        Index("ix_subscriptions_status", "status"),
        Index("ix_subscriptions_unremembered_churn", "churn_kind", "memory_written_at"),
    )


class DunningEvent(Base):
    """One at-risk invoice's recovery sequence -- one row per Stripe invoice
    (Stripe fires multiple `invoice.payment_failed` retries for the *same*
    invoice; those upsert this same row rather than each creating a new one),
    closed by whichever comes first: `invoice.paid` (recovered) or the
    subscription reaching a terminal state (unrecovered) -- see
    `finance/webhooks.py` for both close paths, so a customer who never
    resolves a failed payment does not leave this row open forever.
    """

    __tablename__ = "dunning_events"

    id: Mapped[str] = _pk()
    stripe_invoice_id: Mapped[str] = mapped_column(String(255), nullable=False)
    subscription_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("subscriptions.id"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="open")
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    opened_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closed_reason: Mapped[str | None] = mapped_column(String(64))

    __table_args__ = (
        UniqueConstraint("stripe_invoice_id", name="uq_dunning_events_stripe_invoice_id"),
        CheckConstraint(
            "status IN ('open','recovered','terminal')", name="dunning_events_status_valid"
        ),
        Index("ix_dunning_events_subscription", "subscription_id"),
        Index("ix_dunning_events_open", "subscription_id", "status"),
    )


class SentMessage(Base):
    """Every message this company composed, whether or not it went out.

    `send_email` is the one chokepoint all mail passes through, and nothing kept what
    was in it: `OutreachLog` records a subject and no body, so what was actually said
    to a real prospect could not be read afterwards by anyone -- human or reviewer. A
    review could examine a page in detail and never a word of the company's own writing.

    Retained for every send rather than for the messages somebody thought worth
    keeping, because deciding that in advance is how the interesting one gets missed:
    the confirmation email that said "Thanks for your interest" with no product name,
    no sender and no working unsubscribe link went out under an address nobody had
    read, on every signup, for weeks.

    Holds personal data about the recipient, so `erase_lead` and
    `erase_validation_signals` delete from here too.
    """

    __tablename__ = "sent_messages"

    id: Mapped[str] = _pk()
    product_id: Mapped[str | None] = mapped_column(String(32), index=True)
    to_address: Mapped[str] = mapped_column(String(320), nullable=False, index=True)
    from_address: Mapped[str | None] = mapped_column(String(320))
    subject: Mapped[str] = mapped_column(String(500), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    #: ``sent`` or ``composed_only``. Kept apart because "we said this" and "we would
    #: have said this" are different facts, and a review of an unsent draft read as a
    #: review of what a customer received is worse than no review.
    outcome: Mapped[str] = mapped_column(String(32), nullable=False)
    #: Why it was held, when it was -- the `SendResult` reason, verbatim.
    detail: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (Index("ix_sent_messages_product_created", "product_id", "created_at"),)


class Interrupt(Base):
    """A question for the human. Persisted so an unanswered escalation survives reboot."""

    __tablename__ = "interrupts"

    id: Mapped[str] = _pk()
    reason: Mapped[str] = mapped_column(String(32), nullable=False)
    subject: Mapped[str] = mapped_column(String(200), nullable=False)
    detail: Mapped[str] = mapped_column(Text, nullable=False)
    #: Emoji + specific category phrase for the Telegram message header, e.g.
    #: "🔑 Account problem" -- not derivable from `reason` alone (the same reason
    #: can mean genuinely different things depending on context, e.g.
    #: LEAD_SOURCING_STALLED covers both a credential problem and a code bug), so
    #: every raise site supplies its own. Nullable only for historical-row
    #: safety; every live call site sets it -- see `hitl/formatting.py`.
    headline: Mapped[str | None] = mapped_column(Text)
    #: The exact reply instruction shown in the "What to do" section -- kept
    #: separate from `detail` so the explanation and the instruction never get
    #: conflated or duplicated (they used to both live in `detail`, with a
    #: second generic instruction appended by the old `format_message`).
    reply_hint: Mapped[str | None] = mapped_column(Text)
    #: Whether a reply actually changes anything for this escalation -- controls
    #: "Include this tag in your reply" vs. a reference-only "Tag:" in the
    #: rendered message, so an operator can tell at a glance whether this needs
    #: action or is just an FYI.
    needs_reply: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    task_id: Mapped[str | None] = mapped_column(String(32))
    #: Set on interrupts with no task to retry (e.g. heal-cycle exhaustion, which
    #: resets the product to PLANNED instead of leaving one task in place) so a
    #: human's reply can still be traced back to the product it's about.
    product_id: Mapped[str | None] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="open")
    delivered: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    response: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        # A *partial* unique index, not a plain UniqueConstraint -- deliberately.
        # (reason, subject) must only ever collapse duplicates while status='open'
        # (that is the actual point: don't spam a human with the same still-open
        # ask twice). A plain 3-column UniqueConstraint enforced this globally,
        # across every status forever, so once any interrupt with a given
        # (reason, subject) was ever resolved, no later, unrelated occurrence of
        # that same pair could ever be resolved either -- resolve_interrupt()'s
        # UPDATE crashed on the exact (reason, subject, 'resolved') tuple already
        # sitting there. Found live; see alembic/versions/
        # 0017_interrupt_open_subject_partial.py for the full account.
        # Scoped by *what it is about*, not only by how it is worded -- see
        # migration 0042. A product escalation's subject comes from the product's
        # name, so two products with similar niches otherwise share one notice and
        # the second is silently swallowed. `COALESCE(product_id, '')` rather than
        # the bare column: Postgres treats NULLs as distinct in a unique index, so
        # the plain column would make every product-less escalation unique and undo
        # the provider-outage dedup entirely (BUG_LOG #20).
        Index(
            "uq_interrupts_open_subject",
            "reason",
            func.coalesce(product_id, ""),
            "subject",
            "status",
            unique=True,
            postgresql_where=(status == "open"),
        ),
        Index("ix_interrupts_status", "status", "delivered"),
    )


class IntegrationAlert(Base):
    """A usage-limit/quota/account signal from a third-party integration (Hunter,
    Apollo, ...) -- surfaced on the dashboard's Integrations page, deliberately
    *not* an `Interrupt`. An `Interrupt` asks a human to decide something and
    blocks a specific product/task until they reply; a quota hit blocks nothing
    (the pipeline already degrades gracefully around it -- see leads.py) and
    needs no reply, just visibility. Mixing the two would bury genuine
    decisions a human must make under high-frequency, no-action-needed noise
    every time a metered provider's limit resets on its normal monthly cycle.
    """

    __tablename__ = "integration_alerts"

    id: Mapped[str] = _pk()
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="open")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        # Partial unique index, same reasoning and same bug class as
        # Interrupt.uq_interrupts_open_subject (see
        # 0017_interrupt_open_subject_partial.py): scoped to status='open' so a
        # provider that hits the same limit repeatedly collapses into one open
        # row instead of spamming a new one on every retry, while a *resolved*
        # occurrence never blocks a later, genuinely new one from opening.
        Index(
            "uq_integration_alerts_open_provider_message",
            "provider",
            "message",
            "status",
            unique=True,
            postgresql_where=(status == "open"),
        ),
        Index("ix_integration_alerts_status", "status"),
    )


class CustomCredential(Base):
    """The registry half of a free-form, dashboard-added credential -- the
    Integrations page's escape hatch for a secret this factory doesn't yet
    have a typed `Settings` field or catalog entry for (see
    `observability/integrations.py::CREDENTIAL_CATALOG`).

    Only the *key name* lives here. The actual value is written straight to
    `.env` via the same `write_env_value()` every catalog credential already
    uses -- this table exists purely so the dashboard knows which extra keys
    to list back and preview, since an arbitrary key is not a `Settings`
    attribute `getattr()` can read.
    """

    __tablename__ = "custom_credentials"

    id: Mapped[str] = _pk()
    key: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class CampaignApproval(Base):
    """The top-level legal gate on outbound email: one row per marketing send-task
    cohort, created the first time that task is attempted and never bypassed —
    ``marketing/graph.py::_outreach`` reads ``status`` before opening the SMTP
    transport, and only ``'approved'`` allows the send loop to run at all.

    Scoped to ``task_id`` rather than ``product_id`` because a ``SEND_OUTREACH`` task
    already represents one discrete cohort (one harvest's worth of newly LIA-approved
    leads); a product that is harvested again later gets a fresh task, and therefore a
    fresh brief and a fresh approval, rather than one standing sign-off covering every
    future send.
    """

    __tablename__ = "campaign_approvals"

    id: Mapped[str] = _pk()
    #: Nullable: a `SEND_OUTREACH` task always carries one in practice (`_harvest`
    #: always sets it), but the fallback path in `_product_summary` for an orphaned
    #: task must not make the campaign gate itself blow up with an FK violation.
    product_id: Mapped[str | None] = mapped_column(String(32), ForeignKey("products.id"))
    task_id: Mapped[str] = mapped_column(String(32), ForeignKey("tasks.id"), nullable=False)
    lead_count: Mapped[int] = mapped_column(Integer, nullable=False)
    brief: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    interrupt_id: Mapped[str | None] = mapped_column(String(32), ForeignKey("interrupts.id"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(
            "status IN ('pending','approved','rejected')", name="campaign_approval_status_valid"
        ),
        UniqueConstraint("task_id", name="uq_campaign_approvals_task_id"),
        Index("ix_campaign_approvals_status", "status"),
    )


class ChannelExperiment(Base):
    """A candidate organic growth channel (Reddit, a forum, a community)
    CHANNEL_SCOUT researched and drafted content for -- see `agents/roles.py`.

    Gated by the same poll-based human-approval pattern as `CampaignApproval`,
    but deliberately NOT scoped by ``task_id``/``product_id`` on the raised
    `Interrupt` the way a heal-exhaustion escalation is -- see
    `engines/marketing/channel.py`'s module docstring for why: those fields
    route a reply through `hitl/interrupts.py::_act_on_product_reply`, which
    means "retire this product"/"queue a guided patch", not "approve this
    channel." ``interrupt_id`` on this row is the only link back.

    Real posting is explicitly out of scope: an APPROVE reply means a human
    posts ``draft_content`` themselves. Outcome is measured later, best-effort,
    via `page_views.referrer` matching the channel's own domain.
    """

    __tablename__ = "channel_experiments"

    id: Mapped[str] = _pk()
    product_id: Mapped[str] = mapped_column(ForeignKey("products.id"), nullable=False)
    channel: Mapped[str] = mapped_column(String(40), nullable=False)
    url: Mapped[str] = mapped_column(String(500), nullable=False)
    rationale: Mapped[str] = mapped_column(Text, nullable=False)
    draft_content: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="proposed")
    #: Free-text summary of what the measurement sweep found, set once concluded.
    outcome: Mapped[str | None] = mapped_column(Text)
    interrupt_id: Mapped[str | None] = mapped_column(String(32), ForeignKey("interrupts.id"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    concluded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(
            "status IN ('proposed','approved','rejected','concluded')",
            name="channel_experiment_status_valid",
        ),
        Index("ix_channel_experiments_product", "product_id"),
        Index("ix_channel_experiments_status", "status"),
    )


class ScoutSignal(Base):
    """One market-signal candidate DISCOVERY_SCOUT found while free-roaming the web.

    Purely a hand-off buffer, not a durable record: `discovery/scout.py` writes a
    row per `submit_signal` tool call during one agent run, the same cycle reads
    every row for that ``task_id`` straight back out as `Signal` objects and
    deletes them, so this table is empty between Discovery cycles by design --
    signals themselves have never been persisted anywhere in this codebase (the
    fixed HN/GitHub/Reddit sources hand `Signal` objects to the clusterer
    in-memory, within the same call), and an agent-produced signal keeps that
    property. ``task_id`` -- not a foreign key -- is deliberately loose: the task
    is a `DISCOVER_OPPORTUNITIES` row, not something this table needs to join
    against.
    """

    __tablename__ = "scout_signals"

    id: Mapped[str] = _pk()
    task_id: Mapped[str] = mapped_column(String(32), nullable=False)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    url: Mapped[str] = mapped_column(String(500), nullable=False)
    source: Mapped[str] = mapped_column(String(60), nullable=False)
    engagement: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (Index("ix_scout_signals_task", "task_id"),)


class ScrapeDomainFailure(Base):
    """What's been learned about fetching from one domain (the web-search lead
    provider, `marketing/providers/website.py`) -- a running per-domain state,
    not a per-URL log.

    Global, not per-product: a site blocking a datacenter IP or timing out
    blocks every product's harvest equally, so one domain's history is shared
    across all of them. Once `failure_count` crosses
    `WebsiteContactProvider`'s threshold, the domain is skipped before a fetch
    is even attempted on later harvests -- observed live: repeated
    `ConnectTimeout`/`ReadTimeout`/403s against the same small set of real
    sites wasted several minutes of a single harvest for zero leads.
    """

    __tablename__ = "scrape_domain_failures"

    domain: Mapped[str] = mapped_column(String(255), primary_key=True)
    failure_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    last_reason: Mapped[str] = mapped_column(String(32), nullable=False)
    last_failed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
    #: Set the first time a direct fetch fails but the same URL succeeds
    #: through `integrations/outbound.py`'s datacenter proxy -- once true,
    #: later requests to this domain skip the doomed direct attempt entirely
    #: rather than re-paying a real timeout (up to 20s) to rediscover
    #: something already known. Never reset automatically, matching
    #: `failure_count`'s own permanence -- a site that stops blocking is rare
    #: enough that a stale flag just costs one unnecessary proxy hop, not a
    #: correctness problem.
    requires_proxy: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    #: Failures that happened *through* the proxy, tracked separately from
    #: `failure_count` (which only ever counts direct-attempt failures) -- a
    #: domain that fails even via the datacenter proxy is a different, worse
    #: signal than one that just needed a different IP.
    proxy_failure_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class PypiPackageCheck(Base):
    """A cached verdict on whether a PyPI package name is real -- the deterministic
    defense against slopsquatting (see `engines/development/dependencies.py`).

    Global, not per-product, same reasoning as `ScrapeDomainFailure`: a package's
    existence and registration date are facts about the package, not about any one
    product's build. TTL'd via `checked_at`, not permanent -- a package name that
    genuinely doesn't exist yet today may be registered for real next month, and a
    cached "doesn't exist" must not block that forever.
    """

    __tablename__ = "pypi_package_checks"

    #: PEP 503 normalized (lowercase, runs of -_. collapsed to a single -).
    name: Mapped[str] = mapped_column(String(255), primary_key=True)
    exists: Mapped[bool] = mapped_column(Boolean, nullable=False)
    first_release_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    checked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class Memory(Base):
    """A lesson the factory retains across sessions.

    Scoped so a conclusion about one product cannot silently steer another, and
    superseded rather than deleted so a wrong belief leaves an audit trail.
    """

    __tablename__ = "memories"

    id: Mapped[str] = _pk()
    scope: Mapped[str] = mapped_column(String(96), nullable=False, default="global")
    kind: Mapped[str] = mapped_column(String(32), nullable=False, default="lesson")
    content: Mapped[str] = mapped_column(Text, nullable=False)
    origin: Mapped[str] = mapped_column(String(64), nullable=False, default="unknown")
    source_task_id: Mapped[str | None] = mapped_column(String(32))
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.6)
    hits: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    superseded_reason: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (Index("ix_memories_scope", "scope", "superseded_at"),)


class LLMCall(Base):
    """Per-call cost and usage accounting, split by provider."""

    __tablename__ = "llm_calls"

    id: Mapped[str] = _pk()
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    model: Mapped[str] = mapped_column(String(100), nullable=False)
    task_type: Mapped[str] = mapped_column(String(32), nullable=False)
    engine: Mapped[str | None] = mapped_column(String(32))
    task_id: Mapped[str | None] = mapped_column(String(32))
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cache_read_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cache_write_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    outcome: Mapped[str] = mapped_column(String(32), nullable=False, default="ok")
    #: Computed once, at write time, from the price table *as of that call*. Never
    #: recomputed at read time — otherwise editing PRICE_TABLE would retroactively
    #: rewrite last month's spend history.
    cost_usd: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    #: Which prompt/weights version was live for this call, if any of the four
    #: learning-loop keys were involved. Null for every other call.
    tunable_parameter_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("tunable_parameters.id")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (Index("ix_llm_calls_created", "created_at", "provider"),)


class TunableParameter(Base):
    """A versioned prompt or scoring-weight value the factory is allowed to tune.

    Only ``factory.learning.store`` may write this table, and only for a key on
    ``factory.learning.allowlist.MUTABLE_KEYS`` — enforced in code, not just by this
    being the only module that imports the class for writes. ``key`` is always
    ``module.path:ATTRIBUTE_NAME``, never a bare attribute name: two unrelated
    constants in this codebase are both literally named ``SYSTEM_PROMPT``.
    """

    __tablename__ = "tunable_parameters"

    id: Mapped[str] = _pk()
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    key: Mapped[str] = mapped_column(String(200), nullable=False)
    value: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="candidate")
    target_metric: Mapped[str] = mapped_column(String(64), nullable=False)
    rationale: Mapped[str] = mapped_column(Text, nullable=False)
    created_by: Mapped[str] = mapped_column(String(64), nullable=False)
    parent_id: Mapped[str | None] = mapped_column(String(32), ForeignKey("tunable_parameters.id"))
    trial_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    trial_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    promoted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retired_reason: Mapped[str | None] = mapped_column(String(500))
    trial_numerator: Mapped[int | None] = mapped_column(Integer)
    trial_denominator: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        CheckConstraint("kind IN ('PROMPT','SCORING_WEIGHTS')", name="tunable_kind_valid"),
        CheckConstraint(
            "status IN ('candidate','trial','active','retired')", name="tunable_status_valid"
        ),
        UniqueConstraint("key", "version", name="uq_tunable_parameters_key_version"),
        Index("ix_tunable_parameters_key_status", "key", "status"),
        Index(
            "ux_tunable_parameters_active",
            "key",
            unique=True,
            postgresql_where=(status == "active"),
        ),
    )


class LearningEpisode(Base):
    """One Bernoulli outcome for a single governed call, tagged to the exact
    ``TunableParameter`` version that was live for it. This is what gives a trial's
    numerator/denominator a real, queryable population of *attempts* — not just the
    successes that happened to also get written to ``ideas``/``leads``."""

    __tablename__ = "learning_episodes"

    id: Mapped[str] = _pk()
    tunable_parameter_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("tunable_parameters.id"), nullable=False
    )
    outcome: Mapped[str] = mapped_column(String(16), nullable=False)
    detail: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    task_id: Mapped[str | None] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        CheckConstraint("outcome IN ('success','failure')", name="episode_outcome_valid"),
        Index("ix_learning_episodes_param", "tunable_parameter_id", "created_at"),
    )



class Decision(Base):
    """A judgement this company made, as a record rather than a log line.

    Release decisions were already rich -- the choice, the rationale, which MUSTs were
    unmet, which issues were deliberately accepted -- and they lived as JSONB inside a
    `task_events` row. Good evidence, no identity: nothing could point at one, so
    nothing could ever say "that decision turned out well". A company whose decisions
    are unaddressable cannot review them, and the recurring strategic review the end
    goal names has nothing to review.

    Deliberately wider than release: `kind` covers any judgement worth revisiting --
    what a product should do next, whether an idea clears compliance, what a design
    department commissioned. The question is always the same shape, so the record is.
    """

    __tablename__ = "decisions"

    id: Mapped[str] = _pk()
    #: What kind of judgement this was, e.g. `release`, `product_next`, `compliance`.
    #: Free text rather than an enum: a new kind of decision should cost an insert,
    #: not a migration.
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    #: What it was about -- `product`, `idea` -- following `requirements`' pattern.
    subject_kind: Mapped[str] = mapped_column(String(24), nullable=False)
    subject_id: Mapped[str] = mapped_column(String(32), nullable=False)
    #: The choice itself, in the vocabulary of that decision: `ship`, `rethink`,
    #: `deployment`, `approved`.
    choice: Mapped[str] = mapped_column(String(64), nullable=False)
    #: Why, in the decider's own words. Uncapped `Text` for the reason `Contract`
    #: gives: a truncated rationale is a lost one, and this is the field a human
    #: reads when asking whether the company is reasoning well.
    rationale: Mapped[str] = mapped_column(Text, nullable=False, default="")
    made_by_engine: Mapped[str] = mapped_column(String(32), nullable=False)
    #: The task this was decided in. The link to the causal chain: everything this
    #: task caused is downstream of this decision, which is how an outcome finds the
    #: decisions it is evidence about without anybody writing the link by hand.
    made_by_task_id: Mapped[str | None] = mapped_column(String(32))
    #: What else was available at the time. Without it a decision cannot be judged
    #: later -- "shipped" means nothing unless you know it could have rethought.
    alternatives: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    detail: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        Index("ix_decisions_subject", "subject_id", "created_at"),
        Index("ix_decisions_task", "made_by_task_id"),
        Index("ix_decisions_kind", "kind", "created_at"),
    )


class Outcome(Base):
    """Something that happened, with enough on it to be worth learning from.

    The generalisation of `LearningEpisode`, which was one Bernoulli result tied to one
    `TunableParameter` by a single non-null foreign key. That shape decided, silently,
    that every outcome teaches exactly one thing -- so a validation that failed because
    the *idea* was weak was recorded against the prompt that wrote the page, and the
    idea, the acceptance criteria, the design and the decision to ship it learned
    nothing. See `OutcomeEvidence` for the half that fixes it.

    `succeeded` stays because the trial statistics are Bernoulli and must keep working
    unchanged. `value_cents` is the half the end goal needs: profit is a magnitude, and
    a company that can only record whether something worked cannot tell a EUR 12 month
    from a EUR 1,200 one. Both are nullable -- an outcome may honestly have one, the
    other, or both.
    """

    __tablename__ = "outcomes"

    id: Mapped[str] = _pk()
    #: What kind of result this is: `trial_episode`, `validation`, `revenue`. Free
    #: text for the same reason `Decision.kind` is.
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    succeeded: Mapped[bool | None] = mapped_column(Boolean)
    #: Money, when there is any. Integer cents, like `ledger`.
    value_cents: Mapped[int | None] = mapped_column(Integer)
    subject_kind: Mapped[str | None] = mapped_column(String(24))
    subject_id: Mapped[str | None] = mapped_column(String(32))
    detail: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    #: Where this was observed. The entry point to the causal chain: the decisions
    #: this outcome is evidence about are the ones made in this task's ancestors.
    task_id: Mapped[str | None] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        Index("ix_outcomes_kind", "kind", "created_at"),
        Index("ix_outcomes_subject", "subject_id", "created_at"),
    )


class OutcomeEvidence(Base):
    """One outcome is evidence about one thing. There may be many rows per outcome.

    **This is the keystone.** Everything else in the learning loop was built and works;
    the single foreign key on `LearningEpisode` is what stopped it learning about
    anything but prompts, and therefore what stopped this company measuring the quality
    of a decision at all. Profit is a decision-quality metric, so a system that can
    only attribute outcomes to makers can optimise craft forever without ever noticing
    it is not earning.

    `subject_kind` carries **no check constraint, on purpose.** It starts as
    `parameter` and `decision`, and a later phase adding `code` or `workflow` as
    something this company may change about itself should cost an insert rather than a
    migration -- see ARCHITECTURE_ROADMAP.md, Phase 2.13.

    A caveat that belongs in the schema rather than only in the roadmap: this records
    that an outcome *is evidence about* a subject, never that the subject *caused* it.
    The links are derived by walking the causal chain, which credits every ancestor
    decision, and being upstream of a result is not the same as having produced it.
    """

    __tablename__ = "outcome_evidence"

    id: Mapped[str] = _pk()
    outcome_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("outcomes.id", ondelete="CASCADE"), nullable=False
    )
    subject_kind: Mapped[str] = mapped_column(String(24), nullable=False)
    subject_id: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint(
            "outcome_id", "subject_kind", "subject_id", name="uq_outcome_evidence_subject"
        ),
        Index("ix_outcome_evidence_subject", "subject_kind", "subject_id"),
    )


class TaskEvent(Base):
    """What happened, in order, and what caused it.

    The factory had data but no narrative. `heal_attempts`, `page_views`,
    `llm_calls` and `tasks.last_error` are rows a query can assemble into an answer
    *if someone already knows to ask* -- and `last_error` is overwritten on every
    attempt, so the earlier ones are simply gone. Diagnosing real incidents meant
    reconstructing them from inference across four tables, and the log lines that
    would have explained them were destroyed on the next deploy.

    `caused_by_task_id` is the column that makes this a trail rather than another
    log: it answers "what caused what", which no existing query can. A task that
    enqueues another records the parent, so a product's whole history -- review
    found a defect, which queued a patch, which exhausted its budget, which
    escalated -- reads as one ordered chain instead of four disconnected tables.

    Deliberately append-only and never read for a decision. This is the record
    (see ARCHITECTURE_ROADMAP.md's living-record principle), and a record that
    something branches on becomes a coupling nobody expects.
    """

    __tablename__ = "task_events"

    id: Mapped[str] = _pk()
    #: Nullable and no FK: events outlive the rows they describe (a task can be
    #: deleted during cleanup) and the trail losing its subject is worse than a
    #: dangling id.
    task_id: Mapped[str | None] = mapped_column(String(32))
    product_id: Mapped[str | None] = mapped_column(String(32))
    engine: Mapped[str | None] = mapped_column(String(32))
    #: Dotted, matching the structlog event names already used throughout
    #: (`engine.start`, `repair.budget_exhausted`), so the trail and the logs share
    #: one vocabulary rather than inventing a second.
    event: Mapped[str] = mapped_column(String(64), nullable=False)
    detail: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    caused_by_task_id: Mapped[str | None] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        Index("ix_task_events_product", "product_id", "created_at"),
        Index("ix_task_events_task", "task_id", "created_at"),
        Index("ix_task_events_caused_by", "caused_by_task_id"),
    )


class HealAttempt(Base):
    """Pairs one heal cycle's ``healing`` instructions with its eventual outcome.

    Audit-only: the two prompts that could act on this (codegen, Bug Council) are both
    permanently excluded from the mutation loop, so nothing reads this table to make a
    decision today — it exists so the gap ("did this specific fix actually work") is
    closeable by a human or a future maintenance item, instead of requiring a manual
    scan of ``tasks.payload`` blobs ordered by creation time.
    """

    __tablename__ = "heal_attempts"

    id: Mapped[str] = _pk()
    product_id: Mapped[str] = mapped_column(String(32), ForeignKey("products.id"), nullable=False)
    task_id: Mapped[str | None] = mapped_column(String(32), ForeignKey("tasks.id"))
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    healing_text: Mapped[str] = mapped_column(Text, nullable=False)
    failed_categories: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    outcome: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    #: How many blocking issues were open when this attempt was made. The anchor
    #: convergence is measured against: "did the last attempt reduce the blocking
    #: set" is a question about attempts, and reading it off the last *release
    #: decision* instead meant comparing the current state against itself, because
    #: `_verify_and_ship` writes that decision and then calls `_heal`, which
    #: re-read it. Nullable -- attempts predating this column have no figure, and
    #: treating "unknown" as 0 would manufacture the false signal it exists to
    #: remove.
    blocking_at_attempt: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(
            "outcome IN ('pending','healed','failed_again','escalated')",
            name="heal_attempt_outcome_valid",
        ),
        UniqueConstraint("product_id", "attempt_number", name="uq_heal_attempts_product_attempt"),
    )


class Issue(Base):
    """One defect, with an identity that survives being described differently.

    Before this table every finding was a first encounter: the reviewer reported a
    defect, `queue_review_patch` turned it into a `PATCH_PRODUCT` task carrying a
    text blob, the task went ``done``, and nothing linked any of it. ``done`` meant
    the patch *ran*, never that the symptom was gone. One broken checkout was filed
    four separate times over 24 days -- twice inside the same minute -- and was
    still broken when the fifth review found it.

    Unlike `TaskEvent`, this table is explicitly *read for decisions*: it is the
    backlog a department manager manages, and the thing that answers "is this new,
    or the one we have been failing to fix since August".
    """

    __tablename__ = "issues"

    id: Mapped[str] = _pk()
    product_id: Mapped[str | None] = mapped_column(String(32))
    #: Derived from what the defect *is*, never from how it was worded -- see
    #: `factory.issues.fingerprint_for`. Free text cannot carry identity here: an
    #: LLM never writes the same sentence twice even when nothing has changed,
    #: which is exactly how BUG_LOG #20's escalation dedup failed.
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    kind: Mapped[str] = mapped_column(String(24), nullable=False)
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    location: Mapped[str | None] = mapped_column(Text)
    expected: Mapped[str | None] = mapped_column(Text)
    actual: Mapped[str | None] = mapped_column(Text)
    evidence: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="open")
    found_by: Mapped[str | None] = mapped_column(String(32))
    #: A role, not a person. "Adding a role is a code change a human reviews", so a
    #: role name is the most specific owner this organisation has.
    assigned_to: Mapped[str | None] = mapped_column(String(32))
    #: No FK: a task can be cleaned up, and an issue outliving its task is better
    #: than a dangling constraint blocking the cleanup.
    fix_task_id: Mapped[str | None] = mapped_column(String(32))
    times_seen: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    resolution: Mapped[str | None] = mapped_column(Text)
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    fixed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: Separate from `fixed_at`, and the reason closure means anything. A patch
    #: completing sets `fixed`; only a later review failing to find the symptom
    #: sets `verified`. Nothing else may close an issue.
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    detail: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)

    __table_args__ = (
        CheckConstraint(
            "status IN ('open','triaged','fixing','fixed','verified','closed','wont_fix')",
            name="issue_status_valid",
        ),
        CheckConstraint(
            "severity IN ('critical','high','medium','low')", name="issue_severity_valid"
        ),
        UniqueConstraint("product_id", "fingerprint", name="uq_issues_product_fingerprint"),
    )


class Requirement(Base):
    """What a piece of work must achieve, and how anyone can tell it has.

    Nothing in this factory ever wrote this down. The landing-page prompt carries
    requirements, but generic ones -- the same form, beacon and SEO tags for every
    product ever generated. What *this* page must do for *this* idea existed
    nowhere, so every judgement downstream had nothing to judge against: Bug Council
    reviews categories rather than intent, AUDITOR infers the goal by looking at the
    product, and release was `if not deploy_blocking`.

    The `must`/`should`/`out_of_scope` split is what makes this more than a longer
    checklist. It is what lets a product be **done without being perfect** -- meets
    its must-haves, with known issues accepted deliberately -- which is a state the
    old gate could not express, because there is always another finding.

    Generic in subject rather than product-scoped: a repair task, a campaign and a
    discovery cycle all have "what is this for and when is it done", and a human
    team writes that down for each. Products are just the first subject.
    """

    __tablename__ = "requirements"

    id: Mapped[str] = _pk()
    subject_kind: Mapped[str] = mapped_column(String(24), nullable=False)
    subject_id: Mapped[str] = mapped_column(String(32), nullable=False)
    goal: Mapped[str] = mapped_column(Text, nullable=False)
    must: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list)
    should: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list)
    out_of_scope: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    created_by: Mapped[str | None] = mapped_column(String(64))
    source_task_id: Mapped[str | None] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    #: Superseded rather than rewritten, so "the old build was judged against a
    #: different standard" stays visible instead of being erased by the next version.
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(
            # 'design' is a real subject kind, not a special case: design guidelines
            # are a standard written before the work, which is what this table is
            # for. Omitting it here meant every guideline the Design department
            # produced was rejected by the database after being generated -- see
            # migration 0041.
            "subject_kind IN ('product','task','issue','campaign','design')",
            name="requirement_subject_kind_valid",
        ),
    )


class IdeaEvidence(Base):
    """The raw signals an idea was actually drawn from, kept.

    The opposite of `ScoutSignal` above, deliberately. That one is a hand-off
    buffer emptied the moment clustering reads it, so until this table existed
    nothing in the factory could see what real people had written about a problem
    once the Discovery cycle ended -- only a `source_url` and an integer survived.

    Two consequences, and they are the reason this exists:

    - An audience brief had nothing to be built from. Generating one anyway means
      inventing the customer from the idea's own generated prose and then trusting
      it, which is worse than having none.
    - `evidence_count` could be displayed but never judged. Every idea this factory
      has produced carries `evidence_count = 2`, and the only gate anywhere is
      `if evidence_count == 0` -- so two comments on one GitHub thread cleared the
      bar to build a product as easily as two hundred would have.

    `ondelete="CASCADE"`: this is evidence *for* an idea and has no meaning without
    it.
    """

    __tablename__ = "idea_evidence"

    id: Mapped[str] = _pk()
    idea_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("ideas.id", ondelete="CASCADE"), nullable=False, index=True
    )
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    #: What somebody actually wrote. `Text`, never a bounded `String` -- truncating
    #: a complaint to fit a column loses exactly the specific half that made it
    #: worth keeping. Readers truncate at the point of use.
    body: Mapped[str] = mapped_column(Text, nullable=False)
    url: Mapped[str] = mapped_column(String(500), nullable=False)
    source: Mapped[str] = mapped_column(String(60), nullable=False)
    engagement: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Question(Base):
    """One department asking another something it cannot answer for itself.

    The counterpart to `Interrupt`, which is the same shape addressed to a human.
    Keeping them separate rather than adding a recipient column to interrupts is
    deliberate: an interrupt carries human-delivery machinery (Telegram, reminders,
    a reason vocabulary the person recognises) that a department-to-department
    question has no use for, and an unanswered question must not read as an
    unanswered escalation on someone's list.

    `blocking_task_id` is what makes this work rather than correspondence. A
    question nobody is blocked on is a note; a question that resumes nothing when
    answered is a conversation the factory cannot act on.
    """

    __tablename__ = "questions"

    id: Mapped[str] = _pk()
    asked_by_engine: Mapped[str] = mapped_column(String(32), nullable=False)
    asked_by_task_id: Mapped[str | None] = mapped_column(String(32))
    #: The work waiting on this. Deferred rather than failed while the question is
    #: open, and released the moment it is answered.
    blocking_task_id: Mapped[str | None] = mapped_column(String(32))
    product_id: Mapped[str | None] = mapped_column(String(32))
    #: An engine name routes into that department's queue; `human` falls back to the
    #: interrupt path. The fallback is what makes this useful before any department
    #: can answer -- a surfaced question beats a silent retry.
    asked_of: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    question: Mapped[str] = mapped_column(Text, nullable=False)
    context: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="open")
    answer: Mapped[str | None] = mapped_column(Text)
    answered_by_task_id: Mapped[str | None] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    answered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
