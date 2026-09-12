"""Integration alerts: usage-limit/quota/account signals from third-party
providers (Hunter, Apollo, ...), surfaced on the dashboard's Integrations page.

Revision ID: 0027_integration_alerts
Revises: 0026_scrape_proxy
Create Date: 2026-08-23

Deliberately a separate table from `interrupts`, not a new `InterruptReason`:
a quota hit needs no reply and blocks nothing (see leads.py's existing
graceful-degrade handling), it only needs visibility. Same partial-unique-
index shape as `uq_interrupts_open_subject`
(0017_interrupt_open_subject_partial.py) and for the identical reason: a
provider that keeps hitting the same limit on retry must collapse into one
open row, not spam a new one every time, while a resolved occurrence must
never block a later, genuinely new one from opening.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0027_integration_alerts"
down_revision: str | None = "0026_scrape_proxy"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "integration_alerts",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="open"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("resolved_at", sa.DateTime(timezone=True)),
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_integration_alerts_open_provider_message ON "
        "integration_alerts (provider, message, status) WHERE status = 'open'"
    )
    op.create_index("ix_integration_alerts_status", "integration_alerts", ["status"])


def downgrade() -> None:
    op.drop_index("ix_integration_alerts_status", table_name="integration_alerts")
    op.execute("DROP INDEX IF EXISTS uq_integration_alerts_open_provider_message")
    op.drop_table("integration_alerts")
