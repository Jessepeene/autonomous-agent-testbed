"""Let an outcome be evidence about more than one thing.

`learning_episodes` had exactly one non-null foreign key, `tunable_parameter_id`. That
single column decided, silently and for the whole system, that **every outcome teaches
exactly one thing** -- so a validation that failed because the idea was weak was
recorded against the prompt that wrote the landing page, and the idea, the acceptance
criteria, the design standard and the decision to ship all learned nothing. The page
writer was blamed for a bad idea.

Three tables, and the shape of the fix:

**`decisions`** gives a judgement identity. A release decision already carried the
choice, the rationale, the unmet MUSTs and the issues deliberately accepted -- inside a
JSONB blob on a `task_events` row, where nothing could point at it. A company whose
decisions are unaddressable cannot review them, and the recurring strategic review the
end goal names has nothing to review.

**`outcomes`** generalises an episode. `succeeded` stays, because the trial statistics
are Bernoulli and must keep working untouched; `value_cents` is added, because profit
is a magnitude and a system that records only whether something worked cannot tell a
EUR 12 month from a EUR 1,200 one.

**`outcome_evidence`** is the keystone: many subjects per outcome, through the same
`(subject_kind, subject_id)` pattern `requirements` already uses. No check constraint
on `subject_kind` on purpose -- it starts as `parameter` and `decision`, and a later
phase adding `code` or `workflow` should cost an insert, not a migration.

**Running trials must not move.** Every `learning_episodes` row is copied to one
`outcomes` row plus one `outcome_evidence` row with `subject_kind='parameter'`, ids
preserved, so `episode_counts` reading through the join returns byte-identical
numerators and denominators for every trial currently open. `learning_episodes` is
left in place, unread, as a fallback for one release -- dropping it in the same
migration that starts depending on its replacement would leave no way back.

Revision ID: 0044_decisions_and_outcomes
Revises: 0043_sent_messages_and_self_test
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0044_decisions_and_outcomes"
down_revision = "0043_sent_messages_and_self_test"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "decisions",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("kind", sa.String(length=40), nullable=False),
        sa.Column("subject_kind", sa.String(length=24), nullable=False),
        sa.Column("subject_id", sa.String(length=32), nullable=False),
        sa.Column("choice", sa.String(length=64), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=False, server_default=""),
        sa.Column("made_by_engine", sa.String(length=32), nullable=False),
        sa.Column("made_by_task_id", sa.String(length=32), nullable=True),
        sa.Column(
            "alternatives",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="[]",
        ),
        sa.Column(
            "detail",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="{}",
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_decisions_subject", "decisions", ["subject_id", "created_at"])
    op.create_index("ix_decisions_task", "decisions", ["made_by_task_id"])
    op.create_index("ix_decisions_kind", "decisions", ["kind", "created_at"])

    op.create_table(
        "outcomes",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("kind", sa.String(length=40), nullable=False),
        sa.Column("succeeded", sa.Boolean(), nullable=True),
        sa.Column("value_cents", sa.Integer(), nullable=True),
        sa.Column("subject_kind", sa.String(length=24), nullable=True),
        sa.Column("subject_id", sa.String(length=32), nullable=True),
        sa.Column(
            "detail",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="{}",
        ),
        sa.Column("task_id", sa.String(length=32), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_outcomes_kind", "outcomes", ["kind", "created_at"])
    op.create_index("ix_outcomes_subject", "outcomes", ["subject_id", "created_at"])

    op.create_table(
        "outcome_evidence",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("outcome_id", sa.String(length=32), nullable=False),
        sa.Column("subject_kind", sa.String(length=24), nullable=False),
        sa.Column("subject_id", sa.String(length=32), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["outcome_id"], ["outcomes.id"], ondelete="CASCADE"),
        sa.UniqueConstraint(
            "outcome_id", "subject_kind", "subject_id", name="uq_outcome_evidence_subject"
        ),
    )
    op.create_index(
        "ix_outcome_evidence_subject", "outcome_evidence", ["subject_kind", "subject_id"]
    )

    # The copy that keeps every open trial's arithmetic identical. The episode's own
    # id becomes the outcome's id, so anything that recorded an episode id still
    # resolves, and `created_at` is carried across rather than defaulted -- a trial
    # window is ordered by it, and defaulting would collapse every historical episode
    # onto the migration's own timestamp and silently re-date the evidence.
    op.execute(
        """
        INSERT INTO outcomes (id, kind, succeeded, value_cents, detail, task_id, created_at)
        SELECT id,
               'trial_episode',
               outcome = 'success',
               NULL,
               detail,
               task_id,
               created_at
        FROM learning_episodes
        """
    )
    op.execute(
        """
        INSERT INTO outcome_evidence (id, outcome_id, subject_kind, subject_id, created_at)
        SELECT md5(random()::text || clock_timestamp()::text),
               id,
               'parameter',
               tunable_parameter_id,
               created_at
        FROM learning_episodes
        """
    )


def downgrade() -> None:
    # `learning_episodes` was never dropped, so nothing needs restoring: the rows
    # copied above are still in their original table, and dropping these three leaves
    # the system exactly as it was before this revision ran.
    op.drop_index("ix_outcome_evidence_subject", table_name="outcome_evidence")
    op.drop_table("outcome_evidence")
    op.drop_index("ix_outcomes_subject", table_name="outcomes")
    op.drop_index("ix_outcomes_kind", table_name="outcomes")
    op.drop_table("outcomes")
    op.drop_index("ix_decisions_kind", table_name="decisions")
    op.drop_index("ix_decisions_task", table_name="decisions")
    op.drop_index("ix_decisions_subject", table_name="decisions")
    op.drop_table("decisions")
