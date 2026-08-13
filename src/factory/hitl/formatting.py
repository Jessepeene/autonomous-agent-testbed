"""The one place an escalation's Telegram text is assembled.

Lives separately from both `interrupts.py` (raises/resolves escalations) and
`commands.py` (`/status`, `/issue`) so neither has to import the other for this:
`interrupts.py` already imports `commands` to dispatch `/status`/`/issue` from
its single reply-polling loop, and this codebase's own established precedent for
avoiding the resulting cycle (`repository.find_open_interrupt_by_tag`'s
docstring) is to give shared logic its own home rather than invert or duplicate
it. This module imports neither of them.

Before this, the pushed message and the `/status`/`/issue` views built their text
from just (reason, subject, detail) plus a generic, always-identical
"Reply with: #tag <your answer>" footer. That produced two real problems, found
by auditing every one of the ~18 escalation call sites in this codebase:
reply vocabulary was inconsistent across sites (APPROVE/REJECT, RESUME, SKIP,
"retry", "reply with anything", or nothing at all), and a fixed per-`reason`
label was actively misleading -- the same `LEAD_SOURCING_STALLED` reason covers
both "the API key is being rejected" (an account problem) and "the request is
malformed" (a code bug), which need different explanations, not one shared one.

`headline` and `reply_hint` (both on `Interrupt`, see migration
`0020_escalation_headline.py`) let every raise site supply its own precise
category and exact instruction instead.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from factory.state import repository
from factory.state.orm import Interrupt

_SEPARATOR = "─" * 16  # a plain horizontal rule; Telegram sends plain text


def tag_for(interrupt_id: str) -> str:
    return interrupt_id[:8]


async def build_escalation_text(session: AsyncSession, interrupt: Interrupt) -> str:
    """The full text for one escalation -- used for the initial Telegram push
    (`interrupts.py::deliver_pending`) and the `/issue #<tag>` drill-down
    (`commands.py::build_issue_detail`), so what an escalation says never
    depends on which of the two paths produced it or on scrolling back through
    Telegram history to find the original push.
    """
    lines: list[str] = []

    if interrupt.product_id:
        product = await repository.get_product(session, interrupt.product_id)
        if product is not None:
            lines.append(f"\U0001f4e6 {product.name}")

    # Historical-row/defensive fallback only -- every live raise site sets
    # these (`headline`/`reply_hint` are required kwargs on
    # `interrupts.request()`), so this branch should not be reachable for a
    # newly-raised escalation.
    lines.append(interrupt.headline or f"[{interrupt.reason.upper()}]")
    lines.append("")
    lines.append("\U0001f50d The problem:")
    lines.append(interrupt.detail)
    lines.append("")
    lines.append(_SEPARATOR)
    lines.append("\U0001f449 What to do:")
    lines.append(interrupt.reply_hint or "Reply with your answer.")
    tag = tag_for(interrupt.id)
    if interrupt.needs_reply:
        lines.append(f"Include this tag in your reply: #{tag}")
    else:
        lines.append(f"Tag: #{tag}")

    return "\n".join(lines)
