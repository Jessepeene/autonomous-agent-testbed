"""Questions between departments: asking is a state of the work, not a tool call.

Every repair agent is already required to end with `RESULT: FIXED | NOT_FIXED |
NOT_NEEDED | BLOCKED`, and `runtime.py` already parses it into `is_blocked`.
Nothing branches on it. An agent that says it cannot proceed is handed another
retry, and what it was blocked *on* is discarded.

The rest of the loop exists too, for humans: `defer_task` lets work wait without
counting as failed (its own docstring describes exactly this case), `interrupts`
is an addressed question with a reply, and `resolve_reply` plus five
`_act_on_*_reply` handlers resume the work an answer unblocks.

So this is not a new mechanism. It generalises the recipient from *the human* to
*anyone who can answer*, and gives `is_blocked` somewhere to go.

Deliberately NOT a tool an agent elects to call. That bet was already made and lost
here: `report_product_issue` shipped in AUDITOR's own registry and both live runs
"spent their whole step budget investigating and never chose to call it". The agent
that most needs to ask is the confused one, least likely to spend a step on it.
Asking is a state the work is in; answering is work in someone's queue.

Revision ID: 0040_questions
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0040_questions"
down_revision: str | None = "0039_deployed_content_hash"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "questions",
        sa.Column("id", sa.String(32), primary_key=True),
        # Who is asking, and what is waiting on the answer. `blocking_task_id` is the
        # whole point: a question nobody is blocked on is a note, and a question that
        # does not resume anything is a conversation the factory cannot act on.
        sa.Column("asked_by_engine", sa.String(32), nullable=False),
        sa.Column("asked_by_task_id", sa.String(32), nullable=True),
        sa.Column("blocking_task_id", sa.String(32), nullable=True),
        sa.Column("product_id", sa.String(32), nullable=True),
        # The recipient. An engine name routes it into that department's queue;
        # `human` falls back to the existing interrupt path, which is what makes this
        # useful on day one -- before any department can answer, "Development is
        # blocked on X" reaching a person still beats a silent retry.
        sa.Column("asked_of", sa.String(32), nullable=False),
        sa.Column("question", sa.Text(), nullable=False),
        # What the asker already knows, so the answerer is not starting cold.
        sa.Column("context", sa.Text(), nullable=False, server_default=""),
        sa.Column("status", sa.String(16), nullable=False, server_default="open"),
        sa.Column("answer", sa.Text(), nullable=True),
        sa.Column("answered_by_task_id", sa.String(32), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("answered_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_questions_asked_of_status", "questions", ["asked_of", "status"])
    op.create_index("ix_questions_blocking_task", "questions", ["blocking_task_id"])


def downgrade() -> None:
    op.drop_index("ix_questions_blocking_task", table_name="questions")
    op.drop_index("ix_questions_asked_of_status", table_name="questions")
    op.drop_table("questions")
