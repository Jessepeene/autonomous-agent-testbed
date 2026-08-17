"""A validating product's lead sourcing found nothing, and had no automatic
path forward -- `HARVEST_LEADS` is a one-shot enqueue for a validating
product (`development/graph.py`), so nothing ever asked again on its own and
nothing ever told a human. Observed live: a real product sat at zero leads
indefinitely after a lead-provider credential was rejected, with no retry and
no escalation.

`scheduler.py`'s stalled-validation sweep closes this: try one automatic
re-harvest, then -- only if that also found nothing -- try a different
distribution channel via CHANNEL_SCOUT_REVIEW, and only escalate to a human
once *that* has also found nothing, the same "someone tries to fix it, then
asks" shape as every other autonomous-repair path in this factory (heal
cycles, DevOps-error classification) -- now with an extra rung, since a
different channel is a better answer to "distribution blocked" than an
automatic retry of the same one.

Needs Postgres:

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

from factory.hitl.interrupts import collect_replies
from factory.hitl.telegram import NullNotifier
from factory.llm.client import LLMClient
from factory.llm.providers.base import Provider
from factory.llm.providers.fake import FakeProvider
from factory.llm.router import ProviderRegistry
from factory.sandbox.disabled import DisabledSandbox
from factory.settings import EngineName, Settings
from factory.state import db as db_module
from factory.state import repository
from factory.state.orm import Base, Product, Task
from factory.state.schemas import InterruptReason, ProductStatus, ScoredIdea, TaskKind, TaskStatus
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
    yield
    await db_module.dispose_engine()


class _ScriptedNotifier:
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


def _ctx(*, enabled: set[EngineName], workspace_root: Path) -> FactoryContext:
    settings = Settings(
        database_url=DATABASE_URL, gemini_api_key="test-key", workspace_root=workspace_root
    )
    llm = LLMClient(ProviderRegistry({Provider.GEMINI: FakeProvider()}), settings=settings)
    return FactoryContext(
        settings=settings,
        llm=llm,
        notifier=NullNotifier(),
        sandbox=DisabledSandbox(),
        enabled_engines=enabled,
        worker_id="stalled-validation-test",
    )


async def _make_validating_product(session: AsyncSession, name: str, *, age_hours: int = 48) -> str:
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
    await repository.set_product_status(session, product_id, ProductStatus.VALIDATING)
    await session.execute(
        update(Product)
        .where(Product.id == product_id)
        .values(created_at=datetime.now(UTC) - timedelta(hours=age_hours))
    )
    return product_id


async def _initial_harvest_task(session: AsyncSession, product_id: str) -> str:
    """The one-shot enqueue `_build_landing_page` makes for real -- present for
    every validating product this module's fixtures create, exactly like a real
    one, so `has_retried_lead_sourcing`'s ">= 2" count means what it claims to."""
    task_id = await repository.enqueue_task(
        session,
        kind=TaskKind.HARVEST_LEADS,
        engine=EngineName.MARKETING,
        idempotency_key=f"leads:validate:{product_id}",
        payload={"product_id": product_id},
        product_id=product_id,
        priority=120,
    )
    assert task_id is not None
    await repository.complete_task(session, task_id)
    return task_id


# --- repository.stalled_validations ---------------------------------------------------


async def test_finds_an_old_validating_product_with_zero_leads(session: AsyncSession) -> None:
    product_id = await _make_validating_product(session, "stalled-eligible", age_hours=48)
    await _initial_harvest_task(session, product_id)
    await session.flush()

    found = await repository.stalled_validations(session, older_than=timedelta(hours=24))

    assert [p.id for p in found] == [product_id]


async def test_excludes_a_product_too_young_to_be_stalled_yet(session: AsyncSession) -> None:
    product_id = await _make_validating_product(session, "stalled-too-young", age_hours=1)
    await _initial_harvest_task(session, product_id)
    await session.flush()

    found = await repository.stalled_validations(session, older_than=timedelta(hours=24))

    assert product_id not in {p.id for p in found}


