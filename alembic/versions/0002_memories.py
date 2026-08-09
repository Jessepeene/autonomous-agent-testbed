"""Cross-session memory.

Revision ID: 0002_memories
Revises: 0001_baseline
Create Date: 2026-08-07

Written as explicit DDL for the same reason the baseline is: a revision that derives
its own statements from ``Base.metadata`` describes whatever the ORM says at the
moment it runs, not what it was written to do.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0002_memories"
down_revision: str | None = "0001_baseline"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE memories (
            id VARCHAR(32) NOT NULL,
            scope VARCHAR(96) NOT NULL,
            kind VARCHAR(32) NOT NULL,
            content TEXT NOT NULL,
            origin VARCHAR(64) NOT NULL,
            source_task_id VARCHAR(32),
            confidence FLOAT NOT NULL,
            hits INTEGER NOT NULL,
            superseded_at TIMESTAMP WITH TIME ZONE,
            superseded_reason VARCHAR(500),
            created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
            last_used_at TIMESTAMP WITH TIME ZONE,
            CONSTRAINT pk_memories PRIMARY KEY (id)
        )
        """
    )
    op.execute("CREATE INDEX ix_memories_scope ON memories (scope, superseded_at)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS memories CASCADE")
