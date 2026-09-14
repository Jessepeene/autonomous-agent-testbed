"""Anchor convergence on repair attempts, not on release decisions.

`product_progress.blocking_at_last_attempt` read the most recent `release.decided`
event. `_verify_and_ship` writes that event and *then* calls `_heal`, which
re-reads it -- so the "previous" figure was the current one, written seconds
earlier in the same pass. `blocking_now < blocking_before` compared the state
against itself and was therefore False every single time, which `_heal` reads as
a wall.

That fired on the first attempt of every `fix_first` decision, always. Both of
2026-09-14's escalations were this, and the zero-case guard added that morning
(`converging` returns None when `blocking_now == 0`) only sidestepped the one
manifestation where both numbers happened to be zero; the non-zero case kept
self-comparing.

Convergence is a fact about repair attempts -- "did the last attempt reduce the
blocking set" -- so it has to be anchored on an attempt. `heal_attempts` already
has one row per attempt; this records what was blocking when each was made.

Revision ID: 0037_heal_attempt_blocking
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0037_heal_attempt_blocking"
down_revision: str | None = "0036_idea_evidence"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    # Nullable, and that is meaningful: attempts recorded before this migration
    # have no figure, and "unknown" must stay distinguishable from "zero were
    # blocking" -- reading a missing value as 0 would manufacture the exact false
    # convergence signal this exists to remove.
    op.add_column(
        "heal_attempts", sa.Column("blocking_at_attempt", sa.Integer(), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("heal_attempts", "blocking_at_attempt")
