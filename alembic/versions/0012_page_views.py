"""Page views: a self-hosted analytics beacon for every deployed product.

Revision ID: 0012_page_views
Revises: 0011_product_test_command
Create Date: 2026-08-11

Nothing collected traffic data for a live or validating product before this --
`ValidationSignal` records interest (an email), not visits. Growth decisions
(is this landing page converting, does this product's live traffic justify an
SEO or AB-testing pass) need a volume denominator that didn't exist.

A third-party analytics provider's query API contract can't be verified from
inside this codebase and its own current shape is an unknown risk -- this reuses
the pattern already established for `/interest/{product_id}` -> `validation_signals`
instead: an unauthenticated public beacon endpoint the product's own generated
frontend calls, landing directly in a table this factory fully controls.

`variant` is nullable and unused until the live AB-testing phase -- added now so
that phase does not need a second migration touching this table.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0012_page_views"
down_revision: str | None = "0011_product_test_command"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE page_views (
            id VARCHAR(32) NOT NULL,
            product_id VARCHAR(32) NOT NULL,
            path VARCHAR(400) NOT NULL,
            referrer VARCHAR(400),
            variant VARCHAR(32),
            created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
            CONSTRAINT pk_page_views PRIMARY KEY (id),
            CONSTRAINT fk_page_views_product_id_products
                FOREIGN KEY(product_id) REFERENCES products (id)
        )
        """
    )
    op.execute("CREATE INDEX ix_page_views_product ON page_views (product_id)")
    op.execute("CREATE INDEX ix_page_views_product_created ON page_views (product_id, created_at)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS page_views CASCADE")
