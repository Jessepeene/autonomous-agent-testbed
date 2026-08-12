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
    views, interest signals, leads, whether an AB test is running -- plus how
    many escalations are waiting on a human right now."""
    products = await repository.active_products(session, limit=_MAX_PRODUCTS)
    open_interrupts = await repository.count_open_interrupts(session)

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

    lines.append(f"\nOpen escalations: {open_interrupts}")
    return "\n".join(lines)
