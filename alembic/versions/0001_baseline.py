"""Baseline schema.

Revision ID: 0001_baseline
Revises:
Create Date: 2026-08-07

This DDL is **frozen**: it is the schema as it stood at the baseline, written out
explicitly rather than generated from ``Base.metadata`` at run time.

That distinction matters. A baseline that calls ``Base.metadata.create_all()`` builds
whatever the ORM says *today*, so a fresh database would be created already carrying
columns that a later migration then tries to add — and every new install breaks on
the second migration while existing ones stay fine. Freezing the DDL means 0001 always
produces the same database, and later revisions apply cleanly on top of it.

Regenerating this file is therefore wrong. Schema changes go in a new revision.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0001_baseline"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
CREATE TABLE ideas (
	id VARCHAR(32) NOT NULL, 
	problem_statement TEXT NOT NULL, 
	target_niche VARCHAR(200) NOT NULL, 
	source_url VARCHAR(2000) NOT NULL, 
	monetization_hypothesis TEXT NOT NULL, 
	score FLOAT NOT NULL, 
	evidence_count INTEGER NOT NULL, 
	status VARCHAR(32) NOT NULL, 
	fingerprint VARCHAR(64) NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	CONSTRAINT pk_ideas PRIMARY KEY (id), 
	CONSTRAINT uq_ideas_fingerprint UNIQUE (fingerprint)
)
        """
    )

    op.execute(
        """
CREATE INDEX ix_ideas_queue ON ideas (status, score)
        """
    )

    op.execute(
        """
CREATE TABLE interrupts (
	id VARCHAR(32) NOT NULL, 
	reason VARCHAR(32) NOT NULL, 
	subject VARCHAR(200) NOT NULL, 
	detail TEXT NOT NULL, 
	task_id VARCHAR(32), 
	status VARCHAR(32) NOT NULL, 
	delivered BOOLEAN NOT NULL, 
	response TEXT, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	resolved_at TIMESTAMP WITH TIME ZONE, 
	CONSTRAINT pk_interrupts PRIMARY KEY (id), 
	CONSTRAINT uq_interrupts_open_subject UNIQUE (reason, subject, status)
)
        """
    )

    op.execute(
        """
CREATE INDEX ix_interrupts_status ON interrupts (status, delivered)
        """
    )

    op.execute(
        """
CREATE TABLE leads (
	id VARCHAR(32) NOT NULL, 
	first_name VARCHAR(100) NOT NULL, 
	last_name VARCHAR(100) NOT NULL, 
	job_title VARCHAR(200) NOT NULL, 
	company VARCHAR(200) NOT NULL, 
	work_email VARCHAR(320) NOT NULL, 
	source_url VARCHAR(2000) NOT NULL, 
	scraped_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	provenance_kind VARCHAR(32) NOT NULL, 
	provider VARCHAR(64) NOT NULL, 
	lia_passed BOOLEAN NOT NULL, 
	lia_justification TEXT, 
	product_id VARCHAR(32), 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	CONSTRAINT pk_leads PRIMARY KEY (id), 
	CONSTRAINT uq_leads_work_email UNIQUE (work_email), 
	CONSTRAINT ck_leads_source_url_present CHECK (length(source_url) > 7)
)
        """
    )

    op.execute(
        """
CREATE INDEX ix_leads_domain ON leads (company)
        """
    )

    op.execute(
        """
CREATE TABLE ledger (
	id VARCHAR(32) NOT NULL, 
	external_event_id VARCHAR(200) NOT NULL, 
	kind VARCHAR(32) NOT NULL, 
	amount_cents INTEGER NOT NULL, 
	balance_cents INTEGER NOT NULL, 
	reinvest_pool_cents INTEGER NOT NULL, 
	currency VARCHAR(3) NOT NULL, 
	product_id VARCHAR(32), 
	memo VARCHAR(500), 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	CONSTRAINT pk_ledger PRIMARY KEY (id), 
	CONSTRAINT uq_ledger_external_event_id UNIQUE (external_event_id), 
	CONSTRAINT ck_ledger_balance_non_negative CHECK (balance_cents >= 0), 
	CONSTRAINT ck_ledger_reinvest_pool_non_negative CHECK (reinvest_pool_cents >= 0)
)
        """
    )

    op.execute(
        """
CREATE INDEX ix_ledger_created ON ledger (created_at)
        """
    )

    op.execute(
        """
CREATE TABLE llm_calls (
	id VARCHAR(32) NOT NULL, 
	provider VARCHAR(32) NOT NULL, 
	model VARCHAR(100) NOT NULL, 
	task_type VARCHAR(32) NOT NULL, 
	engine VARCHAR(32), 
	task_id VARCHAR(32), 
	input_tokens INTEGER NOT NULL, 
	output_tokens INTEGER NOT NULL, 
	cache_read_tokens INTEGER NOT NULL, 
	cache_write_tokens INTEGER NOT NULL, 
	latency_ms INTEGER NOT NULL, 
	outcome VARCHAR(32) NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	CONSTRAINT pk_llm_calls PRIMARY KEY (id)
)
        """
    )

    op.execute(
        """
CREATE INDEX ix_llm_calls_created ON llm_calls (created_at, provider)
        """
    )

    op.execute(
        """
CREATE TABLE tasks (
	id VARCHAR(32) NOT NULL, 
	kind VARCHAR(64) NOT NULL, 
	status VARCHAR(32) NOT NULL, 
	engine VARCHAR(32) NOT NULL, 
	priority INTEGER NOT NULL, 
	payload JSONB NOT NULL, 
	idea_id VARCHAR(32), 
	product_id VARCHAR(32), 
	idempotency_key VARCHAR(200) NOT NULL, 
	attempt_count INTEGER NOT NULL, 
	claimed_by VARCHAR(100), 
	lease_expires_at TIMESTAMP WITH TIME ZONE, 
	not_before TIMESTAMP WITH TIME ZONE, 
	last_error TEXT, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	CONSTRAINT pk_tasks PRIMARY KEY (id), 
	CONSTRAINT uq_tasks_idempotency_key UNIQUE (idempotency_key)
)
        """
    )

    op.execute(
        """
CREATE INDEX ix_tasks_engine_status ON tasks (engine, status)
        """
    )

    op.execute(
        """
CREATE INDEX ix_tasks_ready ON tasks (status, priority, not_before)
        """
    )

    op.execute(
        """
CREATE TABLE outreach_log (
	id VARCHAR(32) NOT NULL, 
	lead_id VARCHAR(32) NOT NULL, 
	product_id VARCHAR(32), 
	channel VARCHAR(32) NOT NULL, 
	subject VARCHAR(500) NOT NULL, 
	status VARCHAR(32) NOT NULL, 
	suppressed_reason VARCHAR(200), 
	unsubscribe_token VARCHAR(64) NOT NULL, 
	sent_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	CONSTRAINT pk_outreach_log PRIMARY KEY (id), 
	CONSTRAINT fk_outreach_log_lead_id_leads FOREIGN KEY(lead_id) REFERENCES leads (id)
)
        """
    )

    op.execute(
        """
CREATE INDEX ix_outreach_lead ON outreach_log (lead_id, sent_at)
        """
    )

    op.execute(
        """
CREATE TABLE products (
	id VARCHAR(32) NOT NULL, 
	name VARCHAR(200) NOT NULL, 
	idea_id VARCHAR(32) NOT NULL, 
	status VARCHAR(32) NOT NULL, 
	workspace_path VARCHAR(1000), 
	deploy_url VARCHAR(1000), 
	heal_attempts INTEGER NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	CONSTRAINT pk_products PRIMARY KEY (id), 
	CONSTRAINT fk_products_idea_id_ideas FOREIGN KEY(idea_id) REFERENCES ideas (id)
)
        """
    )

    op.execute(
        """
CREATE INDEX ix_products_status ON products (status)
        """
    )

    op.execute(
        """
CREATE TABLE product_locks (
	product_id VARCHAR(32) NOT NULL, 
	holder VARCHAR(64) NOT NULL, 
	reason VARCHAR(200) NOT NULL, 
	blocks_paid_spend BOOLEAN NOT NULL, 
	acquired_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	expires_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	CONSTRAINT pk_product_locks PRIMARY KEY (product_id), 
	CONSTRAINT fk_product_locks_product_id_products FOREIGN KEY(product_id) REFERENCES products (id)
)
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS product_locks CASCADE")
    op.execute("DROP TABLE IF EXISTS products CASCADE")
    op.execute("DROP TABLE IF EXISTS outreach_log CASCADE")
    op.execute("DROP TABLE IF EXISTS tasks CASCADE")
    op.execute("DROP TABLE IF EXISTS llm_calls CASCADE")
    op.execute("DROP TABLE IF EXISTS ledger CASCADE")
    op.execute("DROP TABLE IF EXISTS leads CASCADE")
    op.execute("DROP TABLE IF EXISTS interrupts CASCADE")
    op.execute("DROP TABLE IF EXISTS ideas CASCADE")
