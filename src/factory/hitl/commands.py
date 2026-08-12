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


async def build_status_report(session: AsyncSession) -> str:
    """A factual snapshot: every active product's name, stage, and the same
    numbers `GROWTH_ANALYST` reads (`tools/growth.py::read_product_growth`) --
    views, interest signals, leads, whether an AB test is running -- plus what
    (not just how many) escalations are waiting on a human right now.

    Each escalation names the product it's about, by id lookup, independent of
    whether that product still appears in the active list above -- a heal-
    exhaustion escalation for a since-retired product must not read as though
    it belongs to whatever is currently active, which is exactly what a bare
    count next to one active product's listing looked like.
    """
    products = await repository.active_products(session, limit=_MAX_PRODUCTS)
    interrupt_total = await repository.count_open_interrupts(session)
    interrupts = await repository.list_open_interrupts(session, limit=_MAX_PRODUCTS)

    lines = ["\U0001f4ca Factory status"]
    if not products:
        lines.append("\nNo active products yet.")
    else:
        since = datetime.now(UTC) - timedelta(days=7)
        lines.append(f"\n{len(products)} active product(s):")
        for product in products:
            views = await repository.tally_page_views(session, product.id, since=since)
            signals = await repository.tally_validation_signals(session, product.id)
            leads = await repository.count_leads(session, product.id)
            ab_test = await repository.get_running_ab_test(session, product.id)
            lines.append(
                f"\n• {product.name}\n"
                f"  {product.status} | {product.deploy_url or 'no URL yet'}\n"
                f"  views (7d): {views} | signals: {signals} | leads: {leads}"
                + (" | AB test running" if ab_test else "")
            )

    lines.append(f"\nOpen escalations ({interrupt_total}):")
    for interrupt in interrupts:
        where = ""
        if interrupt.product_id:
            escalation_product = await repository.get_product(session, interrupt.product_id)
            if escalation_product is not None:
                # No truncation here, unlike the active-product listing above being
                # this section's only real distinguishing text: a name mid-cut right
                # before the one word that actually told two similarly-worded
                # products apart read as confusingly identical to the untruncated
                # name shown above it -- found live. `Product.name` is capped at 80
                # chars at creation (idea.target_niche[:80]) and Telegram's own
                # message cap is 3900 chars for the whole report, so showing it in
                # full here is always safe.
                where = f" [{escalation_product.name} -- {escalation_product.status}]"
        lines.append(f"\n• #{interrupt.id[:8]} ({interrupt.reason}){where}\n  {interrupt.subject}")
    if interrupt_total > len(interrupts):
        lines.append(f"\n...and {interrupt_total - len(interrupts)} more.")

    return "\n".join(lines)
