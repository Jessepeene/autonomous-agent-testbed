"""The objective lives in one row, and nothing quietly changes it.

Phase 5a (`ARCHITECTURE_ROADMAP.md`, "The bigger goal"). Today the founding
objective is a paragraph everyone in a conversation has read and no code has ever
checked against -- held by discipline, not architecture. Nothing would notice if it
drifted, because there was nothing to compare a drift *to*.

`objectives` is the same supersede-not-overwrite shape `requirements` already uses:
one live row (`superseded_at IS NULL`), a change possible but never silent -- it
always produces a new row, an old one that stops being current, and a fact a
strategic review cannot fail to see. Not a `Decision` (nothing decides this, it is
stated) and not a settings value (this codebase's own agents can already propose
edits to settings-shaped things elsewhere).

Seeded here with the founding paragraph verbatim, `set_by='jesse'`, so a strategic
review (Phase 5b, not yet built) never finds this table empty. No code path other
than a human running `scripts/set_objective.py` directly is meant to ever write a
second row.

Revision ID: 0046_objective
Revises: 0045_signup_journey
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0046_objective"
down_revision = "0045_signup_journey"
branch_labels = None
depends_on = None

_FOUNDING_STATEMENT = (
    "An autonomous, AI-driven business organization whose sole high-level "
    "objective is to maximize sustainable long-term profit. Rather than "
    "prescribing a specific business model, the system operates as a General "
    "Manager that can independently research markets, identify opportunities, "
    "choose between freelancing, services, SaaS, digital products, or other "
    "revenue streams, execute projects, acquire customers, manage finances, and "
    "build tools or modify its own workflows when necessary. It uses "
    "specialized AI agents and coding environments to carry out tasks, while an "
    "external persistent memory system stores its mission, strategies, "
    "experiments, decisions, financial results, and lessons learned. An "
    "immutable objective, hierarchical decision structure, and recurring "
    "strategic review cycle ensure that the organization maintains its "
    "long-term direction, learns from real-world feedback, and continuously "
    "reassesses how to generate profit without being constrained to a "
    "predetermined path."
)

_FOUNDING_REASON = (
    "Founding objective, stated by Jesse at project start and unchanged since. "
    "Captured in ARCHITECTURE_ROADMAP.md ('The bigger goal') 2026-09-23; given "
    "an actual row, rather than only a paragraph, on 2026-09-24."
)


def upgrade() -> None:
    op.create_table(
        "objectives",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("statement", sa.Text(), nullable=False),
        sa.Column("set_by", sa.String(length=64), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("superseded_at", sa.DateTime(timezone=True), nullable=True),
    )
    # Partial index would be neater (there is only ever one live row to find), but
    # this project's other supersede-shaped tables (`requirements`) don't use one
    # either -- consistent rather than clever, and the table stays tiny by nature.
    op.create_index("ix_objectives_live", "objectives", ["superseded_at"])

    objectives = sa.table(
        "objectives",
        sa.column("id", sa.String),
        sa.column("statement", sa.Text),
        sa.column("set_by", sa.String),
        sa.column("reason", sa.Text),
    )
    op.execute(
        objectives.insert().values(
            id="obj_founding_0001",
            statement=_FOUNDING_STATEMENT,
            set_by="jesse",
            reason=_FOUNDING_REASON,
        )
    )


def downgrade() -> None:
    op.drop_table("objectives")
