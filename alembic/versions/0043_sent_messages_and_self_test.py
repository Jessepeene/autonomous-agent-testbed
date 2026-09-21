"""Keep what this company says, and make a self-test signal a signal that does not count.

Two removals of a narrowing, not two new rules.

**`sent_messages`.** `send_email` is the single chokepoint every message passes
through, and nothing kept what was in them: `OutreachLog` records a subject and no
body, so what this company actually said to a real prospect was unreadable after the
fact. A reviewer could look at a page and never at a word the company wrote. Retaining
subject, body, sender and product for every send -- including the ones composed but
deliberately not sent -- makes all of it reviewable without anyone deciding in advance
which messages are interesting.

`outcome` distinguishes a message that went out from one that was composed and held
(dry run, blacklist suppression, self-test), because "we said this" and "we would have
said this" are different facts and collapsing them is how a review of an unsent draft
gets mistaken for a review of what a customer received.

**`validation_signals.is_self_test`.** BUG_LOG 36 fixed a reviewer's invented signup
inflating the demand count by making `@factory-selftest.invalid` submissions leave no
trace at all. That conflated "must not corrupt the count" with "must not be recorded" --
and a submission that records nothing composes no email and issues no token, so the
entire journey past the form became invisible to review by construction. The flag is
the honest version: recorded like any other signal, excluded from every count.

Both columns carry personal data about a recipient, so `erase_lead` and
`erase_validation_signals` delete retained messages too -- see `state/repository.py`.
The erasure coverage ships with this, not after it.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0043_sent_messages_and_self_test"
down_revision: str | None = "0042_interrupt_dedup_by_product"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "sent_messages",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("product_id", sa.String(32), nullable=True, index=True),
        sa.Column("to_address", sa.String(320), nullable=False, index=True),
        sa.Column("from_address", sa.String(320), nullable=True),
        sa.Column("subject", sa.String(500), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        # "sent" | "composed_only" -- see the module docstring on why these are not
        # one fact.
        sa.Column("outcome", sa.String(32), nullable=False),
        sa.Column("detail", sa.String(200), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_sent_messages_product_created", "sent_messages", ["product_id", "created_at"]
    )
    op.add_column(
        "validation_signals",
        sa.Column("is_self_test", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("validation_signals", "is_self_test")
    op.drop_index("ix_sent_messages_product_created", table_name="sent_messages")
    op.drop_table("sent_messages")
