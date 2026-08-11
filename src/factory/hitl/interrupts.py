"""Human-in-the-loop escalation.

Human intervention is a system failure, so the surface is deliberately narrow:
exactly two reasons are representable, and the enum makes a third impossible to
express rather than merely discouraged.

Interrupts are rows, not in-memory futures. An escalation raised at 02:00 and
answered at 09:00 survives every reboot in between.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from factory.logging import get_logger
from factory.settings import EngineName
from factory.state import repository
from factory.state.orm import Interrupt, Task
from factory.state.schemas import InterruptReason, TaskKind

log = get_logger(__name__)

#: Replies are matched back to their interrupt by this tag.
_TAG = re.compile(r"#([0-9a-f]{8})\b", re.IGNORECASE)

#: A reply containing this word, to an interrupt whose task exhausted its retries,
#: reopens that task instead of only marking the interrupt resolved -- the escalation
#: message itself already tells a human to "retry" once they've fixed the cause (see
#: graph.py's DevOpsError/generic-failure escalation text), so this is the word they
#: already expect to use.
_RETRY_WORD = re.compile(r"\bretry\b", re.IGNORECASE)

#: Same idea, for a heal-exhausted product (no task_id -- see graph.py's
#: "Reply with guidance, or REJECT to retire this product" escalation text). Any
#: other non-empty reply is treated as guidance for one more patch attempt.
_REJECT_WORD = re.compile(r"\breject\b", re.IGNORECASE)

#: A flagged idea's escalation (compliance/graph.py's FLAG_FOR_REVIEW branch) asks
#: for APPROVE or REJECT specifically -- distinct vocabulary from the task-retry and
#: product-guidance replies above, matched only once the interrupt is identified as
#: an ASSESS_COMPLIANCE escalation (see _act_on_idea_reply).
_APPROVE_WORD = re.compile(r"\bapprove\b", re.IGNORECASE)


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
    product_id: str | None = None,
) -> str | None:
    """Raise an escalation. Duplicate open subjects collapse into the existing one."""
    interrupt_id = await repository.open_interrupt(
        session,
        reason=reason,
        subject=subject,
        detail=detail,
        task_id=task_id,
        product_id=product_id,
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
    """Resolve interrupts from tagged human replies.

    A reply containing "retry" to an interrupt tied to a specific task (one whose
    detail explicitly asked for this — see `graph.py`'s escalation text) reopens
    that task, not just the interrupt. Without this, confirming the root cause is
    fixed had no code path back to actually retrying: the interrupt marked itself
    "resolved" and the task stayed `escalated` forever, needing manual DB surgery to
    move again — the exact gap that produced every escalation this deployment ever
    needed a human for.

    A reply to a heal-exhausted product's interrupt (no `task_id` — there is no
    single task to retry, see `graph.py`'s `_heal()`) is handled separately: REJECT
    retires the product for good, anything else is guidance for exactly one more
    patch attempt. Before this, that escalation's own "Reply with guidance, or
    REJECT to retire this product" text was aspirational — nothing read the reply at
    all, and the product just sat in `PLANNED` forever regardless of what a human
    said.

    A reply to a flagged idea's interrupt (`task_id` set, no `product_id` — see
    `compliance/graph.py`'s FLAG_FOR_REVIEW branch, raised before any product
    exists) is handled the same way: before this, its own "Reply APPROVE to allow
    the build, or REJECT to discard the idea" text was equally aspirational --
    neither word did anything, and the idea just sat `flagged` forever, invisible to
    every pipeline count but never actually resolved either way.
    """
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
        await repository.resolve_interrupt(session, target.id, answer or "acknowledged")
        action = "none"
        target_task = await session.get(Task, target.task_id) if target.task_id else None
        if (
            target_task is not None
            and target_task.kind == TaskKind.ASSESS_COMPLIANCE.value
            and target_task.idea_id
        ):
            action = await _act_on_idea_reply(session, target_task.idea_id, answer)
        elif target.task_id and _RETRY_WORD.search(answer):
            action = (
                "retried"
                if await repository.reopen_escalated_task(session, target.task_id)
                else "none"
            )
        elif target.product_id and not target.task_id:
            action = await _act_on_product_reply(session, target.id, target.product_id, answer)
        log.info("hitl.resolved", interrupt_id=target.id, action=action)
        resolved += 1
    return resolved


async def _act_on_idea_reply(session: AsyncSession, idea_id: str, answer: str) -> str:
    if _REJECT_WORD.search(answer):
        await repository.set_idea_status(session, idea_id, "rejected")
        return "rejected"
    if _APPROVE_WORD.search(answer):
        await repository.set_idea_status(session, idea_id, "approved")
        # A fresh idempotency key -- the original ASSESS_COMPLIANCE task already
        # completed -- but the same task kind, so compliance/graph.py's run() takes
        # its existing "already approved on a previous pass" short-circuit straight
        # to _create_validation: zero further LLM spend, and the real
        # max_concurrent_validations capacity check runs instead of this module
        # duplicating that business rule.
        await repository.enqueue_task(
            session,
            kind=TaskKind.ASSESS_COMPLIANCE,
            engine=EngineName.COMPLIANCE,
            idempotency_key=f"compliance:idea:{idea_id}:approved",
            payload={"idea_id": idea_id},
            idea_id=idea_id,
            priority=50,
        )
        return "approved"
    return "none"


async def _act_on_product_reply(
    session: AsyncSession, interrupt_id: str, product_id: str, answer: str
) -> str:
    if _REJECT_WORD.search(answer):
        retired = await repository.retire_product(session, product_id)
        return "retired" if retired else "none"
    if not answer:
        return "none"
    task_id = await repository.enqueue_guided_patch(
        session, product_id=product_id, guidance=answer, interrupt_id=interrupt_id
    )
    return "patch_queued" if task_id else "none"


async def _find_open_by_tag(session: AsyncSession, tag: str) -> Interrupt | None:
    from sqlalchemy import select

    stmt = select(Interrupt).where(Interrupt.status == "open")
    for row in (await session.execute(stmt)).scalars().all():
        if tag_for(row.id) == tag:
            return row
    return None


async def remind_stale(session: AsyncSession, notifier: Notifier, *, older_than: timedelta) -> int:
    """Send one consolidated nudge for interrupts a human has had time to see but not
    answered.

    `deliver_pending` only guarantees the *first* message went out; it does not
    guarantee a human read it. A reminder that fires per-interrupt would spam one
    message per stale escalation every cadence window it stays open -- instead this
    sends exactly one summary listing all of them, so a factory with several stale
    escalations produces one Telegram message, not several.
    """
    if not notifier.available:
        return 0
    stale = await repository.stale_open_interrupts(session, older_than=older_than)
    if not stale:
        return 0
    lines = [f"#{tag_for(row.id)} ({_age(row.created_at)} old): {row.subject}" for row in stale]
    text = (
        f"⏰ {len(stale)} escalation(s) still waiting on a reply:\n\n"
        + "\n".join(lines)
        + '\n\nReply with: #<tag> <your answer> (include "retry" to reopen a stuck task)'
    )
    await notifier.send(text)
    log.info("hitl.reminded", count=len(stale))
    return len(stale)


def _age(created_at: datetime) -> str:
    delta = datetime.now(UTC) - created_at
    hours = int(delta.total_seconds() // 3600)
    if hours < 24:
        return f"{hours}h"
    return f"{hours // 24}d"
