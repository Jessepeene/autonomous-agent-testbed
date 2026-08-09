"""Stored cost on llm_calls.

Revision ID: 0003_llm_call_cost
Revises: 0002_memories
Create Date: 2026-08-08

Cost was previously always recomputed at read time from the live ``PRICE_TABLE``, so
editing a price retroactively rewrote every historical number. Storing it once, at
write time, makes historical spend a fact rather than a function of today's prices.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0003_llm_call_cost"
down_revision: str | None = "0002_memories"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE llm_calls ADD COLUMN cost_usd DOUBLE PRECISION NOT NULL DEFAULT 0")


def downgrade() -> None:
    op.execute("ALTER TABLE llm_calls DROP COLUMN IF EXISTS cost_usd")
