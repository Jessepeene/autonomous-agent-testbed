"""Reply-driven task retry — the fix for a real, live-observed gap.

Before ``reopen_escalated_task`` existed, ``collect_replies`` only ever called
``resolve_interrupt``: confirming the root cause of an escalation was fixed had no
code path back to actually retrying the task, which stayed ``escalated`` forever and
needed manual DB surgery to move again. This is the exact gap that produced every
escalation this deployment ever needed a human for.

Needs Postgres for the JSONB task payloads:

    docker compose up -d postgres
    alembic upgrade head
    pytest tests/integration -q
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from factory.hitl.interrupts import collect_replies
from factory.settings import EngineName, Settings
from factory.state import repository
from factory.state.orm import Base, Interrupt, Task
from factory.state.schemas import InterruptReason, TaskKind, TaskStatus

DATABASE_URL = os.environ.get("DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not DATABASE_URL, reason="DATABASE_URL is not set; integration tests need Postgres"
)

_FAIL_SETTINGS = Settings(database_url=DATABASE_URL, gemini_api_key="test-key", max_task_attempts=1)


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(DATABASE_URL or "", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as db:
        yield db
        await db.rollback()
    await engine.dispose()


class _ScriptedNotifier:
    """A fake Telegram channel: replies are scripted, sends are recorded."""

    def __init__(self, replies: list[str]) -> None:
        self._replies = replies
        self.sent: list[str] = []

    @property
    def available(self) -> bool:
        return True

    async def send(self, text: str) -> bool:
        self.sent.append(text)
        return True

    async def poll_replies(self) -> list[str]:
        replies, self._replies = self._replies, []
        return replies


async def _escalate_task(session: AsyncSession, *, idempotency_key: str) -> str:
    """Drive a fresh task straight to ``escalated`` via the real claim/fail cycle."""
    task_id = await repository.enqueue_task(
        session,
        kind=TaskKind.PATCH_PRODUCT,
        engine=EngineName.DEVELOPMENT,
        idempotency_key=idempotency_key,
    )
    assert task_id is not None
    claimed = await repository.claim_next_task(
        session, worker_id="w", engines=[EngineName.DEVELOPMENT]
    )
    assert claimed is not None and claimed.id == task_id
    exhausted = await repository.fail_task(session, task_id, "boom", settings=_FAIL_SETTINGS)
    assert exhausted is True
    return task_id


async def test_a_reply_without_retry_only_resolves_the_interrupt(session: AsyncSession) -> None:
    task_id = await _escalate_task(session, idempotency_key="hitl-retry:no-word")
    interrupt_id = await repository.open_interrupt(
        session,
        reason=InterruptReason.LEGAL_UNCERTAINTY,
        subject="patch failed",
        detail="fix it",
        headline="Test escalation",
        reply_hint="Reply retry once you believe the underlying cause is addressed.",
        task_id=task_id,
    )
    assert interrupt_id is not None
    notifier = _ScriptedNotifier([f"#{interrupt_id[:8]} looking into it"])

    resolved = await collect_replies(session, notifier)

    assert resolved == 1
    interrupt = await session.get(Interrupt, interrupt_id)
    assert interrupt is not None
    assert interrupt.status == "resolved"
    task = await session.get(Task, task_id)
    assert task is not None
    assert task.status == TaskStatus.ESCALATED.value


async def test_a_reply_containing_retry_reopens_the_escalated_task(session: AsyncSession) -> None:
    task_id = await _escalate_task(session, idempotency_key="hitl-retry:word")
    interrupt_id = await repository.open_interrupt(
        session,
        reason=InterruptReason.LEGAL_UNCERTAINTY,
        subject="patch failed again",
        detail="fix it",
        headline="Test escalation",
        reply_hint="Reply retry once you believe the underlying cause is addressed.",
        task_id=task_id,
    )
    assert interrupt_id is not None
    notifier = _ScriptedNotifier([f"#{interrupt_id[:8]} fixed the root cause, please retry"])

    resolved = await collect_replies(session, notifier)

    assert resolved == 1
    interrupt = await session.get(Interrupt, interrupt_id)
    assert interrupt is not None
    assert interrupt.status == "resolved"
    task = await session.get(Task, task_id)
    assert task is not None
    assert task.status == TaskStatus.PENDING.value
    assert task.attempt_count == 0
    assert task.last_error is None


async def test_retry_word_on_an_interrupt_with_no_task_id_does_not_crash(
    session: AsyncSession,
) -> None:
    interrupt_id = await repository.open_interrupt(
        session,
        reason=InterruptReason.MISSING_CREDENTIAL,
        subject="STRIPE_API_KEY is not configured",
        detail="set it",
        headline="Test escalation",
        reply_hint="No reply needed here — set the credential and restart the factory.",
        needs_reply=False,
    )
    assert interrupt_id is not None
    notifier = _ScriptedNotifier([f"#{interrupt_id[:8]} retry"])

    resolved = await collect_replies(session, notifier)

    assert resolved == 1
    interrupt = await session.get(Interrupt, interrupt_id)
    assert interrupt is not None
    assert interrupt.status == "resolved"


async def test_retry_word_is_case_insensitive_and_matches_as_a_whole_word(
    session: AsyncSession,
) -> None:
    task_id = await _escalate_task(session, idempotency_key="hitl-retry:case")
    interrupt_id = await repository.open_interrupt(
        session,
        reason=InterruptReason.LEGAL_UNCERTAINTY,
        subject="patch failed a third time",
        detail="fix it",
        headline="Test escalation",
        reply_hint="Reply retry once you believe the underlying cause is addressed.",
        task_id=task_id,
    )
    assert interrupt_id is not None
    notifier = _ScriptedNotifier([f"#{interrupt_id[:8]} RETRY please"])

    resolved = await collect_replies(session, notifier)

    assert resolved == 1
    task = await session.get(Task, task_id)
    assert task is not None
    assert task.status == TaskStatus.PENDING.value