async def test_excludes_a_product_that_already_has_leads(session: AsyncSession) -> None:
    product_id = await _make_validating_product(session, "stalled-has-leads", age_hours=48)
    await _initial_harvest_task(session, product_id)
    await repository.insert_lead(
        session,
        {
            "first_name": "A",
            "last_name": "B",
            "job_title": "Bookkeeper",
            "company": "Acme",
            "work_email": "a@example.test",
            "source_url": "https://example.test/lead",
            "scraped_at": datetime.now(UTC),
        },
        lia_passed=True,
        provider="apollo",
        product_id=product_id,
    )
    await session.flush()

    found = await repository.stalled_validations(session, older_than=timedelta(hours=24))

    assert product_id not in {p.id for p in found}


async def test_excludes_a_product_with_an_open_interrupt_already(session: AsyncSession) -> None:
    product_id = await _make_validating_product(session, "stalled-already-escalated", age_hours=48)
    await _initial_harvest_task(session, product_id)
    interrupt_id = await repository.open_interrupt(
        session,
        reason=InterruptReason.LEAD_SOURCING_STALLED,
        subject="already asked",
        detail="...",
        headline="Test escalation",
        reply_hint="Reply anything to retry sourcing leads, or REJECT to retire this product.",
        product_id=product_id,
    )
    assert interrupt_id is not None
    await session.flush()

    found = await repository.stalled_validations(session, older_than=timedelta(hours=24))

    assert product_id not in {p.id for p in found}


async def test_excludes_a_live_product(session: AsyncSession) -> None:
    product_id = await _make_validating_product(session, "stalled-goes-live", age_hours=48)
    await repository.set_product_status(session, product_id, ProductStatus.LIVE)
    await session.flush()

    found = await repository.stalled_validations(session, older_than=timedelta(hours=24))

    assert product_id not in {p.id for p in found}


# --- repository.has_retried_lead_sourcing ----------------------------------------------


async def test_has_not_retried_with_only_the_original_harvest(session: AsyncSession) -> None:
    product_id = await _make_validating_product(session, "retry-check-one")
    await _initial_harvest_task(session, product_id)
    await session.flush()

    assert await repository.has_retried_lead_sourcing(session, product_id) is False


async def test_has_retried_once_a_second_harvest_task_exists(session: AsyncSession) -> None:
    product_id = await _make_validating_product(session, "retry-check-two")
    await _initial_harvest_task(session, product_id)
    await repository.enqueue_task(
        session,
        kind=TaskKind.HARVEST_LEADS,
        engine=EngineName.MARKETING,
        idempotency_key=f"leads:validate:{product_id}:retry",
        payload={"product_id": product_id},
        product_id=product_id,
        priority=120,
    )
    await session.flush()

    assert await repository.has_retried_lead_sourcing(session, product_id) is True


# --- repository.has_attempted_channel_scout ---------------------------------------------


async def test_has_not_attempted_channel_scout_with_no_task(session: AsyncSession) -> None:
    product_id = await _make_validating_product(session, "scout-check-one")
    await session.flush()

    assert await repository.has_attempted_channel_scout(session, product_id) is False


async def test_has_attempted_channel_scout_once_a_task_exists(session: AsyncSession) -> None:
    product_id = await _make_validating_product(session, "scout-check-two")
    await repository.enqueue_task(
        session,
        kind=TaskKind.CHANNEL_SCOUT_REVIEW,
        engine=EngineName.MARKETING,
        idempotency_key=f"channel-scout:stall:{product_id}",
        payload={"product_id": product_id},
        product_id=product_id,
        priority=100,
    )
    await session.flush()

    assert await repository.has_attempted_channel_scout(session, product_id) is True


# --- scheduler: retry, then try a channel, then escalate --------------------------------


