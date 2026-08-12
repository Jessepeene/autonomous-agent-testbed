"""Fix uq_interrupts_open_subject to actually be scoped to open interrupts.

Revision ID: 0017_interrupt_open_subject
Revises: 0016_scout_signals
Create Date: 2026-08-12

The existing constraint is a plain UNIQUE(reason, subject, status) -- despite
its name, this makes (reason, subject) globally unique *per status value
forever*, not just while status='open'. The moment any interrupt with a given
(reason, subject) is resolved, no later, unrelated occurrence of that same
pair can ever be resolved either: `resolve_interrupt()`'s UPDATE to
status='resolved' collides with the row already sitting at that exact
(reason, subject, 'resolved') tuple.

Found live for HEAL_EXHAUSTED (worked around at that one call site by giving
its subject a fresh per-occurrence id). A broader audit found the same latent
crash at several other call sites that use a subject with no per-occurrence
uniqueness at all -- deliverability.py's freeze/re-freeze escalations,
supervisor/graph.py's Docker-unavailable escalation, main.py's preflight
announcements, and even supervisor/audit.py's own data-integrity check,
shipped the same day this was found.

A partial unique index scoped to status='open' matches what the name always
claimed to do, and closes this bug class everywhere at once -- including at
call sites nobody has written yet -- rather than requiring every future
escalation site to remember to hand-roll its own uniqueness suffix.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0017_interrupt_open_subject"
down_revision: str | None = "0016_scout_signals"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE interrupts DROP CONSTRAINT uq_interrupts_open_subject")
    op.execute(
        "CREATE UNIQUE INDEX uq_interrupts_open_subject ON interrupts "
        "(reason, subject, status) WHERE status = 'open'"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_interrupts_open_subject")
    op.execute(
        "ALTER TABLE interrupts ADD CONSTRAINT uq_interrupts_open_subject "
        "UNIQUE (reason, subject, status)"
    )
