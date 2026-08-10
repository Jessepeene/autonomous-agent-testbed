"""Validation signals: interest captured before any MVP is built.

Revision ID: 0009_validation_signals
Revises: 0008_interrupt_product_id
Create Date: 2026-08-10

Deliberately not lead-attributed -- matches the existing product-level-only
revenue-attribution philosophy in ``marketing/attribution.py`` (Stripe never
reveals which lead paid; a landing-page form submission has the same problem).
Unique on (product_id, email) so repeat submissions from the same address can
never inflate the count a build-or-scrap decision is gated on.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0009_validation_signals"
down_revision: str | None = "0008_interrupt_product_id"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Durable link from a product back to the exact Learning-Loop version of
    # LANDING_PAGE_SYSTEM that built its validation page -- EVALUATE_VALIDATION
    # (run later, after HARVEST_LEADS/SEND_OUTREACH have both intervened) needs
    # this to credit the right tunable_parameter_id. A Product column, not
    # payload-threading through two marketing-engine tasks that have nothing to do
    # with codegen, is the simple, durable source of truth.
    op.execute("ALTER TABLE products ADD COLUMN landing_page_prompt_version VARCHAR(32)")
    op.execute(
        """
        CREATE TABLE validation_signals (
            id VARCHAR(32) NOT NULL,
            product_id VARCHAR(32) NOT NULL,
            email VARCHAR(320) NOT NULL,
            message TEXT,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
            CONSTRAINT pk_validation_signals PRIMARY KEY (id),
            CONSTRAINT fk_validation_signals_product_id_products
                FOREIGN KEY(product_id) REFERENCES products (id),
            CONSTRAINT uq_validation_signals_product_email UNIQUE (product_id, email)
        )
        """
    )
    op.execute("CREATE INDEX ix_validation_signals_product ON validation_signals (product_id)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS validation_signals CASCADE")
    op.execute("ALTER TABLE products DROP COLUMN IF EXISTS landing_page_prompt_version")
