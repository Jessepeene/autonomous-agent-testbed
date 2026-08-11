"""Custom domain request + Vercel attachment.

Revision ID: 0015_custom_domains
Revises: 0014_growth_roles
Create Date: 2026-08-12

Three columns on `products`, all read by `development/domain.py`:

`vercel_project` is the exact project name/slug `deploy.py` used for the most
recent successful deploy -- domain attachment targets a specific Vercel project,
and re-deriving the slug from `product_name` risks drift (a repair pass can
rename it).

`custom_domain` / `domain_status` track the request-and-attach lifecycle: NULL
(never asked) -> 'pending' (asked, no reply yet) -> 'attached' (Vercel has it,
DNS not yet verified) -> 'verified' (live on the custom domain).
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0015_custom_domains"
down_revision: str | None = "0014_growth_roles"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE products ADD COLUMN vercel_project VARCHAR(64)")
    op.execute("ALTER TABLE products ADD COLUMN custom_domain VARCHAR(253)")
    op.execute("ALTER TABLE products ADD COLUMN domain_status VARCHAR(16)")


def downgrade() -> None:
    op.execute("ALTER TABLE products DROP COLUMN IF EXISTS domain_status")
    op.execute("ALTER TABLE products DROP COLUMN IF EXISTS custom_domain")
    op.execute("ALTER TABLE products DROP COLUMN IF EXISTS vercel_project")
