"""Dedup escalations by what they are *about*, not only by how they are worded.

`uq_interrupts_open_subject` collapsed open escalations on `(reason, subject)`. For
a provider outage that is exactly right -- the subject *is* the thing ("gemini is in
a sustained outage"), and one condition should produce one notice.

For a product it is wrong. The subject is built from the product's *name*, and names
come from `idea.target_niche`, so two different products with similar enough niches
share one escalation and the second is silently swallowed. Observed while testing:
two consecutive runs of the same idea produced two products, and the second's release
diagnosis folded into the first's still-open notice and was never delivered. The
first notice pointed at a product that no longer existed.

**`COALESCE(product_id, '')`, not a plain column.** Postgres treats NULLs as distinct
in a unique index, so adding the bare column would make every product-less escalation
unique -- which is precisely the storm BUG_LOG #20 fixed (40 identical provider-outage
notices). Coalescing to the empty string keeps all product-less escalations sharing
one dedup slot per subject, exactly as before, while scoping product escalations to
their own product.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0042_interrupt_dedup_by_product"
down_revision: str | None = "0041_design_requirements"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_NAME = "uq_interrupts_open_subject"


def upgrade() -> None:
    op.drop_index(_NAME, table_name="interrupts")
    op.execute(
        f"CREATE UNIQUE INDEX {_NAME} ON interrupts "
        "(reason, COALESCE(product_id, ''), subject, status) "
        "WHERE status = 'open'"
    )


def downgrade() -> None:
    # Two open escalations that differ only by product would violate the narrower
    # index, so the newer of each pair is resolved rather than blocking the
    # downgrade. Resolving is the honest direction: the older notice is the one an
    # operator has already seen.
    op.execute(
        """
        UPDATE interrupts SET status = 'resolved'
        WHERE status = 'open' AND id NOT IN (
            SELECT DISTINCT ON (reason, subject) id FROM interrupts
            WHERE status = 'open' ORDER BY reason, subject, created_at ASC
        )
        """
    )
    op.drop_index(_NAME, table_name="interrupts")
    op.execute(
        f"CREATE UNIQUE INDEX {_NAME} ON interrupts "
        "(reason, subject, status) WHERE status = 'open'"
    )
