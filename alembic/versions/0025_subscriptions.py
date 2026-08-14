"""Subscriptions, dunning, and churn tracking.

Revision ID: 0025_subscriptions
Revises: 0024_idea_price
Create Date: 2026-08-14

Inert until a real recurring sale exists -- no generated product creates a
Stripe Subscription yet (Phase 5 only wired up a one-time Checkout amount).
This builds the ingestion side so the factory is ready to correctly process
subscription lifecycle events the moment any product does, without another
migration in the critical path then.

``customers``/``subscriptions`` deliberately store no personal data (see
``Customer``'s own docstring) -- MRR and churn math need only the Stripe ids.
``ledger`` gains ``stripe_subscription_id``/``stripe_customer_id`` as plain
nullable columns, not foreign keys, for the same reason ``product_id`` already
is: Stripe webhook delivery order across different event types is not
guaranteed, and a revenue event arriving before its subscription's ``created``
event has been processed must never be rejected because of it.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0025_subscriptions"
down_revision: str | None = "0024_idea_price"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("ledger", sa.Column("stripe_subscription_id", sa.String(length=255)))
    op.add_column("ledger", sa.Column("stripe_customer_id", sa.String(length=255)))

    op.create_table(
        "customers",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("stripe_customer_id", sa.String(length=255), nullable=False),
        sa.Column("product_id", sa.String(length=32), sa.ForeignKey("products.id")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint("stripe_customer_id", name="uq_customers_stripe_customer_id"),
    )
    op.create_index("ix_customers_product", "customers", ["product_id"])

    op.create_table(
        "subscriptions",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("stripe_subscription_id", sa.String(length=255), nullable=False),
        sa.Column(
            "customer_id", sa.String(length=32), sa.ForeignKey("customers.id"), nullable=False
        ),
        sa.Column("product_id", sa.String(length=32), sa.ForeignKey("products.id")),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("price_cents", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cancel_at_period_end", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("current_period_end", sa.DateTime(timezone=True)),
        sa.Column("churn_kind", sa.String(length=16)),
        sa.Column("churned_at", sa.DateTime(timezone=True)),
        sa.Column("memory_written_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "stripe_subscription_id", name="uq_subscriptions_stripe_subscription_id"
        ),
        sa.CheckConstraint(
            "churn_kind IN ('voluntary','involuntary') OR churn_kind IS NULL",
            name="subscriptions_churn_kind_valid",
        ),
    )
    op.create_index("ix_subscriptions_customer", "subscriptions", ["customer_id"])
    op.create_index("ix_subscriptions_product", "subscriptions", ["product_id"])
    op.create_index("ix_subscriptions_status", "subscriptions", ["status"])
    op.create_index(
        "ix_subscriptions_unremembered_churn",
        "subscriptions",
        ["churn_kind", "memory_written_at"],
    )

    op.create_table(
        "dunning_events",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("stripe_invoice_id", sa.String(length=255), nullable=False),
        sa.Column(
            "subscription_id",
            sa.String(length=32),
            sa.ForeignKey("subscriptions.id"),
            nullable=False,
        ),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="open"),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "opened_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("closed_at", sa.DateTime(timezone=True)),
        sa.Column("closed_reason", sa.String(length=64)),
        sa.UniqueConstraint("stripe_invoice_id", name="uq_dunning_events_stripe_invoice_id"),
        sa.CheckConstraint(
            "status IN ('open','recovered','terminal')", name="dunning_events_status_valid"
        ),
    )
    op.create_index("ix_dunning_events_subscription", "dunning_events", ["subscription_id"])
    op.create_index("ix_dunning_events_open", "dunning_events", ["subscription_id", "status"])


def downgrade() -> None:
    op.drop_table("dunning_events")
    op.drop_table("subscriptions")
    op.drop_table("customers")
    op.drop_column("ledger", "stripe_customer_id")
    op.drop_column("ledger", "stripe_subscription_id")
