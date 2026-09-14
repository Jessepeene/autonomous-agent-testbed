"""Keep the raw signals behind an idea instead of deleting them.

`scout_signals` is a one-shot hand-off buffer: `drain_scout_signals` reads the
rows for a task and deletes them in the same call. So the moment clustering
finished, everything real people had actually written about the problem was
gone, and all that survived was a `source_url` and an integer.

That made two things impossible rather than merely absent:

- **Audience research.** There was nothing to research. Any "audience brief"
  built from what remained would be a persona generated from the idea's own
  generated prose -- inventing the customer and then believing it.
- **Judging the evidence.** `evidence_count` could be counted and displayed but
  never weighed, because the content behind it no longer existed. Every idea this
  factory has ever produced carries `evidence_count = 2`, and the only gate
  anywhere is `if evidence_count == 0`, so two signals from a single GitHub
  thread cleared the bar to build a product exactly as easily as two hundred
  would have.

Both are the same missing fact, so this is one table rather than two features.

Revision ID: 0036_idea_evidence
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0036_idea_evidence"
down_revision: str | None = "0035_product_build_kind"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "idea_evidence",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column(
            "idea_id",
            sa.String(32),
            sa.ForeignKey("ideas.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("title", sa.String(300), nullable=False),
        # Text, not a bounded String: this is what somebody actually wrote, and
        # truncating it to fit a column is how the useful half of a complaint gets
        # lost. The reader truncates at the point of use instead.
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("url", sa.String(500), nullable=False),
        sa.Column("source", sa.String(60), nullable=False),
        sa.Column("engagement", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index("ix_idea_evidence_idea", "idea_evidence", ["idea_id"])
    # The judgement read from that evidence, kept with the idea. Nullable: every
    # idea created before this migration has evidence nobody retained, and a NULL
    # here means "never assessed", which must stay distinguishable from "assessed
    # and found wanting".
    op.add_column("ideas", sa.Column("demand_assessment", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("ideas", "demand_assessment")
    op.drop_index("ix_idea_evidence_idea", table_name="idea_evidence")
    op.drop_table("idea_evidence")