async def test_first_encounter_enqueues_a_retry_not_an_escalation(
    session: AsyncSession, tmp_path: Path
) -> None:
    product_id = await _make_validating_product(session, "sweep-first-strike", age_hours=48)
    await _initial_harvest_task(session, product_id)
    await session.commit()

    ctx = _ctx(enabled={EngineName.MARKETING}, workspace_root=tmp_path)
    scheduler = Scheduler(ctx, graph=None)
    await scheduler._schedule_periodic(session)
    await session.commit()

    task = await repository.claim_next_task(session, worker_id="w", engines=[EngineName.MARKETING])
    assert task is not None
    assert task.kind == TaskKind.HARVEST_LEADS.value
    assert task.idempotency_key == f"leads:validate:{product_id}:retry"
    assert await repository.count_open_interrupts(session) == 0


async def test_second_encounter_tries_a_channel_scout_before_escalating(
    session: AsyncSession, tmp_path: Path
) -> None:
    product_id = await _make_validating_product(session, "sweep-second-strike", age_hours=48)
    await _initial_harvest_task(session, product_id)
    retry_task_id = await repository.enqueue_task(
        session,
        kind=TaskKind.HARVEST_LEADS,
        engine=EngineName.MARKETING,
        idempotency_key=f"leads:validate:{product_id}:retry",
        payload={"product_id": product_id},
        product_id=product_id,
        priority=120,
    )
    assert retry_task_id is not None
    await repository.complete_task(session, retry_task_id)
    await session.commit()

    ctx = _ctx(enabled={EngineName.MARKETING}, workspace_root=tmp_path)
    scheduler = Scheduler(ctx, graph=None)
    await scheduler._schedule_periodic(session)
    await session.commit()

    task = await repository.claim_next_task(session, worker_id="w", engines=[EngineName.MARKETING])
    assert task is not None
    assert task.kind == TaskKind.CHANNEL_SCOUT_REVIEW.value
    assert task.idempotency_key == f"channel-scout:stall:{product_id}"
    assert await repository.count_open_interrupts(session) == 0


async def test_third_encounter_escalates_after_a_channel_scout_also_found_nothing(
    session: AsyncSession, tmp_path: Path
) -> None:
    product_id = await _make_validating_product(session, "sweep-third-strike", age_hours=48)
    await _initial_harvest_task(session, product_id)
    retry_task_id = await repository.enqueue_task(
        session,
        kind=TaskKind.HARVEST_LEADS,
        engine=EngineName.MARKETING,
        idempotency_key=f"leads:validate:{product_id}:retry",
        payload={"product_id": product_id},
        product_id=product_id,
        priority=120,
    )
    assert retry_task_id is not None
    await repository.complete_task(session, retry_task_id)
    scout_task_id = await repository.enqueue_task(
        session,
        kind=TaskKind.CHANNEL_SCOUT_REVIEW,
        engine=EngineName.MARKETING,
        idempotency_key=f"channel-scout:stall:{product_id}",
        payload={"product_id": product_id},
        product_id=product_id,
        priority=100,
    )
    assert scout_task_id is not None
    await repository.complete_task(session, scout_task_id)
    await session.commit()

    ctx = _ctx(enabled={EngineName.MARKETING}, workspace_root=tmp_path)
    scheduler = Scheduler(ctx, graph=None)
    await scheduler._schedule_periodic(session)
    await session.commit()

    assert await repository.count_pending_tasks(session, EngineName.MARKETING) == 0
    interrupts = await repository.list_open_interrupts(session)
    assert len(interrupts) == 1
    assert interrupts[0].reason == InterruptReason.LEAD_SOURCING_STALLED.value
    assert interrupts[0].product_id == product_id
    assert "sweep-third-strike" in interrupts[0].subject


