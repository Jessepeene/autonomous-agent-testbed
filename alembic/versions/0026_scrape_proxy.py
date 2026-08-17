"""Datacenter-proxy tracking for blocked scrape domains, and a place for
lead-sourcing's own failure summary to survive past one harvest call.

Revision ID: 0026_scrape_proxy
Revises: 0025_subscriptions
Create Date: 2026-08-14

Two independent additions, bundled because both close out the Phase 7
distribution work:

* ``scrape_domain_failures`` gains ``requires_proxy``/``proxy_failure_count``
  -- see `integrations/outbound.py` and `marketing/providers/website.py`'s
  retry logic. A domain already known to need the datacenter proxy skips a
  doomed direct attempt (up to a real 20s timeout) instead of re-paying it on
  every fetch.
* ``products`` gains ``last_lead_sourcing_summary`` -- `IngestReport` (the
  object that actually knows *why* a harvest found nothing) is ephemeral,
  built fresh per call and discarded once the immediate credential-problem
  escalation checks it. Without somewhere durable to put its summary, the
  later, separate stalled-validation sweep that spawns CHANNEL_SCOUT as a
  third strike has no way to tell it why the previous channel failed.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0026_scrape_proxy"
down_revision: str | None = "0025_subscriptions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "scrape_domain_failures",
        sa.Column("requires_proxy", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "scrape_domain_failures",
        sa.Column("proxy_failure_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column("products", sa.Column("last_lead_sourcing_summary", sa.String(length=500)))


def downgrade() -> None:
    op.drop_column("products", "last_lead_sourcing_summary")
    op.drop_column("scrape_domain_failures", "proxy_failure_count")
    op.drop_column("scrape_domain_failures", "requires_proxy")
