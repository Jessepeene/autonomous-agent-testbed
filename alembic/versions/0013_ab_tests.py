"""Live AB testing for landing pages.

Revision ID: 0013_ab_tests
Revises: 0012_page_views
Create Date: 2026-08-11

The Learning Loop's existing prompt trials (learning/trials.py) are sequential:
one version goes fully live for up to 30 days, then gets compared to *historical*
episodes. This is a genuinely different mechanism -- two variants served
concurrently to different visitors of the same product, compared against each
other over the same time window via a two-sample test (see
development/ab_testing.py, reusing learning/stats.py's existing
fisher_exact_less -- the underlying hypergeometric test is identical whether the
two groups are "trial vs. history" or "variant A vs. variant B").

`variant` on validation_signals mirrors what page_views already carries (added in
migration 0012) -- both need it to tally per-arm outcomes.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0013_ab_tests"
down_revision: str | None = "0012_page_views"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE ab_tests (
            id VARCHAR(32) NOT NULL,
            product_id VARCHAR(32) NOT NULL,
            variant_a_brief TEXT NOT NULL,
            variant_b_brief TEXT NOT NULL,
            status VARCHAR(16) NOT NULL DEFAULT 'running',
            winner VARCHAR(16),
            started_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
            concluded_at TIMESTAMP WITH TIME ZONE,
            CONSTRAINT pk_ab_tests PRIMARY KEY (id),
            CONSTRAINT fk_ab_tests_product_id_products
                FOREIGN KEY(product_id) REFERENCES products (id)
        )
        """
    )
    op.execute("CREATE INDEX ix_ab_tests_product ON ab_tests (product_id)")
    # At most one *running* test per product -- a partial unique index rather than
    # a plain unique constraint, since many concluded tests for the same product
    # over time are expected and fine.
    op.execute(
        "CREATE UNIQUE INDEX uq_ab_tests_one_running_per_product "
        "ON ab_tests (product_id) WHERE status = 'running'"
    )
    op.execute("ALTER TABLE validation_signals ADD COLUMN variant VARCHAR(32)")


def downgrade() -> None:
    op.execute("ALTER TABLE validation_signals DROP COLUMN IF EXISTS variant")
    op.execute("DROP TABLE IF EXISTS ab_tests CASCADE")
