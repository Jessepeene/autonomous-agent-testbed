"""Product attribution on interrupts.

Revision ID: 0008_interrupt_product_id
Revises: 0007_deliverability
Create Date: 2026-08-10

A heal-cycle-exhaustion escalation (``development/graph.py``) has no ``task_id`` --
the product was deliberately reset to ``PLANNED`` instead of leaving one task to
retry, so there was never a task to link. Without a ``product_id`` either, a human's
"reply with guidance, or REJECT" had no way to be traced back to the product it was
about. Bare column, no FK, matching ``task_id``'s existing pattern on this table.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0008_interrupt_product_id"
down_revision: str | None = "0007_deliverability"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE interrupts ADD COLUMN product_id VARCHAR(32)")


def downgrade() -> None:
    op.execute("ALTER TABLE interrupts DROP COLUMN IF EXISTS product_id")
