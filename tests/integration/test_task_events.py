"""The causal trail — the query that did not exist.

Answering "what happened to this product" used to mean joining `tasks`,
`heal_attempts`, `llm_calls` and `interrupts` by hand and inferring the gaps, with
`tasks.last_error` overwritten on every attempt so earlier failures were simply
gone. Two days of real incidents were diagnosed that way, and the log lines that
would have explained them were destroyed by the next deploy.

Needs Postgres:

    docker compose up -d postgres
    alembic upgrade head
    pytest tests/integration -q
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from factory.settings import EngineName
from factory.state import repository
from factory.state.orm import Base
from factory.state.schemas import ScoredIdea, TaskKind

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


async def _make_product(session: AsyncSession) -> str:
    idea = ScoredIdea(
        problem_statement="Bookkeepers reconcile invoices by hand every single week.",
        target_niche="trail-test",
        source_url="https://example.test/thread",
        monetization_hypothesis="EUR 49/month per seat.",
        pain_severity=0.8,
        willingness_to_pay=0.7,
        build_feasibility=0.6,
        adoption_ease=0.8,
    )
    idea_id = await repository.upsert_idea(session, idea)
    assert idea_id is not None
    return await repository.create_product(session, name="trail-test", idea_id=idea_id)


async def test_a_products_history_reads_as_one_ordered_chain(session: AsyncSession) -> None:
    product_id = await _make_product(session)

    for event in ("engine.start", "council.verdict", "engine.done"):
        await repository.record_task_event(
            session, event=event, product_id=product_id, engine=EngineName.DEVELOPMENT
        )
    await session.commit()

    history = await repository.product_history(session, product_id)
    assert [e.event for e in history] == ["engine.start", "council.verdict", "engine.done"]
    assert all(e.engine == EngineName.DEVELOPMENT.value for e in history)


async def test_caused_by_links_a_task_to_the_one_that_created_it(
    session: AsyncSession,
) -> None:
    """The column the whole table exists for. Without it, "a review found a defect,
    which queued a patch, which exhausted its budget, which escalated" is four
    unrelated rows in four tables."""
    product_id = await _make_product(session)
    parent = await repository.enqueue_task(
        session,
        kind=TaskKind.REVIEW_LIVE_PRODUCT,
        engine=EngineName.DEVELOPMENT,
        idempotency_key="trail:parent",
        product_id=product_id,
    )
    child = await repository.enqueue_task(
        session,
        kind=TaskKind.PATCH_PRODUCT,
        engine=EngineName.DEVELOPMENT,
        idempotency_key="trail:child",
        product_id=product_id,
    )
    assert parent is not None and child is not None

    await repository.record_task_event(
        session,
        event="engine.start",
        task_id=child,
        product_id=product_id,
        engine=EngineName.DEVELOPMENT,
        caused_by_task_id=parent,
    )
    await session.commit()

    history = await repository.product_history(session, product_id)
    assert [e.caused_by_task_id for e in history] == [parent]


async def test_a_deferral_records_why_not_just_when(session: AsyncSession) -> None:
    """Before this, a campaign-approval wait, a marketing freeze, a
    validation-capacity hold, a blocked repair and a provider outage were
    indistinguishable in the database -- the difference lived only in a log line
    that the next deploy destroyed."""
    product_id = await _make_product(session)
    task_id = await repository.enqueue_task(
        session,
        kind=TaskKind.PATCH_PRODUCT,
        engine=EngineName.DEVELOPMENT,
        idempotency_key="trail:deferred",
        product_id=product_id,
    )
    assert task_id is not None

    await repository.defer_task(
        session,
        task_id,
        not_before=datetime.now(UTC) + timedelta(hours=1),
        reason="provider_outage:gemini",
    )
    await session.commit()

    history = await repository.product_history(session, product_id)
    deferrals = [e for e in history if e.event == "task.deferred"]
    assert len(deferrals) == 1
    assert deferrals[0].detail["reason"] == "provider_outage:gemini"


async def test_recording_an_event_never_breaks_the_work_it_describes(
    session: AsyncSession,
) -> None:
    """This runs on the hot path of every engine tick. Observability that can take
    down the thing it observes is worse than none, so a bad write is swallowed."""
    await repository.record_task_event(
        session,
        event="engine.start",
        task_id="does-not-exist",
        product_id="also-does-not-exist",
    )
    await session.commit()
