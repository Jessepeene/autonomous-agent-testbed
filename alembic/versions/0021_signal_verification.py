"""Double opt-in for validation signals, and rate-limit buckets for abuse control.

Revision ID: 0021_signal_verification
Revises: 0020_escalation_headline
Create Date: 2026-08-14

Proven exploitable live: six unconfirmed email addresses submitted directly to
`/interest/{product_id}` cleared `validation_min_response_rate` and carried a
product all the way through `EVALUATE_VALIDATION` into a real MVP build --
indistinguishable, from the system's own point of view, from six genuine
prospects. A validation signal now only counts once the submitter clicks an
emailed confirmation link.

`rate_limit_buckets` backs `api/abuse.py`'s per-IP/per-product request limiter
for the two unauthenticated public write endpoints (`/interest`, `/analytics`).
Stored in Postgres, not in-process memory, so a caller can't bypass the limit by
landing on a different API worker.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0021_signal_verification"
down_revision: str | None = "0020_escalation_headline"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "validation_signals", sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "validation_signals", sa.Column("confirmation_token", sa.String(length=64), nullable=True)
    )
    op.add_column(
        "validation_signals",
        sa.Column("confirmation_method", sa.String(length=16), nullable=True),
    )
    op.add_column(
        "validation_signals", sa.Column("submitter_ip_hash", sa.String(length=64), nullable=True)
    )
    op.create_index(
        "ix_validation_signals_confirmation_token",
        "validation_signals",
        ["confirmation_token"],
        unique=True,
        postgresql_where=sa.text("confirmation_token IS NOT NULL"),
    )

    op.create_table(
        "rate_limit_buckets",
        sa.Column("bucket_key", sa.String(length=200), primary_key=True, nullable=False),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("hits", sa.Integer(), nullable=False, server_default="1"),
    )
    op.create_index("ix_rate_limit_buckets_window", "rate_limit_buckets", ["window_start"])


def downgrade() -> None:
    op.drop_index("ix_rate_limit_buckets_window", table_name="rate_limit_buckets")
    op.drop_table("rate_limit_buckets")

    op.drop_index("ix_validation_signals_confirmation_token", table_name="validation_signals")
    op.drop_column("validation_signals", "submitter_ip_hash")
    op.drop_column("validation_signals", "confirmation_method")
    op.drop_column("validation_signals", "confirmation_token")
    op.drop_column("validation_signals", "confirmed_at")
