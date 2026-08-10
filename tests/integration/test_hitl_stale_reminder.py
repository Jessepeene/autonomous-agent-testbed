"""The stale-interrupt reminder — the safety net for a missed Telegram message.

`deliver_pending` only guarantees an escalation was *sent*; nothing previously
guaranteed a human actually saw it. `remind_stale` sends one consolidated nudge for
every open interrupt that has aged past a threshold, so a missed or forgotten
escalation doesn't sit silently forever.

Needs Postgres for the JSONB task payloads:

    docker compose up -d postgres
    alembic upgrade head
    pytest tests/integration -q
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from factory.engines.maintenance import run as run_maintenance
from factory.hitl.interrupts import remind_stale
from factory.llm.client import LLMClient
from factory.llm.providers.base import Provider
from factory.llm.providers.fake import FakeProvider
from factory.llm.router import ProviderRegistry
from factory.sandbox.disabled import DisabledSandbox
from factory.settings import EngineName, Settings
from factory.state import repository
from factory.state.orm import Base, Interrupt, Task
from factory.state.schemas import InterruptReason, TaskKind, TaskStatus
from factory.supervisor.context import FactoryContext

DATABASE_URL = os.environ.get("DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not DATABASE_URL, reason="DATABASE_URL is not set; integration tests need Postgres"
)


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
    def __init__(self, *, available: bool = True) -> None:
        self._available = available
        self.sent: list[str] = []

    @property
    def available(self) -> bool:
        return self._available

    async def send(self, text: str) -> bool:
        self.sent.append(text)
        return True

    async def poll_replies(self) -> list[str]:
        return []


async def _open_interrupt_aged(session: AsyncSession, *, subject: str, age: timedelta) -> str:
    interrupt_id = await repository.open_interrupt(
        session,
        reason=InterruptReason.MISSING_CREDENTIAL,
        subject=subject,
        detail="set it",
    )
    assert interrupt_id is not None
    await session.execute(
        update(Interrupt)
        .where(Interrupt.id == interrupt_id)
        .values(created_at=datetime.now(UTC) - age)
    )
    await session.flush()
    return interrupt_id


async def test_nothing_stale_sends_no_reminder(session: AsyncSession) -> None:
    await _open_interrupt_aged(session, subject="fresh one", age=timedelta(hours=1))
    notifier = _ScriptedNotifier()

    reminded = await remind_stale(session, notifier, older_than=timedelta(hours=12))

    assert reminded == 0
    assert notifier.sent == []


async def test_stale_interrupts_get_one_consolidated_message(session: AsyncSession) -> None:
    await _open_interrupt_aged(session, subject="STRIPE_API_KEY missing", age=timedelta(hours=20))
    await _open_interrupt_aged(session, subject="MAILGUN_API_KEY missing", age=timedelta(days=3))
    await _open_interrupt_aged(session, subject="still fresh", age=timedelta(hours=2))
    notifier = _ScriptedNotifier()

    reminded = await remind_stale(session, notifier, older_than=timedelta(hours=12))

    assert reminded == 2
    assert len(notifier.sent) == 1
    text = notifier.sent[0]
    assert "STRIPE_API_KEY missing" in text
    assert "MAILGUN_API_KEY missing" in text
    assert "still fresh" not in text


async def test_no_notifier_available_sends_nothing_and_does_not_crash(
    session: AsyncSession,
) -> None:
    await _open_interrupt_aged(session, subject="STRIPE_API_KEY missing", age=timedelta(hours=20))
    notifier = _ScriptedNotifier(available=False)

    reminded = await remind_stale(session, notifier, older_than=timedelta(hours=12))

    assert reminded == 0
    assert notifier.sent == []


async def test_resolved_interrupts_are_never_reminded(session: AsyncSession) -> None:
    interrupt_id = await _open_interrupt_aged(
        session, subject="STRIPE_API_KEY missing", age=timedelta(days=5)
    )
    await repository.resolve_interrupt(session, interrupt_id, "fixed")
    notifier = _ScriptedNotifier()

    reminded = await remind_stale(session, notifier, older_than=timedelta(hours=12))

    assert reminded == 0
    assert notifier.sent == []


def _ctx(*, notifier: _ScriptedNotifier, workspace_root: Path) -> FactoryContext:
    settings = Settings(
        database_url=DATABASE_URL,
        gemini_api_key="test-key",
        workspace_root=workspace_root,
        stale_interrupt_reminder_hours=12,
    )
    llm = LLMClient(ProviderRegistry({Provider.GEMINI: FakeProvider()}), settings=settings)
    return FactoryContext(
        settings=settings,
        llm=llm,
        notifier=notifier,
        sandbox=DisabledSandbox(),
        enabled_engines={EngineName.SUPERVISOR},
        worker_id="maintenance-test",
    )


async def test_maintenance_run_dispatches_the_slug_to_remind_stale(
    session: AsyncSession, tmp_path: Path
) -> None:
    await _open_interrupt_aged(session, subject="STRIPE_API_KEY missing", age=timedelta(days=1))
    task_id = await repository.enqueue_task(
        session,
        kind=TaskKind.MAINTENANCE,
        engine=EngineName.SUPERVISOR,
        idempotency_key="maintenance:remind-stale-interrupts:test",
        payload={"slug": "remind-stale-interrupts"},
    )
    assert task_id is not None
    task = await session.get(Task, task_id)
    assert task is not None
    task.status = TaskStatus.CLAIMED.value

    notifier = _ScriptedNotifier()
    ctx = _ctx(notifier=notifier, workspace_root=tmp_path)

    summary = await run_maintenance(ctx, session, task)

    assert "1 stale interrupt" in summary
    assert len(notifier.sent) == 1
    assert "STRIPE_API_KEY missing" in notifier.sent[0]
