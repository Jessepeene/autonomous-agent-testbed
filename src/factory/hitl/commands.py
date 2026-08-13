"""``/status`` and ``/issue #<tag>`` -- Telegram commands a human can send the
factory, as opposed to a tagged reply to a specific escalation
(`#<tag> <answer>`). `/status` is the summary; `/issue` is the drill-down --
`/status` only shows one line per escalation (`issue: <subject>`), and the
full original detail text (what a human actually needs to answer it) lives
one command away rather than requiring a scroll back through Telegram history.

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

from factory.hitl.formatting import build_escalation_text, tag_for
from factory.state import repository

#: Telegram appends "@BotName" to a command in a group chat; a bare "/status"
#: is what a 1:1 chat sends. Matches either, and ignores anything after.
STATUS_COMMAND = re.compile(r"^/status(@\w+)?\b", re.IGNORECASE)

#: "/issue #a1b2c3d4" or "/issue a1b2c3d4" -- the "#" is optional since /status
#: itself prints the tag with one, and typing it back is the natural instinct.
ISSUE_COMMAND = re.compile(r"^/issue(@\w+)?\s+#?([0-9a-f]{8})\b", re.IGNORECASE)

_MAX_PRODUCTS = 10


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
    # Already grouped by product (`list_open_interrupts` sorts that way) --
    # entries below preserve that order rather than re-sorting.
    for i, interrupt in enumerate(interrupts, start=1):
        about = "(not tied to a specific product)"
        if interrupt.product_id:
            escalation_product = await repository.get_product(session, interrupt.product_id)
            if escalation_product is not None:
                about = f"{escalation_product.name} ({escalation_product.status})"
        headline = interrupt.headline or interrupt.reason
        lines.append(
            f"\n{i}. {about}\n"
            f"   {headline}\n"
            f"   issue: {interrupt.subject}\n"
            f"   tag: #{tag_for(interrupt.id)}"
        )
    if interrupt_total > len(interrupts):
        lines.append(f"\n...and {interrupt_total - len(interrupts)} more.")
    if interrupts:
        lines.append("\nSend /issue #<tag> for the full detail on any one of these.")

    return "\n".join(lines)


async def build_issue_detail(session: AsyncSession, tag: str) -> str:
    """The full text behind one escalation's short `issue:` line in `/status` --
    identical to what was originally pushed to Telegram (`hitl/formatting.py`),
    so nothing here duplicates that template or drifts from it. Saves hunting
    back through Telegram history for the original push message (which may
    have scrolled off, or arrived before the human was watching).

    Scoped to *open* interrupts only, same as a tagged reply (`#<tag> <answer>`)
    -- a resolved one has nothing left to act on.
    """
    interrupt = await repository.find_open_interrupt_by_tag(session, tag)
    if interrupt is None:
        return f"No open escalation matches #{tag}. Send /status for the current list."
    return await build_escalation_text(session, interrupt)
