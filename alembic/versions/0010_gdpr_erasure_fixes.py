"""GDPR erasure fixes: outreach_log.lead_id survives a Lead erasure.

Revision ID: 0010_gdpr_erasure_fixes
Revises: 0009_validation_signals
Create Date: 2026-08-10

`erase_lead()` (GDPR Art. 17) deletes the `Lead` row outright. `outreach_log.lead_id`
was `NOT NULL` with a plain FK and no `ON DELETE` action, so every real erasure --
reachable only via the unsubscribe link on a message that was, by definition,
actually sent -- raised a foreign-key violation and never completed. Every erasure
request this factory has ever received has failed silently behind a 500.

Made nullable with `ON DELETE SET NULL` instead of `ON DELETE CASCADE`: the send
history itself (subject, status, product_id, converted_at, ledger attribution) is
not personal data once the identifying link is gone, and this codebase already
treats revenue attribution as product-level, never lead-level
(`marketing/attribution.py`) -- deleting the row would destroy real audit/analytics
value for no GDPR benefit.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0010_gdpr_erasure_fixes"
down_revision: str | None = "0009_validation_signals"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE outreach_log DROP CONSTRAINT fk_outreach_log_lead_id_leads")
    op.execute("ALTER TABLE outreach_log ALTER COLUMN lead_id DROP NOT NULL")
    op.execute(
        """
        ALTER TABLE outreach_log
            ADD CONSTRAINT fk_outreach_log_lead_id_leads
            FOREIGN KEY (lead_id) REFERENCES leads (id) ON DELETE SET NULL
        """
    )


def downgrade() -> None:
    op.execute("ALTER TABLE outreach_log DROP CONSTRAINT fk_outreach_log_lead_id_leads")
    op.execute(
        "DELETE FROM outreach_log WHERE lead_id IS NULL"
    )
    op.execute("ALTER TABLE outreach_log ALTER COLUMN lead_id SET NOT NULL")
    op.execute(
        """
        ALTER TABLE outreach_log
            ADD CONSTRAINT fk_outreach_log_lead_id_leads
            FOREIGN KEY (lead_id) REFERENCES leads (id)
        """
    )
