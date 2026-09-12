"""Custom credentials: dashboard-added free-form key/value credentials.

Revision ID: 0029_custom_credentials
Revises: 0028_tier0_prompt_appendices
Create Date: 2026-08-24

The Integrations page's fixed `CREDENTIAL_CATALOG` (`observability/
integrations.py`) only covers credentials this factory already has adapter
code for -- there was deliberately no way to add an arbitrary key. This
table is the registry half of that escape hatch: only the *key name* is
stored here, never the value. The value is written straight to `.env` via
the same `write_env_value()` every catalog credential already uses; this
table exists purely so the dashboard knows which extra keys to list back
and preview (an arbitrary key isn't a `Settings` attribute `getattr()` can
read).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0029_custom_credentials"
down_revision: str | None = "0028_tier0_prompt_appendices"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "custom_credentials",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_unique_constraint("uq_custom_credentials_key", "custom_credentials", ["key"])


def downgrade() -> None:
    op.drop_constraint("uq_custom_credentials_key", "custom_credentials", type_="unique")
    op.drop_table("custom_credentials")
