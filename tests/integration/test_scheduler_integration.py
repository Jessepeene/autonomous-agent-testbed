"""The periodic cadences — the fix for two silent-stall bugs.

Before ``_schedule_periodic`` existed, Finance only ran when a Stripe webhook
arrived, so a factory with a misconfigured webhook (or simply no sales yet) never
reconciled — the ledger and the virtual card limit could drift apart forever with no
error anywhere. Marketing had the matching gap: outreach only fired right after a
deploy, so a live product stopped acquiring customers the moment its first batch of
leads was sent.

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
from sqlalchemy import select
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
from factory.state.orm import Base, Task
from factory.state.schemas import ProductStatus, ScoredIdea, TaskKind, TaskStatus
from factory.supervisor.context import FactoryContext
from factory.supervisor.scheduler import Scheduler

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
    """`Scheduler.housekeeping()` opens sessions through the process-wide cached
    engine (`session_scope` -> `get_engine`), a different connection than this
    file's `session` fixture. Reset it around each test so no pooled connection
    leaks into a later test file."""
    yield
    await db_module.dispose_engine()


def _ctx(*, enabled: set[EngineName], workspace_root: Path) -> FactoryContext:
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
        enabled_engines=enabled,
        worker_id="cadence-test",
    )


async def _make_live_product(session: AsyncSession, name: str) -> str:
    idea = ScoredIdea(
        problem_statement="Bookkeepers reconcile invoices by hand every single week.",
        target_niche=name,
        source_url="https://example.test/thread",
        monetization_hypothesis="EUR 49/month, bought after the first month-end close.",
        pain_severity=0.8,
        willingness_to_pay=0.7,
        build_feasibility=0.6,
        adoption_ease=0.8,
    )
    idea_id = await repository.upsert_idea(session, idea)
    assert idea_id is not None
    product_id = await repository.create_product(session, name=name, idea_id=idea_id)
    await repository.set_product_status(session, product_id, ProductStatus.LIVE)
    return product_id


async def test_finance_reconciliation_is_scheduled_without_any_webhook(
    session: AsyncSession, tmp_path: Path
) -> None:
    """Reproduces the original bug: no Stripe event has ever arrived, yet a
    reconciliation task must still appear so the ledger and the card limit can never
    silently drift apart."""
    ctx = _ctx(enabled={EngineName.FINANCE}, workspace_root=tmp_path)
    scheduler = Scheduler(ctx, graph=None)

    await scheduler._schedule_periodic(session)
    await session.commit()

    pending = await repository.count_pending_tasks(session, EngineName.FINANCE)
    assert pending == 1


async def test_finance_reconciliation_is_not_scheduled_when_finance_is_disabled(
    session: AsyncSession, tmp_path: Path
) -> None:
    ctx = _ctx(enabled=set(), workspace_root=tmp_path)
    scheduler = Scheduler(ctx, graph=None)

    await scheduler._schedule_periodic(session)
    await session.commit()

    assert await repository.count_pending_tasks(session, EngineName.FINANCE) == 0


async def test_reconciliation_is_not_duplicated_within_the_same_hour(
    session: AsyncSession, tmp_path: Path
) -> None:
    ctx = _ctx(enabled={EngineName.FINANCE}, workspace_root=tmp_path)
    scheduler = Scheduler(ctx, graph=None)

    await scheduler._schedule_periodic(session)
    await scheduler._schedule_periodic(session)
    await session.commit()

    assert await repository.count_pending_tasks(session, EngineName.FINANCE) == 1


async def test_live_products_get_lead_harvesting_without_a_fresh_deploy(
    session: AsyncSession, tmp_path: Path
) -> None:
    """Reproduces the second bug: outreach previously only fired right after a
    deploy, so a product that has been live for weeks would never get fresh leads."""
    product_id = await _make_live_product(session, "Reconciliation Assistant")
    await session.commit()

    ctx = _ctx(enabled={EngineName.MARKETING}, workspace_root=tmp_path)
    scheduler = Scheduler(ctx, graph=None)

    await scheduler._schedule_periodic(session)
    await session.commit()

    task = await repository.claim_next_task(session, worker_id="w", engines=[EngineName.MARKETING])
    assert task is not None
    assert task.kind == TaskKind.HARVEST_LEADS.value
    assert task.product_id == product_id


async def test_lead_harvesting_is_not_scheduled_when_marketing_is_disabled(
    session: AsyncSession, tmp_path: Path
) -> None:
    await _make_live_product(session, "Reconciliation Assistant")
    await session.commit()

    ctx = _ctx(enabled=set(), workspace_root=tmp_path)
    scheduler = Scheduler(ctx, graph=None)

    await scheduler._schedule_periodic(session)
    await session.commit()

    assert await repository.count_pending_tasks(session, EngineName.MARKETING) == 0


async def test_every_live_product_gets_its_own_harvest_task(
    session: AsyncSession, tmp_path: Path
) -> None:
    await _make_live_product(session, "Product One")
    await _make_live_product(session, "Product Two")
    await session.commit()

    ctx = _ctx(enabled={EngineName.MARKETING}, workspace_root=tmp_path)
    scheduler = Scheduler(ctx, graph=None)

    await scheduler._schedule_periodic(session)
    await session.commit()

    assert await repository.count_pending_tasks(session, EngineName.MARKETING) == 2


async def test_every_live_product_gets_a_daily_browser_review(
    session: AsyncSession, tmp_path: Path
) -> None:
    """Deliberately on this unconditional sweep rather than `backlog.py`'s
    CATALOGUE: that one is only enqueued from `graph.py::_backlog`, reached solely
    on IDLE_BACKLOG/HOLD_DISCOVERY, so a *busy* factory would never review its
    live products at all -- exactly when a shipped defect is most likely to be
    missed."""
    product_id = await _make_live_product(session, "Reconciliation Assistant")
    await session.commit()

    ctx = _ctx(enabled={EngineName.DEVELOPMENT}, workspace_root=tmp_path)
    scheduler = Scheduler(ctx, graph=None)

    await scheduler._schedule_periodic(session)
    await session.commit()

    reviews = (
        (
            await session.execute(
                select(Task).where(Task.kind == TaskKind.REVIEW_LIVE_PRODUCT.value)
            )
        )
        .scalars()
        .all()
    )
    assert len(reviews) == 1
    assert reviews[0].product_id == product_id


async def test_the_live_review_is_not_duplicated_within_the_same_day(
    session: AsyncSession, tmp_path: Path
) -> None:
    await _make_live_product(session, "Reconciliation Assistant")
    await session.commit()

    ctx = _ctx(enabled={EngineName.DEVELOPMENT}, workspace_root=tmp_path)
    scheduler = Scheduler(ctx, graph=None)

    await scheduler._schedule_periodic(session)
    await scheduler._schedule_periodic(session)
    await session.commit()

    reviews = (
        (
            await session.execute(
                select(Task).where(Task.kind == TaskKind.REVIEW_LIVE_PRODUCT.value)
            )
        )
        .scalars()
        .all()
    )
    assert len(reviews) == 1


async def test_a_deferred_task_does_not_count_as_pending_work(
    session: AsyncSession, tmp_path: Path
) -> None:
    """`count_pending_tasks` backs the Orchestrator's snapshot, and a task deferred
    into the future cannot be claimed -- counting it makes the queue look like it
    never drains. Observed live: 45 "Factory is stuck" escalations in three hours,
    every one counting 16 tasks deliberately deferred for 30 days. The provider
    circuit breaker sharpens this: pausing a provider defers its tasks, which under
    the old count looked exactly like a stall."""
    task_id = await repository.enqueue_task(
        session,
        kind=TaskKind.RECONCILE_REVENUE,
        engine=EngineName.FINANCE,
        idempotency_key="deferred-not-pending",
        payload={},
    )
    assert task_id is not None
    assert await repository.count_pending_tasks(session, EngineName.FINANCE) == 1

    await repository.defer_task(
        session, task_id, not_before=datetime.now(UTC) + timedelta(days=30)
    )
    await session.commit()

    assert await repository.count_pending_tasks(session, EngineName.FINANCE) == 0
    # And it is genuinely unclaimable, which is the property the count must match.
    assert (
        await repository.claim_next_task(session, worker_id="w", engines=[EngineName.FINANCE])
    ) is None


async def test_housekeeping_runs_all_cadences_together(
    session: AsyncSession, tmp_path: Path
) -> None:
    """`housekeeping()` is what the real tick loop calls — the discovery throttle
    and the new periodic cadences must not step on each other."""
    await _make_live_product(session, "Product One")
    await session.commit()

    ctx = _ctx(
        enabled={EngineName.FINANCE, EngineName.MARKETING, EngineName.DISCOVERY},
        workspace_root=tmp_path,
    )
    scheduler = Scheduler(ctx, graph=None)

    await scheduler.housekeeping()

    assert await repository.count_pending_tasks(session, EngineName.FINANCE) == 1
    assert await repository.count_pending_tasks(session, EngineName.MARKETING) == 1
    assert await repository.count_pending_tasks(session, EngineName.DISCOVERY) == 1


async def test_reclaimed_leases_do_not_block_new_periodic_tasks(
    session: AsyncSession, tmp_path: Path
) -> None:
    """Housekeeping reclaims expired leases before scheduling — a crashed worker
    must not leave the finance queue looking falsely non-empty forever."""
    await repository.enqueue_task(
        session,
        kind=TaskKind.RECONCILE_REVENUE,
        engine=EngineName.FINANCE,
        idempotency_key="stale-reconcile",
    )
    stuck = await repository.claim_next_task(
        session, worker_id="dead-worker", engines=[EngineName.FINANCE]
    )
    assert stuck is not None
    stuck_id = stuck.id
    stuck.lease_expires_at = datetime.now(UTC) - timedelta(minutes=5)
    await session.commit()

    ctx = _ctx(enabled={EngineName.FINANCE}, workspace_root=tmp_path)
    scheduler = Scheduler(ctx, graph=None)
    await scheduler.housekeeping()

    from factory.state.orm import Task

    # `housekeeping()` mutates this row through its own internal `session_scope()`
    # — a separate connection from this test's `session` fixture. Without expiring
    # first, `session.get()` returns the stale object already in this session's
    # identity map (loaded back when `claim_next_task` first returned `stuck`)
    # instead of re-querying the database.
    session.expire_all()
    reclaimed = await session.get(Task, stuck_id)
    assert reclaimed is not None
    assert reclaimed.status == TaskStatus.PENDING.value
