"""Add Product.lead_qualification_brief: a cached, per-product ICP definition.

Revision ID: 0018_lead_brief
Revises: 0017_interrupt_open_subject
Create Date: 2026-08-13

The web-search-driven lead provider needs genuinely narrow, per-product
qualification criteria (positive criteria + strict exclusions), not just a loose
product description -- the same precision a founder would hand-write for one fixed
audience, but generated once per product instead. Generating it fresh on every
harvest call would waste an LLM call on something that doesn't change call to call,
so it is generated once (`leads.py::build_qualification_brief`) and cached here.

Nullable and NULL by default: most existing products have never gone through this
provider and never need the column populated.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0018_lead_brief"
down_revision: str | None = "0017_interrupt_open_subject"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("products", sa.Column("lead_qualification_brief", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("products", "lead_qualification_brief")
