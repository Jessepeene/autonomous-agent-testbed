"""Growth roles: GROWTH_ANALYST, SEO_OPTIMIZER, CHANNEL_SCOUT.

Revision ID: 0014_growth_roles
Revises: 0013_ab_tests
Create Date: 2026-08-11

Two additions, both read by the new Orchestrator-callable growth roles
(agents/roles.py) rather than any fixed pipeline step:

`products.last_growth_review_at` / `last_channel_scout_at` are staleness
timestamps -- what the periodic backstop sweeps in scheduler.py order by, and
what GROWTH_ANALYST/CHANNEL_SCOUT's own `list_products_needing_growth_review`
tool reads, so a quiet product still gets looked at even if the Orchestrator's
own judgement never picks it.

`channel_experiments` tracks a genuinely new organic-channel proposal (Reddit,
a forum, a community) CHANNEL_SCOUT researched and drafted content for. Gated
by a mandatory human approval (`InterruptReason.CHANNEL_APPROVAL`, the same
poll-based pattern as `campaign_approvals`) before anything happens with it --
real posting is a human action, not something this factory automates. Outcome
is measured later, best-effort, via `page_views.referrer`.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0014_growth_roles"
down_revision: str | None = "0013_ab_tests"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE products ADD COLUMN last_growth_review_at TIMESTAMP WITH TIME ZONE")
    op.execute("ALTER TABLE products ADD COLUMN last_channel_scout_at TIMESTAMP WITH TIME ZONE")
    op.execute(
        """
        CREATE TABLE channel_experiments (
            id VARCHAR(32) NOT NULL,
            product_id VARCHAR(32) NOT NULL,
            channel VARCHAR(40) NOT NULL,
            url VARCHAR(500) NOT NULL,
            rationale TEXT NOT NULL,
            draft_content TEXT NOT NULL,
            status VARCHAR(16) NOT NULL DEFAULT 'proposed',
            outcome TEXT,
            interrupt_id VARCHAR(32),
            created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
            decided_at TIMESTAMP WITH TIME ZONE,
            concluded_at TIMESTAMP WITH TIME ZONE,
            CONSTRAINT pk_channel_experiments PRIMARY KEY (id),
            CONSTRAINT fk_channel_experiments_product_id_products
                FOREIGN KEY(product_id) REFERENCES products (id),
            CONSTRAINT fk_channel_experiments_interrupt_id_interrupts
                FOREIGN KEY(interrupt_id) REFERENCES interrupts (id),
            CONSTRAINT ck_channel_experiments_channel_experiment_status_valid
                CHECK (status IN ('proposed','approved','rejected','concluded'))
        )
        """
    )
    op.execute("CREATE INDEX ix_channel_experiments_product ON channel_experiments (product_id)")
    op.execute("CREATE INDEX ix_channel_experiments_status ON channel_experiments (status)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS channel_experiments CASCADE")
    op.execute("ALTER TABLE products DROP COLUMN IF EXISTS last_channel_scout_at")
    op.execute("ALTER TABLE products DROP COLUMN IF EXISTS last_growth_review_at")
