"""Add Product.design_brief: a cached, per-product visual design summary.

Revision ID: 0030_product_design_brief
Revises: 0029_custom_credentials
Create Date: 2026-08-24

The dashboard's product detail view wants to show "visual design
guidelines" for a shipped product, but nothing generates or stores that
anywhere. Generated once, from the real deployed page (not a guess from the
idea text) -- see `development/design_brief.py::build_design_brief` -- and
cached here, the same shape as `lead_qualification_brief`.

Nullable and NULL by default: every existing product predates this feature
and gets one lazily (either automatically on its next real ship, or on
demand from the dashboard for a product that's already live).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0030_product_design_brief"
down_revision: str | None = "0029_custom_credentials"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("products", sa.Column("design_brief", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("products", "design_brief")
