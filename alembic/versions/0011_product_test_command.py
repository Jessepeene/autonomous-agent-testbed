"""Persist a product's test command so a repair does not have to regenerate it.

Revision ID: 0011_product_test_command
Revises: 0010_gdpr_erasure_fixes
Create Date: 2026-08-11

`test_command` lived only on `ScaffoldPlan`, the in-memory result of a code
generation call, and was passed straight to `_verify_and_ship` and then discarded.
That was fine while every heal cycle *regenerated* the whole product: the plan --
and with it a fresh test command -- was rebuilt from scratch each time.

The agentic repair path does not regenerate. It edits the existing workspace in
place, so nothing reproduces the plan, and both the repair agent (which must run the
failing suite to diagnose it) and the verification gate (which must run the same
suite to confirm the fix) need the command from somewhere durable.

Nullable on purpose: products built before this migration have no recorded command,
and the repair path deliberately refuses to guess a default -- a guessed command
would have the agent optimising against a different suite than the gate runs, which
is a worse failure than simply falling back to full regeneration.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011_product_test_command"
down_revision: str | None = "0010_gdpr_erasure_fixes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("products", sa.Column("test_command", sa.String(length=400), nullable=True))


def downgrade() -> None:
    op.drop_column("products", "test_command")
