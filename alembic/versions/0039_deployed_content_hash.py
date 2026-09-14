"""Record what was actually deployed, so "is this shipped?" can be asked, not remembered.

`workspace_undeployed` (0038) was a boolean set when a repair changed files and
cleared on a successful ship. It answered the right question and answered it the
wrong way: as a *remembered* fact, written by one code path and cleared by
another, it can drift from reality and silently did -- every edit made before the
column existed was invisible to it, so a product whose workspace genuinely held an
unshipped fix reported `false`.

That is the same failure the whole day was made of: state the system believes
rather than state it can check.

The content hash makes it checkable. Deploy records a digest of exactly the bundle
it uploaded; asking whether the workspace is shipped means hashing the workspace
the same way and comparing. Both sides are computed from reality at the moment the
question is asked, so there is nothing to keep in sync and nothing to drift.

Revision ID: 0039_deployed_content_hash
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0039_deployed_content_hash"
down_revision: str | None = "0038_workspace_undeployed"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    # Nullable, and the null case is meaningful: a product deployed before this
    # existed has no recorded digest, and "unknown" must not be read as "matches".
    # Callers fall back to `workspace_undeployed` there, which is why 0038's column
    # stays rather than being replaced.
    op.add_column(
        "products", sa.Column("deployed_content_hash", sa.String(64), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("products", "deployed_content_hash")
