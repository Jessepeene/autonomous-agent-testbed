"""What kind of thing a product currently is, on the product itself.

Revision ID: 0035_product_build_kind
Revises: 0034_requirements
Create Date: 2026-09-14

`_verify_and_ship` took `is_validation` as a parameter, and two of its four call
sites never passed it. Both are the repair paths. So a landing page verified
cleanly when it was built, and the moment it needed *any* repair it was
re-verified as though it were a full MVP -- at which point the codegen contract
demanded STRIPE_API_KEY, PRODUCT_PRICE_CENTS and client_reference_id on a page
whose own requirements list payment as out of scope.

The repair agent then did as it was told and built a checkout. That checkout had
no product behind it, so it returned 422; a reviewer found the broken checkout; a
repair was commissioned to fix it; and that ran for 25 days. The product became an
MVP because the repair gate kept insisting it was one.

A parameter that four call sites must remember is a fact waiting to be dropped, and
it was dropped. This moves it onto the product, where the gate reads it directly
and no call site can forget it.

`validation` is the default because it is where every product genuinely starts:
idea -> landing page -> real interest -> build_mvp. A product only becomes `mvp`
when `_build_mvp` actually runs, which in this factory's whole history has never
happened -- so backfilling every existing row to `validation` is not a guess, it is
the truth.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0035_product_build_kind"
down_revision: str | None = "0034_requirements"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "products",
        sa.Column(
            "build_kind",
            sa.String(length=16),
            nullable=False,
            server_default="validation",
        ),
    )
    op.create_check_constraint(
        "product_build_kind_valid",
        "products",
        "build_kind IN ('validation','mvp')",
    )


def downgrade() -> None:
    op.drop_constraint("product_build_kind_valid", "products", type_="check")
    op.drop_column("products", "build_kind")
