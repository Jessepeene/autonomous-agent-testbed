"""``/status`` -- the one Telegram command a human can send the factory, as
opposed to a tagged reply to a specific escalation (`#<tag> <answer>`).

Lives separately from `interrupts.py`'s reply-resolution logic, but is
dispatched from the same place (`collect_replies`): Telegram polling
(`TelegramNotifier.poll_replies`) advances a stateful offset on every call, so
a second, independent poller for commands would silently miss whatever the
first one already consumed. One inbound channel, one poll per tick, routed by
shape.

Deterministic, no LLM call -- this is a factual snapshot, not a judgement.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from factory.state import repository

#: Telegram appends "@BotName" to a command in a group chat; a bare "/status"
#: is what a 1:1 chat sends. Matches either, and ignores anything after.
STATUS_COMMAND = re.compile(r"^/status(@\w+)?\b", re.IGNORECASE)

_MAX_PRODUCTS = 10

#: `InterruptReason`'s own values are snake_case wire identifiers, chosen for
#: being a stable enum, not for being read by a human in a Telegram message --
#: rendered here instead of the raw value.
_REASON_LABEL: dict[str, str] = {
    "missing_credential": "Missing credential",
    "legal_uncertainty": "Needs legal/compliance review",
    "budget_exceeded": "Daily spend cap reached",
    "campaign_approval": "Outreach campaign approval",
    "deliverability_frozen": "Deliverability frozen (spam complaints)",
    "channel_approval": "Growth channel approval",
    "domain_required": "Custom domain needed",
    "data_integrity": "Data integrity check",
    "heal_exhausted": "Repair failed repeatedly",
}


async def build_status_report(session: AsyncSession) -> str:
    """A factual snapshot in two clearly separate sections -- PRODUCTS and
    ESCALATIONS use different internal fields on purpose (stage/url/numbers vs.
    reason/about/issue/reply), not just a different bullet, so which kind of
    thing a given entry is can never be mistaken for the other at a glance.

    PRODUCTS: every active product's name, stage, and the same numbers
    `GROWTH_ANALYST` reads (`tools/growth.py::read_product_growth`) -- views,
    interest signals, leads, whether an AB test is running.

    ESCALATIONS: what (not just how many) is waiting on a human right now.
    Each names the product it's *about* explicitly, by id lookup and in full
    (no truncation -- a name cut off right before the one word that actually
    distinguished it from a similarly-worded active product read as
    confusingly identical, found live), independent of whether that product
    still appears in the PRODUCTS section above -- a heal-exhaustion
    escalation for a since-retired product must never read as though it
    belongs to whatever is currently active.
    """
    products = await repository.active_products(session, limit=_MAX_PRODUCTS)
    interrupt_total = await repository.count_open_interrupts(session)
    interrupts = await repository.list_open_interrupts(session, limit=_MAX_PRODUCTS)

    lines = ["\U0001f4ca Factory status"]

    lines.append(f"\nPRODUCTS — {len(products)} active")
    if not products:
        lines.append("(none yet)")
    for i, product in enumerate(products, start=1):
        since = datetime.now(UTC) - timedelta(days=7)
        views = await repository.tally_page_views(session, product.id, since=since)
        signals = await repository.tally_validation_signals(session, product.id)
        leads = await repository.count_leads(session, product.id)
        ab_test = await repository.get_running_ab_test(session, product.id)
        lines.append(
            f"\n{i}. {product.name}\n"
            f"   stage: {product.status}\n"
            f"   url: {product.deploy_url or 'no URL yet'}\n"
            f"   views (7d): {views} | signals: {signals} | leads: {leads}"
            + (" | AB test running" if ab_test else "")
        )

    lines.append(f"\nESCALATIONS — {interrupt_total} open")
    if not interrupts:
        lines.append("(none)")
    for i, interrupt in enumerate(interrupts, start=1):
        about = "(not tied to a specific product)"
        if interrupt.product_id:
            escalation_product = await repository.get_product(session, interrupt.product_id)
            if escalation_product is not None:
                about = f"{escalation_product.name} ({escalation_product.status})"
        reason_label = _REASON_LABEL.get(interrupt.reason, interrupt.reason)
        lines.append(
            f"\n{i}. {reason_label}\n"
            f"   about: {about}\n"
            f"   issue: {interrupt.subject}\n"
            f"   reply: #{interrupt.id[:8]} <your answer>"
        )
    if interrupt_total > len(interrupts):
        lines.append(f"\n...and {interrupt_total - len(interrupts)} more.")

    return "\n".join(lines)
