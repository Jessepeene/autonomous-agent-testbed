"""Give the signup journey a place a product can own.

The confirmation email and the page its link leads to have been byte-identical
constants in `api/app.py` since this factory started shipping, for every product it
has ever made. A pre-ship reviewer can now read the mail it sent and follow the
confirmation link to the page behind it (Phase 2.9's "seeing" half) -- and had
nothing to change on either, because neither belongs to any product. Run 15's
reviewer opened that exact page, screenshotted it, and reported nothing, because it
had no lever.

`Product.signup_journey` is that lever: a small JSONB override, empty until a
reviewer actually flags the generic version as a problem (Phase 2.9's remaining
half, ARCHITECTURE_ROADMAP.md). Nothing renders differently for a single product
until this migration's default value stops being `{}`.

Deliberately does not cover `/u/{token}` (GDPR erasure). That page's job is to be
correct, not persuasive, and is not scoped to one product at all.

Revision ID: 0045_signup_journey
Revises: 0044_decisions_and_outcomes
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0045_signup_journey"
down_revision = "0044_decisions_and_outcomes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "products",
        sa.Column(
            "signup_journey",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="{}",
        ),
    )


def downgrade() -> None:
    op.drop_column("products", "signup_journey")
