"""Scout-signal hand-off buffer for DISCOVERY_SCOUT.

Revision ID: 0016_scout_signals
Revises: 0015_custom_domains
Create Date: 2026-08-12

A pure hand-off buffer, not a durable record -- see `state/orm.py::ScoutSignal`.
DISCOVERY_SCOUT's `submit_signal` tool writes one row per finding during an
agent run; the same Discovery cycle reads every row for that `task_id` back out
and deletes them before clustering. Empty between cycles by design.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0016_scout_signals"
down_revision: str | None = "0015_custom_domains"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE scout_signals (
            id VARCHAR(32) NOT NULL,
            task_id VARCHAR(32) NOT NULL,
            title VARCHAR(300) NOT NULL,
            body TEXT NOT NULL,
            url VARCHAR(500) NOT NULL,
            source VARCHAR(60) NOT NULL,
            engagement INTEGER NOT NULL DEFAULT 0,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT pk_scout_signals PRIMARY KEY (id)
        )
        """
    )
    op.execute("CREATE INDEX ix_scout_signals_task ON scout_signals (task_id)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS scout_signals")
