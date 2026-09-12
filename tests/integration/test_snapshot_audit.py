"""Snapshot-metric integrity auditing (`supervisor/audit.py`).

Regression-shaped coverage for the *class* of bug `approved_ideas_waiting` had:
not a wrong value, but a silently-trusted assumption (every approved idea has a
product) that nothing checked. This is the deterministic, no-LLM-call check that
verifies the assumption independent of whether the metric's own query would
ever notice it broke.

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
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from factory.engines.maintenance import run as run_maintenance
from factory.hitl.telegram import NullNotifier
from factory.llm.client import LLMClient
from factory.llm.providers.base import Provider
from factory.llm.providers.fake import FakeProvider
from factory.llm.router import ProviderRegistry
from factory.sandbox.disabled import DisabledSandbox
from factory.settings import EngineName, Settings
from factory.state import repository
from factory.state.orm import Base, HealAttempt, Interrupt, LLMCall, Task, new_id
from factory.state.schemas import InterruptReason, ScoredIdea, TaskKind, TaskStatus
from factory.supervisor.audit import (
    check_approved_idea_orphans,
    check_business_health,
    check_scoring_contract_drift,
)
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


def _idea(niche: str) -> ScoredIdea:
    return ScoredIdea(
        problem_statement="Bookkeepers reconcile invoices by hand every week.",
        target_niche=niche,
        source_url="https://example.test/thread",
        monetization_hypothesis="Bookkeepers pay EUR 49/month per seat.",
        pain_severity=0.8,
        willingness_to_pay=0.7,
        build_feasibility=0.6,
        adoption_ease=0.8,
    )


async def _approved_idea_with_product(session: AsyncSession, niche: str) -> str:
    idea_id = await repository.upsert_idea(session, _idea(niche))
    assert idea_id is not None
    await repository.set_idea_status(session, idea_id, "approved")
    await repository.create_product(session, name=niche, idea_id=idea_id)
    return idea_id


async def _approved_idea_without_product(session: AsyncSession, niche: str) -> str:
    idea_id = await repository.upsert_idea(session, _idea(niche))
    assert idea_id is not None
    await repository.set_idea_status(session, idea_id, "approved")
    return idea_id


# --- repository.orphaned_approved_ideas --------------------------------------------


async def test_no_orphans_when_every_approved_idea_has_a_product(
    session: AsyncSession,
) -> None:
    await _approved_idea_with_product(session, "audit-has-product")
    await session.flush()

    assert await repository.orphaned_approved_ideas(session) == []


async def test_finds_an_approved_idea_with_no_product(session: AsyncSession) -> None:
    orphan_id = await _approved_idea_without_product(session, "audit-orphan")
    await session.flush()

    orphans = await repository.orphaned_approved_ideas(session)

    assert [i.id for i in orphans] == [orphan_id]


async def test_ignores_a_pending_idea_with_no_product(session: AsyncSession) -> None:
    """A freshly-discovered idea has no product yet by design -- only 'approved'
    with no product is the violated guarantee."""
    idea_id = await repository.upsert_idea(session, _idea("audit-pending"))
    assert idea_id is not None
    await session.flush()

    assert await repository.orphaned_approved_ideas(session) == []


# --- supervisor/audit.py::check_approved_idea_orphans -------------------------------


async def test_check_returns_zero_and_raises_nothing_when_healthy(
    session: AsyncSession,
) -> None:
    await _approved_idea_with_product(session, "audit-healthy")
    await session.flush()

    count = await check_approved_idea_orphans(session)

    assert count == 0
    assert (await session.execute(select(Interrupt))).scalars().all() == []


async def test_check_raises_a_data_integrity_interrupt_for_an_orphan(
    session: AsyncSession,
) -> None:
    orphan_id = await _approved_idea_without_product(session, "audit-flagged")
    await session.flush()

    count = await check_approved_idea_orphans(session)

    assert count == 1
    interrupts = (await session.execute(select(Interrupt))).scalars().all()
    assert len(interrupts) == 1
    assert interrupts[0].reason == InterruptReason.DATA_INTEGRITY.value
    assert orphan_id in interrupts[0].detail


async def test_check_does_not_duplicate_an_already_open_interrupt(
    session: AsyncSession,
) -> None:
    await _approved_idea_without_product(session, "audit-repeat")
    await session.flush()

    first = await check_approved_idea_orphans(session)
    second = await check_approved_idea_orphans(session)

    assert first == 1
    assert second == 1  # still reports the live count...
    interrupts = (await session.execute(select(Interrupt))).scalars().all()
    assert len(interrupts) == 1  # ...but only one open interrupt exists


# --- supervisor/audit.py::check_scoring_contract_drift (Phase 4) -----------------

_WEIGHTS_KEY = "factory.engines.discovery.score:SCORING_WEIGHTS"


async def test_scoring_check_is_healthy_when_never_bootstrapped(session: AsyncSession) -> None:
    """No SCORING_WEIGHTS row at all yet -- nothing has drifted from anything."""
    assert await check_scoring_contract_drift(session) is False
    assert (await session.execute(select(Interrupt))).scalars().all() == []


async def test_scoring_check_is_healthy_when_dimensions_match(session: AsyncSession) -> None:
    from factory.learning import store as learning_store

    await learning_store.bootstrap_active(
        session,
        kind="SCORING_WEIGHTS",
        key=_WEIGHTS_KEY,
        value=ScoredIdea.default_weights(),
        created_by="bootstrap",
        target_metric="idea_survival_rate",
    )
    await session.flush()

    assert await check_scoring_contract_drift(session) is False
    assert (await session.execute(select(Interrupt))).scalars().all() == []


async def test_scoring_check_flags_a_dimension_mismatch(session: AsyncSession) -> None:
    """The exact scenario this check exists for: a row that predates the
    defensibility dimension, still being served."""
    from factory.learning import store as learning_store

    old_default = {
        "pain_severity": 0.3,
        "willingness_to_pay": 0.3,
        "adoption_ease": 0.25,
        "build_feasibility": 0.15,
    }
    await learning_store.bootstrap_active(
        session,
        kind="SCORING_WEIGHTS",
        key=_WEIGHTS_KEY,
        value=old_default,
        created_by="bootstrap",
        target_metric="idea_survival_rate",
    )
    await session.flush()

    drifted = await check_scoring_contract_drift(session)

    assert drifted is True
    interrupts = (await session.execute(select(Interrupt))).scalars().all()
    assert len(interrupts) == 1
    assert interrupts[0].reason == InterruptReason.DATA_INTEGRITY.value
    assert "defensibility" in interrupts[0].detail


async def test_scoring_check_does_not_duplicate_an_already_open_interrupt(
    session: AsyncSession,
) -> None:
    from factory.learning import store as learning_store

    await learning_store.bootstrap_active(
        session,
        kind="SCORING_WEIGHTS",
        key=_WEIGHTS_KEY,
        value={"pain_severity": 1.0},
        created_by="bootstrap",
        target_metric="idea_survival_rate",
    )
    await session.flush()

    first = await check_scoring_contract_drift(session)
    second = await check_scoring_contract_drift(session)

    assert first is True
    assert second is True
    interrupts = (await session.execute(select(Interrupt))).scalars().all()
    assert len(interrupts) == 1


# --- engines/maintenance.py dispatch -------------------------------------------------


def _ctx(*, workspace_root: Path) -> FactoryContext:
    settings = Settings(
        database_url=DATABASE_URL, gemini_api_key="test-key", workspace_root=workspace_root
    )
    # Deliberately no queued turn on the fake provider: if this slug ever spawned
    # an agent or made an LLM call, FakeProvider raises rather than silently
    # succeeding -- this check must stay fully deterministic and free.
    llm = LLMClient(ProviderRegistry({Provider.GEMINI: FakeProvider()}), settings=settings)
    return FactoryContext(
        settings=settings,
        llm=llm,
        notifier=NullNotifier(),
        sandbox=DisabledSandbox(),
        enabled_engines={EngineName.SUPERVISOR},
        worker_id="audit-maintenance-test",
    )


def _maintenance_task() -> Task:
    return Task(
        id=new_id(),
        kind=TaskKind.MAINTENANCE.value,
        engine=EngineName.SUPERVISOR.value,
        status=TaskStatus.CLAIMED.value,
        priority=500,
        payload={"slug": "audit-snapshot-integrity"},
        attempt_count=1,
    )


async def test_maintenance_run_dispatches_the_slug_without_any_llm_call(
    session: AsyncSession, tmp_path: Path
) -> None:
    await _approved_idea_without_product(session, "audit-via-maintenance")
    await session.flush()

    summary = await run_maintenance(_ctx(workspace_root=tmp_path), session, _maintenance_task())

    assert "1 orphaned approved idea" in summary
    assert "scoring contract drift: False" in summary  # no SCORING_WEIGHTS row seeded here
    assert "business-health concern(s)" in summary


# --- supervisor/audit.py::check_business_health (Tier 0 finding #0g) ---------------


async def test_a_freshly_active_factory_with_no_history_has_no_concerns(
    session: AsyncSession,
) -> None:
    """No products at all yet, no heal attempts, no spend history -- nothing
    to be worried about, and nothing here is an LLM judgement call."""
    concerns = await check_business_health(session)

    assert concerns == []
    assert (await session.execute(select(Interrupt))).scalars().all() == []


async def test_a_stuck_factory_is_flagged(session: AsyncSession) -> None:
    product_id = await _approved_idea_with_product(session, "audit-stuck")
    await session.flush()
    # Backdate the product's own last-activity timestamp directly -- onupdate
    # only fires on a real UPDATE, so this simulates "nothing has touched
    # this row in days" without needing to wait days in a test.
    from factory.state.orm import Product

    stale_product = (
        await session.execute(select(Product).where(Product.idea_id == product_id))
    ).scalar_one()
    stale_product.updated_at = datetime.now(UTC) - timedelta(days=5)
    await session.flush()

    concerns = await check_business_health(session)

    assert any("has changed status" in c for c in concerns)
    interrupts = (await session.execute(select(Interrupt))).scalars().all()
    assert len(interrupts) == 1
    assert interrupts[0].reason == InterruptReason.DATA_INTEGRITY.value


async def _bare_product(session: AsyncSession, niche: str) -> str:
    """A product with no approval-orphan baggage -- just something with a real
    id a `HealAttempt` can point at."""
    idea_id = await repository.upsert_idea(session, _idea(niche))
    assert idea_id is not None
    return await repository.create_product(session, name=niche, idea_id=idea_id)


async def test_several_products_healing_at_once_is_flagged(session: AsyncSession) -> None:
    now = datetime.now(UTC)
    for i in range(3):
        product_id = await _bare_product(session, f"audit-heal-spike-{i}")
        session.add(
            HealAttempt(
                product_id=product_id,
                attempt_number=1,
                healing_text="something broke",
                created_at=now,
            )
        )
    await session.flush()

    concerns = await check_business_health(session)

    assert any("different products hit a heal cycle" in c for c in concerns)


async def test_a_single_products_repeated_heals_is_not_flagged_as_a_spike(
    session: AsyncSession,
) -> None:
    """One product legitimately cycling through several heal attempts is
    routine -- only several *different* products at once is the pattern
    worth naming."""
    product_id = await _bare_product(session, "audit-single-heal")
    now = datetime.now(UTC)
    for attempt in range(1, 4):
        session.add(
            HealAttempt(
                product_id=product_id,
                attempt_number=attempt,
                healing_text="something broke",
                created_at=now,
            )
        )
    await session.flush()

    concerns = await check_business_health(session)

    assert not any("heal cycle" in c for c in concerns)


async def test_an_llm_spend_spike_relative_to_the_trailing_week_is_flagged(
    session: AsyncSession,
) -> None:
    now = datetime.now(UTC)
    # A quiet trailing week: $1/day for 7 days.
    for day in range(1, 8):
        session.add(
            LLMCall(
                provider="anthropic",
                model="claude-sonnet-5",
                task_type="analysis",
                cost_usd=1.0,
                created_at=now - timedelta(days=day, hours=1),
            )
        )
    # Today: a huge spike relative to that baseline.
    session.add(
        LLMCall(
            provider="anthropic",
            model="claude-sonnet-5",
            task_type="analysis",
            cost_usd=100.0,
            created_at=now,
        )
    )
    await session.flush()

    concerns = await check_business_health(session)

    assert any("LLM spend" in c for c in concerns)


async def test_multiple_concerns_on_the_same_day_raise_exactly_one_interrupt(
    session: AsyncSession,
) -> None:
    product_id = await _approved_idea_with_product(session, "audit-multi-concern")
    from factory.state.orm import Product

    stale_product = (
        await session.execute(select(Product).where(Product.idea_id == product_id))
    ).scalar_one()
    stale_product.updated_at = datetime.now(UTC) - timedelta(days=10)
    await session.flush()

    concerns = await check_business_health(session)

    assert len(concerns) >= 1
    interrupts = (await session.execute(select(Interrupt))).scalars().all()
    assert len(interrupts) == 1
