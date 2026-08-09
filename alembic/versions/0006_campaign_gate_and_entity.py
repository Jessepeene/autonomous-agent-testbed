"""Top-level campaign approval gate and entity-resolution cache.

Revision ID: 0006_campaign_gate_and_entity
Revises: 0005_learning_loop
Create Date: 2026-08-09

Two independent, unrelated tables landing together because both are prerequisites for
the same feature set: ``campaign_approvals`` is the durable state behind the mandatory
human sign-off before any outbound campaign opens the SMTP transport
(``marketing/campaign.py``), and ``entity_verdicts`` caches the agent-driven judgement
of whether a lead's company domain is a registered corporate entity
(``compliance/entity.py``) so the same domain is not re-resolved on every harvest.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0006_campaign_gate_and_entity"
down_revision: str | None = "0005_learning_loop"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE campaign_approvals (
            id VARCHAR(32) NOT NULL,
            product_id VARCHAR(32),
            task_id VARCHAR(32) NOT NULL,
            lead_count INTEGER NOT NULL,
            brief TEXT NOT NULL,
            status VARCHAR(16) NOT NULL DEFAULT 'pending',
            interrupt_id VARCHAR(32),
            created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
            decided_at TIMESTAMP WITH TIME ZONE,
            CONSTRAINT pk_campaign_approvals PRIMARY KEY (id),
            CONSTRAINT ck_campaign_approvals_campaign_approval_status_valid
                CHECK (status IN ('pending','approved','rejected')),
            CONSTRAINT uq_campaign_approvals_task_id UNIQUE (task_id),
            CONSTRAINT fk_campaign_approvals_product_id_products
                FOREIGN KEY(product_id) REFERENCES products (id),
            CONSTRAINT fk_campaign_approvals_task_id_tasks
                FOREIGN KEY(task_id) REFERENCES tasks (id),
            CONSTRAINT fk_campaign_approvals_interrupt_id_interrupts
                FOREIGN KEY(interrupt_id) REFERENCES interrupts (id)
        )
        """
    )
    op.execute("CREATE INDEX ix_campaign_approvals_status ON campaign_approvals (status)")

    op.execute(
        """
        CREATE TABLE entity_verdicts (
            id VARCHAR(32) NOT NULL,
            email_domain VARCHAR(255) NOT NULL,
            entity_type VARCHAR(32) NOT NULL,
            confidence FLOAT NOT NULL,
            evidence TEXT NOT NULL,
            task_id VARCHAR(32),
            resolved_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
            CONSTRAINT pk_entity_verdicts PRIMARY KEY (id),
            CONSTRAINT ck_entity_verdicts_entity_verdict_type_valid
                CHECK (entity_type IN ('corporate','sole_proprietor_or_individual','unknown')),
            CONSTRAINT uq_entity_verdicts_email_domain UNIQUE (email_domain)
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS entity_verdicts CASCADE")
    op.execute("DROP INDEX IF EXISTS ix_campaign_approvals_status")
    op.execute("DROP TABLE IF EXISTS campaign_approvals CASCADE")
