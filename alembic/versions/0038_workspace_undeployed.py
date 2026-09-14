"""Track whether a workspace holds changes that never reached the customer.

The repair agent judges the *workspace*; the briefs it is given describe the
*deployed page*. Those are the same thing only while every workspace change has
shipped, and they diverge precisely when this matters most: a repair applies a
fix, the verify step escalates before deploying, and the fix sits in the files
unshipped.

Observed live on 2026-09-14. A repair fixed two defects a human had found on the
live page. Verification escalated before deploying. The next repair opened the
same workspace, correctly saw the work already done, reported `NOT_NEEDED` -- and
the handler closed both issues as invalid and promoted a validation landing page
to LIVE, while both defects were still on the page in front of visitors.

The agent was right about the workspace and wrong about the product, because
nothing recorded the difference. This records it.

Revision ID: 0038_workspace_undeployed
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0038_workspace_undeployed"
down_revision: str | None = "0037_heal_attempt_blocking"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    # Defaults false: a product with no repair since this shipped has nothing
    # pending by definition, and assuming otherwise would send every existing
    # product through a redeploy it does not need.
    op.add_column(
        "products",
        sa.Column(
            "workspace_undeployed",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )


def downgrade() -> None:
    op.drop_column("products", "workspace_undeployed")
