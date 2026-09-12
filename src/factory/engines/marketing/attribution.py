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

from factory.learning import prompts as learning_prompts
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
    #: Sender-identity outcomes recorded this run, success and timeout combined —
    #: see the two passes below and `learning/allowlist.py`'s comment on why this
    #: key's outcome can't be scored synchronously the way the content prompt's is.
    identity_episodes: int = 0


async def reconcile(session: AsyncSession) -> ReconciliationReport:
    """Attribute each product's first unattributed revenue event to its most recent
    outreach touch within the attribution window, if any -- and, as a side effect,
    score the SENDER_IDENTITY version active on that touch (a real conversion) and
    on any touch that aged out of the window unconverted (a real non-conversion)."""
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
        if touch.sender_identity_version_id:
            await learning_prompts.record_outcome(
                session, tunable_parameter_id=touch.sender_identity_version_id, success=True
            )
            report.identity_episodes += 1

    # A send this old could never be matched to a ledger entry above anyway (the
    # window check in `last_outreach_touch` already excludes it), so there's no
    # race between this pass recording a failure and the loop above recording a
    # late success for the same row.
    timed_out = await repository.outreach_needing_identity_timeout(
        session, older_than_days=ATTRIBUTION_WINDOW_DAYS
    )
    for row in timed_out:
        assert row.sender_identity_version_id is not None  # query guarantees this
        await learning_prompts.record_outcome(
            session, tunable_parameter_id=row.sender_identity_version_id, success=False
        )
        await repository.mark_identity_outcome_recorded(session, row.id)
        report.identity_episodes += 1

    return report
