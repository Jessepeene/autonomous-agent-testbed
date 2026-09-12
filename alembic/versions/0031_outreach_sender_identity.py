"""Track which sender-identity version served each outreach send.

Revision ID: 0031_outreach_sender_identity
Revises: 0030_product_design_brief
Create Date: 2026-09-08

The outreach sender's display name is becoming a learning-loop tunable
parameter (``factory.engines.marketing.outreach:SENDER_IDENTITY``) instead of
a single fixed value, so its real outcome -- did the recipient's product
eventually convert -- needs to be recorded back against whichever version was
active at send time. Unlike every other tunable parameter's outcome, that
answer isn't known synchronously: it's either a match from
``marketing/attribution.py::reconcile`` up to 14 days later, or a timeout with
no match. ``identity_outcome_recorded_at`` is what lets the timeout path run
repeatedly without ever double-counting a send.

Both nullable: every existing ``outreach_log`` row predates this feature and
was never scored against a sender identity at all.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0031_outreach_sender_identity"
down_revision: str | None = "0030_product_design_brief"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE outreach_log ADD COLUMN sender_identity_version_id VARCHAR(32) "
        "REFERENCES tunable_parameters (id)"
    )
    op.execute(
        "ALTER TABLE outreach_log ADD COLUMN identity_outcome_recorded_at "
        "TIMESTAMP WITH TIME ZONE"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE outreach_log DROP COLUMN IF EXISTS identity_outcome_recorded_at")
    op.execute("ALTER TABLE outreach_log DROP COLUMN IF EXISTS sender_identity_version_id")
