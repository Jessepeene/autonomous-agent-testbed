"""Human-in-the-loop escalation.

Human intervention is a system failure, so the surface is deliberately narrow:
exactly two reasons are representable, and the enum makes a third impossible to
express rather than merely discouraged.

Interrupts are rows, not in-memory futures. An escalation raised at 02:00 and
answered at 09:00 survives every reboot in between.
"""

from __future__ import annotations

import re
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from factory.logging import get_logger
from factory.state import repository
from factory.state.schemas import InterruptReason

log = get_logger(__name__)

#: Replies are matched back to their interrupt by this tag.
_TAG = re.compile(r"#([0-9a-f]{8})\b", re.IGNORECASE)


class Notifier(Protocol):
    @property
    def available(self) -> bool: ...

    async def send(self, text: str) -> bool: ...

    async def poll_replies(self) -> list[str]: ...


def tag_for(interrupt_id: str) -> str:
    return interrupt_id[:8]


async def request(
    session: AsyncSession,
    *,
    reason: InterruptReason,
    subject: str,
    detail: str,
    task_id: str | None = None,
) -> str | None:
    """Raise an escalation. Duplicate open subjects collapse into the existing one."""
    interrupt_id = await repository.open_interrupt(
        session, reason=reason, subject=subject, detail=detail, task_id=task_id
    )
    if interrupt_id is None:
        log.debug("hitl.duplicate", subject=subject)
        return None
    log.warning("hitl.raised", reason=str(reason), subject=subject, interrupt_id=interrupt_id)
    return interrupt_id


def format_message(interrupt_id: str, reason: str, subject: str, detail: str) -> str:
    return (
        f"[{reason.upper()}] {subject}\n\n"
        f"{detail}\n\n"
        f"Reply with: #{tag_for(interrupt_id)} <your answer>"
    )


async def deliver_pending(session: AsyncSession, notifier: Notifier) -> int:
    """Push undelivered escalations to the human channel.

    When no channel is configured the rows simply stay undelivered — they are not
    dropped, so configuring Telegram later flushes the entire backlog.
    """
    if not notifier.available:
        return 0
    delivered = 0
    for interrupt in await repository.undelivered_interrupts(session):
        text = format_message(interrupt.id, interrupt.reason, interrupt.subject, interrupt.detail)
        if await notifier.send(text):
            await repository.mark_interrupt_delivered(session, interrupt.id)
            delivered += 1
    if delivered:
        log.info("hitl.delivered", count=delivered)
    return delivered


async def collect_replies(session: AsyncSession, notifier: Notifier) -> int:
    """Resolve interrupts from tagged human replies."""
    if not notifier.available:
        return 0
    resolved = 0
    for message in await notifier.poll_replies():
        match = _TAG.search(message)
        if match is None:
            continue
        tag = match.group(1).lower()
        answer = _TAG.sub("", message, count=1).strip()
        target = await _find_open_by_tag(session, tag)
        if target is None:
            continue
        await repository.resolve_interrupt(session, target, answer or "acknowledged")
        log.info("hitl.resolved", interrupt_id=target)
        resolved += 1
    return resolved


async def _find_open_by_tag(session: AsyncSession, tag: str) -> str | None:
    from sqlalchemy import select

    from factory.state.orm import Interrupt

    stmt = select(Interrupt.id).where(Interrupt.status == "open")
    for row in (await session.execute(stmt)).scalars().all():
        if tag_for(row) == tag:
            return str(row)
    return None
