"""Task events: what happened, in order, and what caused it.

Revision ID: 0032_task_events
Revises: 0031_outreach_sender_identity
Create Date: 2026-09-13

The factory had data but no narrative. `heal_attempts`, `page_views`, `llm_calls`
and `tasks.last_error` are rows a query can assemble into an answer if someone
already knows to ask -- and `last_error` is overwritten on every attempt, so
earlier failures are simply gone. Two days of real incidents were diagnosed by
inference across four tables, with the log lines that would have explained them
destroyed by the next deploy.

`caused_by_task_id` is the point: it answers "what caused what", which no existing
query can.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0032_task_events"
down_revision: str | None = "0031_outreach_sender_identity"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE task_events (
            id VARCHAR(32) NOT NULL,
            task_id VARCHAR(32),
            product_id VARCHAR(32),
            engine VARCHAR(32),
            event VARCHAR(64) NOT NULL,
            detail JSONB NOT NULL DEFAULT '{}'::jsonb,
            caused_by_task_id VARCHAR(32),
            created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
            CONSTRAINT pk_task_events PRIMARY KEY (id)
        )
        """
    )
    # No foreign keys on task_id/product_id by design: events outlive the rows they
    # describe (cleanup deletes tasks), and a trail that loses its subject is worse
    # than a dangling id.
    op.execute("CREATE INDEX ix_task_events_product ON task_events (product_id, created_at)")
    op.execute("CREATE INDEX ix_task_events_task ON task_events (task_id, created_at)")
    op.execute("CREATE INDEX ix_task_events_caused_by ON task_events (caused_by_task_id)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS task_events CASCADE")
