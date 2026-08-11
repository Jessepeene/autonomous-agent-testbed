"""Maintenance tasks must always dispatch to the engine that actually handles
`TaskKind.MAINTENANCE`.

Regression coverage for a bug found in the 2026-08-10 full-codebase audit:
`enqueue_maintenance` used to tag a task with its catalogue item's *topic* engine
(e.g. DEVELOPMENT for `audit-live-products`), which is a different engine from the
one whose `run()` actually implements maintenance handling (SUPERVISOR only, see
`engines/base.py`). Any item not already tagged SUPERVISOR was claimed by an engine
that had never heard of `TaskKind.MAINTENANCE`, returned an "unsupported task kind"
string, and completed instantly -- so `audit-live-products`, `improve-test-coverage`,
and `reconcile-outreach-conversions` had never actually executed once.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from factory.settings import EngineName
from factory.state.orm import Base, Task
from factory.state.schemas import TaskKind
from factory.supervisor.backlog import CATALOGUE, enqueue_maintenance

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


async def test_every_enqueued_maintenance_task_is_dispatched_to_supervisor(
    session: AsyncSession,
) -> None:
    topic_engines = {item.engine for item in CATALOGUE}
    assert topic_engines != {EngineName.SUPERVISOR}, (
        "catalogue no longer has any non-SUPERVISOR topic engines; this test's "
        "premise is gone, but the assertions below still hold trivially"
    )

    created = await enqueue_maintenance(session, enabled_engines=set(EngineName))
    await session.commit()

    assert set(created) == {item.slug for item in CATALOGUE}

    rows = (
        (await session.execute(select(Task).where(Task.kind == TaskKind.MAINTENANCE.value)))
        .scalars()
        .all()
    )
    assert len(rows) == len(CATALOGUE)
    assert {t.engine for t in rows} == {EngineName.SUPERVISOR.value}


async def test_previously_misrouted_slugs_are_now_tagged_supervisor(
    session: AsyncSession,
) -> None:
    previously_broken = {
        "audit-live-products",
        "improve-test-coverage",
        "reconcile-outreach-conversions",
    }
    await enqueue_maintenance(session, enabled_engines=set(EngineName))
    await session.commit()

    rows = (
        (await session.execute(select(Task).where(Task.kind == TaskKind.MAINTENANCE.value)))
        .scalars()
        .all()
    )
    by_slug = {r.payload["slug"]: r for r in rows}
    for slug in previously_broken:
        assert by_slug[slug].engine == EngineName.SUPERVISOR.value


async def test_topic_engine_still_gates_relevance(session: AsyncSession) -> None:
    """`item.engine` (the topic tag) must still control whether an item is enqueued
    at all -- only its use as the *task's* engine was wrong."""
    created = await enqueue_maintenance(
        session, enabled_engines={EngineName.SUPERVISOR, EngineName.MARKETING}
    )
    await session.commit()

    development_only_slugs = {
        item.slug for item in CATALOGUE if item.engine == EngineName.DEVELOPMENT
    }
    assert development_only_slugs, "fixture assumption: at least one DEVELOPMENT-topic item"
    assert not (development_only_slugs & set(created))
