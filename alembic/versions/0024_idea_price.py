"""Persist the discriminator's self-serve price target on ideas.

Revision ID: 0024_idea_price
Revises: 0023_idea_defensibility
Create Date: 2026-08-14

`self_serve_price_usd` was already reasoned about (it gates the PLG
constraint, it's part of what the discriminator scores) and then discarded --
nothing downstream ever read it again, so a generated product's actual
Stripe Checkout amount had no connection to the number the discriminator and
Compliance already agreed on. Phase 5 carries it through to codegen instead.

Defaulted 0.0 (not nullable): every idea, including one scored before this
column existed, gets a real, well-typed value rather than NULL needing a
special case at every read site -- matching `ScoredIdea.self_serve_price_usd`'s
own default.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0024_idea_price"
down_revision: str | None = "0023_idea_defensibility"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "ideas",
        sa.Column("self_serve_price_usd", sa.Float(), nullable=False, server_default="0.0"),
    )


def downgrade() -> None:
    op.drop_column("ideas", "self_serve_price_usd")
