"""Ledger product attribution.

Revision ID: 0004_ledger_product_attribution
Revises: 0003_llm_call_cost
Create Date: 2026-08-08

``ledger.product_id`` existed since the baseline schema but was never populated on
either revenue write path — structurally present, functionally dead. This adds the FK
and index that make it a real, queryable attribution column now that
``finance/webhooks.py`` actually populates it. Nullable throughout: an unattributed
event (a product deployed before this contract existed, or a payment type that never
carries a reference) is routine, not an error, and must never block revenue recording.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0004_ledger_product_attribution"
down_revision: str | None = "0003_llm_call_cost"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE ledger ADD CONSTRAINT fk_ledger_product_id_products "
        "FOREIGN KEY (product_id) REFERENCES products(id)"
    )
    op.execute("CREATE INDEX ix_ledger_product ON ledger (product_id)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_ledger_product")
    op.execute("ALTER TABLE ledger DROP CONSTRAINT IF EXISTS fk_ledger_product_id_products")