async def test_a_product_already_escalated_is_left_alone(
    session: AsyncSession, tmp_path: Path
) -> None:
    """The sweep must not pile a second automatic action onto a product a human
    is already being asked about."""
    product_id = await _make_validating_product(session, "sweep-already-open", age_hours=48)
    await _initial_harvest_task(session, product_id)
    await repository.open_interrupt(
        session,
        reason=InterruptReason.LEAD_SOURCING_STALLED,
        subject="already asked",
        detail="...",
        headline="Test escalation",
        reply_hint="Reply anything to retry sourcing leads, or REJECT to retire this product.",
        product_id=product_id,
    )
    await session.commit()

    ctx = _ctx(enabled={EngineName.MARKETING}, workspace_root=tmp_path)
    scheduler = Scheduler(ctx, graph=None)
    await scheduler._schedule_periodic(session)
    await session.commit()

    assert await repository.count_pending_tasks(session, EngineName.MARKETING) == 0
    assert await repository.count_open_interrupts(session) == 1  # unchanged, not doubled


# --- collect_replies: REJECT retires, anything else retries ---------------------------


async def test_reject_reply_retires_the_stalled_product(session: AsyncSession) -> None:
    product_id = await _make_validating_product(session, "reply-reject-product")
    interrupt_id = await repository.open_interrupt(
        session,
        reason=InterruptReason.LEAD_SOURCING_STALLED,
        subject="needs a decision",
        detail="...",
        headline="Test escalation",
        reply_hint="Reply anything to retry sourcing leads, or REJECT to retire this product.",
        product_id=product_id,
    )
    assert interrupt_id is not None
    await session.flush()

    notifier = _ScriptedNotifier([f"#{interrupt_id[:8]} REJECT"])
    resolved = await collect_replies(session, notifier)

    assert resolved == 1
    product = await session.get(Product, product_id)
    assert product is not None
    assert product.status == ProductStatus.RETIRED.value


async def test_any_other_reply_queues_a_fresh_harvest_attempt(session: AsyncSession) -> None:
    product_id = await _make_validating_product(session, "reply-retry-product")
    interrupt_id = await repository.open_interrupt(
        session,
        reason=InterruptReason.LEAD_SOURCING_STALLED,
        subject="needs a decision",
        detail="...",
        headline="Test escalation",
        reply_hint="Reply anything to retry sourcing leads, or REJECT to retire this product.",
        product_id=product_id,
    )
    assert interrupt_id is not None
    await session.flush()

    notifier = _ScriptedNotifier([f"#{interrupt_id[:8]} fixed the Apollo key, please retry"])
    resolved = await collect_replies(session, notifier)

    assert resolved == 1
    task = await repository.claim_next_task(session, worker_id="w", engines=[EngineName.MARKETING])
    assert task is not None
    assert task.kind == TaskKind.HARVEST_LEADS.value
    assert task.product_id == product_id
    assert task.idempotency_key.startswith(f"leads:validate:{product_id}:human-retry:")

    product = await session.get(Product, product_id)
    assert product is not None
    assert product.status == ProductStatus.VALIDATING.value  # not retired


async def test_resolving_does_not_disturb_an_unrelated_task(session: AsyncSession) -> None:
    """Sanity check on the new branch's placement in collect_replies: an
    unrelated task must not be touched by resolving a LEAD_SOURCING_STALLED
    interrupt."""
    other_task_id = await repository.enqueue_task(
        session,
        kind=TaskKind.RECONCILE_REVENUE,
        engine=EngineName.FINANCE,
        idempotency_key="unrelated-task",
    )
    assert other_task_id is not None
    product_id = await _make_validating_product(session, "reply-side-effect-check")
    interrupt_id = await repository.open_interrupt(
        session,
        reason=InterruptReason.LEAD_SOURCING_STALLED,
        subject="needs a decision",
        detail="...",
        headline="Test escalation",
        reply_hint="Reply anything to retry sourcing leads, or REJECT to retire this product.",
        product_id=product_id,
    )
    assert interrupt_id is not None
    await session.flush()

    notifier = _ScriptedNotifier([f"#{interrupt_id[:8]} try again"])
    await collect_replies(session, notifier)

    other_task = await session.get(Task, other_task_id)
    assert other_task is not None
    assert other_task.status == TaskStatus.PENDING.value
