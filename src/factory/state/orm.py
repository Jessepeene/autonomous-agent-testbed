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
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("fingerprint", name="uq_ideas_fingerprint"),
        Index("ix_ideas_queue", "status", "score"),
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
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    __table_args__ = (Index("ix_products_status", "status"),)


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
    lead_id: Mapped[str] = mapped_column(ForeignKey("leads.id"), nullable=False)
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


class Interrupt(Base):
    """A question for the human. Persisted so an unanswered escalation survives reboot."""

    __tablename__ = "interrupts"

    id: Mapped[str] = _pk()
    reason: Mapped[str] = mapped_column(String(32), nullable=False)
    subject: Mapped[str] = mapped_column(String(200), nullable=False)
    detail: Mapped[str] = mapped_column(Text, nullable=False)
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
        UniqueConstraint("reason", "subject", "status", name="uq_interrupts_open_subject"),
        Index("ix_interrupts_status", "status", "delivered"),
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
