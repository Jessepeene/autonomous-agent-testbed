"""Add scrape_domain_failures: a no-call list for domains that keep timing out.

Revision ID: 0019_scrape_failures
Revises: 0018_lead_brief
Create Date: 2026-08-13

Observed live: a real test harvest against the web-search lead provider spent
several minutes attempting the same handful of real-world domains
(quickbooks.intuit.com, a couple of small bookkeeping sites), every one of them
failing with `ConnectTimeout`/`ReadTimeout` — datacenter-IP blocking or bot
defenses, not anything fixable in this factory's own request-building code.
Zero leads came back, but the time and the wasted fetch attempts were real.

This table records a running per-domain failure count. Once a domain crosses
`WebsiteContactProvider`'s threshold, it is skipped before a fetch is even
attempted on later harvests — global across products, since a site blocking
this server's IP blocks every product's harvest equally.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0019_scrape_failures"
down_revision: str | None = "0018_lead_brief"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "scrape_domain_failures",
        sa.Column("domain", sa.String(length=255), primary_key=True, nullable=False),
        sa.Column("failure_count", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("last_reason", sa.String(length=32), nullable=False),
        sa.Column(
            "last_failed_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_table("scrape_domain_failures")
