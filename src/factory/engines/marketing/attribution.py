"""Product-level outreach-to-revenue attribution.

Stripe never reveals which specific lead paid — a ``LedgerEntry`` only ever carries a
``product_id``, not a buyer email — so this is product-level attribution ("this touch
plausibly caused this product's revenue"), never lead-level. True lead-level
attribution would need the buyer's email threaded through Checkout back to a `Lead`
row, which is a separate, materially larger feature and out of scope here.

Deterministic, no LLM call: this is plumbing that feeds the learning loop's outcome
data, not a judgement call.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from factory.logging import get_logger
from factory.state import repository

log = get_logger(__name__)

#: Matches the range commercial outbound tooling uses by default for cold-email
#: attribution — short enough that the causal claim stays credible, long enough to
#: cover "saw it, meant to try it, got to it a week later."
ATTRIBUTION_WINDOW_DAYS = 14


@dataclass(slots=True)
class ReconciliationReport:
    checked: int = 0
    attributed: int = 0


async def reconcile(session: AsyncSession) -> ReconciliationReport:
    """Attribute each product's first unattributed revenue event to its most recent
    outreach touch within the attribution window, if any."""
    report = ReconciliationReport()
    entries = await repository.first_revenue_ledger_entries_needing_attribution(session)
    for entry in entries:
        report.checked += 1
        if entry.product_id is None:  # pragma: no cover - query already filters this
            continue
        touch = await repository.last_outreach_touch(
            session,
            product_id=entry.product_id,
            before=entry.created_at,
            window_days=ATTRIBUTION_WINDOW_DAYS,
        )
        if touch is None:
            log.info("attribution.no_touch_found", product_id=entry.product_id)
            continue
        await repository.attribute_outreach(
            session,
            outreach_log_id=touch.id,
            ledger_entry_id=entry.id,
            converted_at=entry.created_at,
        )
        report.attributed += 1
        log.info(
            "attribution.matched",
            product_id=entry.product_id,
            outreach_log_id=touch.id,
            ledger_entry_id=entry.id,
        )
    return report
