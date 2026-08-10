"""Two different tasks failing the same way must both reach a human.

Before this, `_run_engine`'s generic escalation subjects were templated only on
`engine.value` -- e.g. "development task exhausted its retries" -- with no
per-task identifier. `interrupts.request` dedupes on (reason, subject, status),
so the *second* task on a given engine to exhaust its retries silently collided
with whatever earlier, possibly already-irrelevant interrupt happened to still be
open with that exact subject, and never reached Telegram at all. Observed live:
a product's patch task hit this and the escalation was invisible until the DB
was inspected directly.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from factory.hitl.telegram import NullNotifier
from factory.llm.client import LLMClient
from factory.llm.providers.base import Provider
from factory.llm.providers.fake import FakeProvider
from factory.llm.router import ProviderRegistry
from factory.sandbox.disabled import DisabledSandbox
from factory.settings import EngineName, Settings
from factory.state import db as db_module
from factory.state import repository
from factory.state.orm import Base, Interrupt, Task
from factory.state.schemas import TaskKind, TaskStatus
from factory.supervisor import graph as sup_graph
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


@pytest_asyncio.fixture(autouse=True)
async def _reset_global_engine() -> AsyncIterator[None]:
    """`_run_engine` opens sessions through the process-wide cached engine
    (`session_scope` -> `get_sessionmaker`), a different connection than this
    file's `session` fixture. Reset it around the test so no pooled connection
    leaks into a later test file."""
    yield
    await db_module.dispose_engine()


def _ctx(*, workspace_root: Path) -> FactoryContext:
    settings = Settings(
        database_url=DATABASE_URL,
        gemini_api_key="test-key",
        workspace_root=workspace_root,
    )
    llm = LLMClient(ProviderRegistry({Provider.GEMINI: FakeProvider()}), settings=settings)
    return FactoryContext(
        settings=settings,
        llm=llm,
        notifier=NullNotifier(),
        sandbox=DisabledSandbox(),
        enabled_engines={EngineName.DEVELOPMENT},
        worker_id="subject-uniqueness-test",
    )


async def _always_fails(ctx: object, session: object, task: object) -> str:
    raise RuntimeError("boom")


async def test_two_tasks_that_exhaust_the_same_way_both_get_their_own_interrupt(
    session: AsyncSession, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sup_graph, "get_runner", lambda engine: _always_fails)

    first_task_id = await repository.enqueue_task(
        session,
        kind=TaskKind.PATCH_PRODUCT,
        engine=EngineName.DEVELOPMENT,
        idempotency_key="subject-uniqueness:1",
    )
    second_task_id = await repository.enqueue_task(
        session,
        kind=TaskKind.PATCH_PRODUCT,
        engine=EngineName.DEVELOPMENT,
        idempotency_key="subject-uniqueness:2",
    )
    assert first_task_id is not None
    assert second_task_id is not None
    # `_run_engine`'s exception handlers call `fail_task` without passing
    # `ctx.settings` through, so exhaustion there is judged against whatever
    # `get_settings()` resolves to in-process, not this test's `ctx.settings` --
    # harmless in production (they're the same object there), but it means this
    # test can't rely on a custom `max_task_attempts` to force exhaustion on the
    # first failure. Pre-set attempt_count high enough to exhaust regardless of
    # environment (max_task_attempts is capped at 10).
    await session.execute(
        update(Task).where(Task.id.in_([first_task_id, second_task_id])).values(attempt_count=99)
    )
    await session.commit()

    ctx = _ctx(workspace_root=tmp_path)
    await sup_graph._run_engine({}, ctx=ctx, engine=EngineName.DEVELOPMENT)
    await sup_graph._run_engine({}, ctx=ctx, engine=EngineName.DEVELOPMENT)

    tasks = (
        (await session.execute(select(Task).where(Task.id.in_([first_task_id, second_task_id]))))
        .scalars()
        .all()
    )
    assert len(tasks) == 2
    assert all(t.status == TaskStatus.ESCALATED.value for t in tasks)

    interrupts = (
        (await session.execute(select(Interrupt).where(Interrupt.status == "open"))).scalars().all()
    )
    assert len(interrupts) == 2
    subjects = {i.subject for i in interrupts}
    assert len(subjects) == 2, f"expected two distinct subjects, got {subjects}"
    assert {i.task_id for i in interrupts} == {first_task_id, second_task_id}
