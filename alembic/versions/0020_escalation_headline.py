"""Add headline/reply_hint/needs_reply to interrupts for clearer Telegram messages.

Revision ID: 0020_escalation_headline
Revises: 0019_scrape_failures
Create Date: 2026-08-14

The pushed Telegram message and the `/status`/`/issue` views previously built
their text from just (reason, subject, detail) plus a generic, always-identical
"Reply with: #tag <your answer>" footer -- reply vocabulary was inconsistent
across call sites (APPROVE/REJECT, RESUME, SKIP, "retry", "reply with anything"),
several sites gave no reply instruction at all, and the same `reason` value could
mean genuinely different things (e.g. LEAD_SOURCING_STALLED covers both a
credential problem and a code bug), so a fixed per-reason label was misleading.

`headline` and `reply_hint` let each raise site supply its own precise category
and exact instruction; `needs_reply` lets the message honestly distinguish "your
reply changes something" from "this is a reference tag only."

Nullable / defaulted so no backfill is needed -- historical resolved interrupts
are never re-rendered, and `hitl/formatting.py` falls back to the old generic
shape if `headline`/`reply_hint` are ever unset.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0020_escalation_headline"
down_revision: str | None = "0019_scrape_failures"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("interrupts", sa.Column("headline", sa.Text(), nullable=True))
    op.add_column("interrupts", sa.Column("reply_hint", sa.Text(), nullable=True))
    op.add_column(
        "interrupts",
        sa.Column("needs_reply", sa.Boolean(), nullable=False, server_default=sa.true()),
    )


def downgrade() -> None:
    op.drop_column("interrupts", "needs_reply")
    op.drop_column("interrupts", "reply_hint")
    op.drop_column("interrupts", "headline")
