"""Deliverability circuit breaker and bounce/complaint tracking.

Revision ID: 0007_deliverability
Revises: 0006_campaign_gate_and_entity
Create Date: 2026-08-09

Raw SMTP from a primary domain has no bounce/complaint feedback loop, so nothing
tells the factory it has a reputation problem until it's already blacklisted. This
adds the tracking columns a deliverability API's webhook populates
(``outreach_log.bounced_at``/``complained_at``) and the circuit breaker's persisted
state (``marketing_freezes``) — see ``engines/marketing/deliverability.py``.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0007_deliverability"
down_revision: str | None = "0006_campaign_gate_and_entity"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE outreach_log ADD COLUMN bounced_at TIMESTAMP WITH TIME ZONE")
    op.execute("ALTER TABLE outreach_log ADD COLUMN complained_at TIMESTAMP WITH TIME ZONE")

    op.execute(
        """
        CREATE TABLE marketing_freezes (
            id VARCHAR(32) NOT NULL,
            reason TEXT NOT NULL,
            complaint_rate FLOAT NOT NULL,
            status VARCHAR(16) NOT NULL DEFAULT 'active',
            interrupt_id VARCHAR(32),
            created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
            cleared_at TIMESTAMP WITH TIME ZONE,
            CONSTRAINT pk_marketing_freezes PRIMARY KEY (id),
            CONSTRAINT ck_marketing_freezes_marketing_freeze_status_valid
                CHECK (status IN ('active','cleared')),
            CONSTRAINT fk_marketing_freezes_interrupt_id_interrupts
                FOREIGN KEY(interrupt_id) REFERENCES interrupts (id)
        )
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX ux_marketing_freezes_active ON marketing_freezes (status) "
        "WHERE status = 'active'"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS marketing_freezes CASCADE")
    op.execute("ALTER TABLE outreach_log DROP COLUMN IF EXISTS complained_at")
    op.execute("ALTER TABLE outreach_log DROP COLUMN IF EXISTS bounced_at")
