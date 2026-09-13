"""Requirements: what a piece of work must achieve, and how anyone can tell.

Revision ID: 0034_requirements
Revises: 0033_issues
Create Date: 2026-09-13

Nothing in this factory ever wrote down what it was trying to build. The landing
page prompt carries requirements, but they are *generic* -- the same form, beacon,
SEO tags and privacy page for every product ever generated. What this page must do
for this idea, and what would make it done, existed nowhere.

Everything downstream inherited that hole. Bug Council reviews against categories
(security, performance, quality), never against intent. AUDITOR has to infer what a
product was supposed to do by looking at it. Tests cover whatever the generator
chose to cover. And release is `if not deploy_blocking` -- which is unbounded,
because there is always another finding, so "done" was a state the system could not
reach.

`must` / `should` / `out_of_scope` is the part that makes it useful rather than
decorative. It is what lets a product be **done without being perfect**: meets its
must-haves, with known issues accepted deliberately. A single flat list of
requirements would just be a longer blocking gate.

Deliberately generic in `subject_kind`/`subject_id` rather than a `product_id`
column. Requirements are not a product feature -- a repair task, a campaign, a
discovery cycle all have "what is this for and when is it done", and a human team
writes that down for every one of them. Products are simply the first subject.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0034_requirements"
down_revision: str | None = "0033_issues"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "requirements",
        sa.Column("id", sa.String(length=32), nullable=False),
        # Generic on purpose -- see the module docstring. No FK for the same reason
        # task_events has none: the record outliving its subject is better than a
        # constraint that blocks cleanup.
        sa.Column("subject_kind", sa.String(length=24), nullable=False),
        sa.Column("subject_id", sa.String(length=32), nullable=False),
        #: What this work is for, in one sentence, in terms of the outcome wanted.
        sa.Column("goal", sa.Text(), nullable=False),
        #: Non-negotiable, checkable statements. Failing one means not done.
        sa.Column("must", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        #: Valuable, not blocking. The difference between these and `must` is the
        #: whole point: it is what a release judgement weighs rather than obeys.
        sa.Column("should", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        #: Named explicitly so a reviewer stops flagging the absence of things that
        #: were never meant to exist -- and so scope creep is visible as a change to
        #: this row rather than as an argument nobody recorded.
        sa.Column("out_of_scope", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_by", sa.String(length=64), nullable=True),
        sa.Column("source_task_id", sa.String(length=32), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        #: Requirements change; they are not rewritten. A superseded row stays so
        #: "we judged the old build against a different standard" remains true and
        #: visible rather than being quietly erased by the next version.
        sa.Column("superseded_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_requirements"),
        sa.CheckConstraint(
            "subject_kind IN ('product','task','issue','campaign')",
            name="requirement_subject_kind_valid",
        ),
    )
    # The live set for a subject: one partial index answering "what is this work
    # meant to achieve right now", which is the only question anything asks.
    op.create_index(
        "ix_requirements_live",
        "requirements",
        ["subject_kind", "subject_id"],
        unique=True,
        postgresql_where=sa.text("superseded_at IS NULL"),
    )
    op.create_index("ix_requirements_subject", "requirements", ["subject_kind", "subject_id"])


def downgrade() -> None:
    op.drop_index("ix_requirements_subject", table_name="requirements")
    op.drop_index("ix_requirements_live", table_name="requirements")
    op.drop_table("requirements")
