"""Issues: a finding with an identity, a state, and a closure that must be earned.

Revision ID: 0033_issues
Revises: 0032_task_events
Create Date: 2026-09-13

Every defect this factory found was a first encounter. The reviewer reported one,
`queue_review_patch` turned it into a `PATCH_PRODUCT` task carrying a text blob,
the task eventually went `done`, and nothing connected any of it. "Done" meant the
patch *ran*, never that the symptom was gone.

Proven live on one product: the same broken checkout was filed four separate
times over 24 days -- twice within the same minute, as two unrelated work items --
and was still broken when the fifth review found it again. The system was not
failing to fix it. It was failing to remember it was the same bug.

Three columns carry the whole idea:

- `fingerprint` gives a defect an identity that survives rewording, so re-finding
  one *matches* instead of spawning a twin. Free text cannot do this: the
  escalation-dedup bug (BUG_LOG #20) proved an LLM never writes the same sentence
  twice even when the situation is unchanged.
- `status` makes the lightweight workflow real -- detected, triaged, fixing,
  fixed, verified, closed.
- `verified_at`, separate from `fixed_at`, is what makes closure mean something. A
  patch completing sets `fixed`. Only a later review failing to find the symptom
  sets `verified`. Nothing else may close an issue.

Reopening rather than re-creating is deliberate: a closed issue found again is a
regression, and that is worth seeing as one row with a history instead of two rows
with none.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0033_issues"
down_revision: str | None = "0032_task_events"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "issues",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("product_id", sa.String(length=32), nullable=True),
        # The identity. Derived from what the defect *is* (product + kind +
        # normalised location), never from how it was described.
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=24), nullable=False),
        sa.Column("severity", sa.String(length=16), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("location", sa.Text(), nullable=True),
        sa.Column("expected", sa.Text(), nullable=True),
        sa.Column("actual", sa.Text(), nullable=True),
        sa.Column("evidence", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="open"),
        # Which employee found it, and which one it is currently with. `assigned_to`
        # is a role, not a person -- "adding a role is a code change a human
        # reviews", so a role name is the most specific owner this org has.
        sa.Column("found_by", sa.String(length=32), nullable=True),
        sa.Column("assigned_to", sa.String(length=32), nullable=True),
        # The work commissioned for it. No FK: a task can be cleaned up and the
        # issue outliving its task is better than a dangling constraint.
        sa.Column("fix_task_id", sa.String(length=32), nullable=True),
        sa.Column("times_seen", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("resolution", sa.Text(), nullable=True),
        sa.Column(
            "first_seen_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "last_seen_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("fixed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("detail", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_issues"),
        sa.CheckConstraint(
            "status IN ('open','triaged','fixing','fixed','verified','closed','wont_fix')",
            name="issue_status_valid",
        ),
        sa.CheckConstraint(
            "severity IN ('critical','high','medium','low')", name="issue_severity_valid"
        ),
    )
    # One row per real defect per product. This is the constraint that makes
    # "the same bug, found again" an update instead of a duplicate -- the single
    # thing whose absence produced four filings of one broken checkout.
    op.create_index(
        "uq_issues_product_fingerprint",
        "issues",
        ["product_id", "fingerprint"],
        unique=True,
    )
    op.create_index("ix_issues_status", "issues", ["status", "last_seen_at"])
    op.create_index("ix_issues_product_status", "issues", ["product_id", "status"])
    op.create_index("ix_issues_fix_task", "issues", ["fix_task_id"])


def downgrade() -> None:
    op.drop_index("ix_issues_fix_task", table_name="issues")
    op.drop_index("ix_issues_product_status", table_name="issues")
    op.drop_index("ix_issues_status", table_name="issues")
    op.drop_index("uq_issues_product_fingerprint", table_name="issues")
    op.drop_table("issues")
