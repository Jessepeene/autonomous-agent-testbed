"""Add pypi_package_checks: a cached verdict on whether a package name is real.

Revision ID: 0022_pypi_package_checks
Revises: 0021_signal_verification
Create Date: 2026-08-14

Deterministic slopsquatting defense (`engines/development/dependencies.py`).
Bug Council's dependency reviewer already asks a model to flag "packages that
are abandoned or typosquat a popular name", but a hallucinated name that
sounds plausible to the generator can sound just as plausible to the reviewer
grading it. This table backs a real PyPI existence/registration-age check,
run before `pip install`, not after -- Bug Council never sees a package that
failed to install, since a build with a hallucinated dependency already fails
the install step and regenerates from scratch long before Council convenes.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0022_pypi_package_checks"
down_revision: str | None = "0021_signal_verification"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "pypi_package_checks",
        sa.Column("name", sa.String(length=255), primary_key=True, nullable=False),
        sa.Column("exists", sa.Boolean(), nullable=False),
        sa.Column("first_release_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "checked_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_table("pypi_package_checks")
