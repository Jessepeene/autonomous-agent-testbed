"""The dashboard's real read API -- new repository/observability functions
that back `GET /dashboard/state`, and `observability/dashboard.py::collect`
assembling them into one snapshot.

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

from factory.observability import costs
from factory.observability import dashboard as dashboard_metrics
from factory.preflight import PreflightReport
from factory.settings import EngineName, Settings
from factory.state import repository
from factory.state.orm import Base, LLMCall, TunableParameter
from factory.state.schemas import ProductStatus, ScoredIdea, TaskKind

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


async def _make_product(session: AsyncSession, name: str, *, niche: str | None = None) -> str:
    idea = ScoredIdea(
        problem_statement="Bookkeepers reconcile invoices by hand every single week.",
        target_niche=niche or name,
        source_url="https://example.test/thread",
        monetization_hypothesis="EUR 49/month, bought after the first month-end close.",
        pain_severity=0.8,
        willingness_to_pay=0.7,
        build_feasibility=0.6,
        adoption_ease=0.8,
    )
    idea_id = await repository.upsert_idea(session, idea)
    assert idea_id is not None
    return await repository.create_product(session, name=name, idea_id=idea_id)


# --- repository.recent_tasks / tasks_in_flight_by_engine --------------------------------


async def test_recent_tasks_orders_newest_first_and_respects_limit(session: AsyncSession) -> None:
    for i in range(3):
        await repository.enqueue_task(
            session,
            kind=TaskKind.RECONCILE_REVENUE,
            engine=EngineName.FINANCE,
            idempotency_key=f"recent-{i}",
        )
    await session.commit()

    found = await repository.recent_tasks(session, limit=2)

    assert len(found) == 2
    assert found[0].updated_at >= found[1].updated_at


async def test_tasks_in_flight_by_engine_counts_claimed_and_running(
    session: AsyncSession,
) -> None:
    await repository.enqueue_task(
        session,
        kind=TaskKind.RECONCILE_REVENUE,
        engine=EngineName.FINANCE,
        idempotency_key="inflight-1",
    )
    await session.commit()
    claimed = await repository.claim_next_task(session, worker_id="w", engines=[EngineName.FINANCE])
    assert claimed is not None
    await session.commit()

    counts = await repository.tasks_in_flight_by_engine(session)

    assert counts.get(EngineName.FINANCE.value) == 1


async def test_tasks_in_flight_by_engine_excludes_a_stale_lease(session: AsyncSession) -> None:
    """A crashed worker's abandoned claim must not read as "running" -- see
    `reclaim_expired_leases`, the write-side equivalent of this read-side check."""
    await repository.enqueue_task(
        session,
        kind=TaskKind.RECONCILE_REVENUE,
        engine=EngineName.FINANCE,
        idempotency_key="stale-inflight",
    )
    await session.commit()
    claimed = await repository.claim_next_task(
        session, worker_id="dead-worker", engines=[EngineName.FINANCE]
    )
    assert claimed is not None
    claimed.lease_expires_at = datetime.now(UTC) - timedelta(minutes=5)
    await session.commit()

    counts = await repository.tasks_in_flight_by_engine(session)

    assert counts.get(EngineName.FINANCE.value, 0) == 0


# --- repository.products_with_idea / mrr_by_product / latest_ledger_entry ---------------


async def test_products_with_idea_joins_niche_and_score(session: AsyncSession) -> None:
    product_id = await _make_product(session, "join-test-product", niche="join-test-niche")
    await session.commit()

    pairs = await repository.products_with_idea(session, limit=10)

    matches = [(p, i) for p, i in pairs if p.id == product_id]
    assert len(matches) == 1
    _product, idea = matches[0]
    assert idea.target_niche == "join-test-niche"
    assert isinstance(idea.score, float)


async def test_products_with_idea_includes_retired_products(session: AsyncSession) -> None:
    """Unlike `active_products`, the dashboard's portfolio view is a
    historical record, not a work queue -- RETIRED products must still show."""
    product_id = await _make_product(session, "retired-join-test")
    await repository.set_product_status(session, product_id, ProductStatus.RETIRED)
    await session.commit()

    pairs = await repository.products_with_idea(session, limit=50)

    assert product_id in {p.id for p, _ in pairs}


async def test_mrr_by_product_only_counts_active_subscriptions(session: AsyncSession) -> None:
    product_id = await _make_product(session, "mrr-test-product")
    customer_id = await repository.upsert_customer(session, stripe_customer_id="cus_mrr_test")
    await repository.upsert_subscription(
        session,
        stripe_subscription_id="sub_active",
        customer_id=customer_id,
        product_id=product_id,
        status="active",
        price_cents=4900,
        cancel_at_period_end=False,
        current_period_end=None,
    )
    await repository.upsert_subscription(
        session,
        stripe_subscription_id="sub_canceled",
        customer_id=customer_id,
        product_id=product_id,
        status="canceled",
        price_cents=9900,
        cancel_at_period_end=False,
        current_period_end=None,
    )
    await session.commit()

    mrr = await repository.mrr_by_product(session)

    assert mrr.get(product_id) == 4900


async def test_latest_ledger_entry_returns_the_most_recent_row(session: AsyncSession) -> None:
    assert await repository.latest_ledger_entry(session) is None

    await repository.append_ledger(
        session, external_event_id="evt_1", kind="revenue", amount_cents=1000
    )
    await repository.append_ledger(
        session, external_event_id="evt_2", kind="revenue", amount_cents=500
    )
    await session.commit()

    entry = await repository.latest_ledger_entry(session)

    assert entry is not None
    assert entry.external_event_id == "evt_2"
    assert entry.balance_cents == 1500


# --- learning.store.list_all -------------------------------------------------------------


async def test_list_all_returns_every_status_not_just_trial(session: AsyncSession) -> None:
    from factory.learning import store as learning_store

    session.add_all(
        [
            TunableParameter(
                kind="PROMPT",
                key="dashboard.test:CANDIDATE_KEY",
                value={"v": 1},
                version=1,
                status="candidate",
                target_metric="idea_survival_rate",
                rationale="test",
                created_by="test",
            ),
            TunableParameter(
                kind="PROMPT",
                key="dashboard.test:ACTIVE_KEY",
                value={"v": 1},
                version=1,
                status="active",
                target_metric="idea_survival_rate",
                rationale="test",
                created_by="test",
            ),
        ]
    )
    await session.commit()

    rows = await learning_store.list_all(session, limit=50)

    statuses = {row.status for row in rows if row.key.startswith("dashboard.test:")}
    assert statuses == {"candidate", "active"}


# --- observability.costs.provider_outcome_rates -------------------------------------------


async def test_provider_outcome_rates_counts_non_ok_outcomes_as_errors(
    session: AsyncSession,
) -> None:
    session.add_all(
        [
            LLMCall(
                provider="anthropic", model="claude-sonnet-5", task_type="analysis", outcome="ok"
            ),
            LLMCall(
                provider="anthropic", model="claude-sonnet-5", task_type="analysis", outcome="ok"
            ),
            LLMCall(
                provider="anthropic",
                model="claude-sonnet-5",
                task_type="analysis",
                outcome="unavailable",
            ),
        ]
    )
    await session.commit()

    rates = await costs.provider_outcome_rates(session, minutes=30)

    errors, total = rates["anthropic"]
    assert (errors, total) == (1, 3)


async def test_provider_outcome_rates_treats_schema_violation_repaired_as_success(
    session: AsyncSession,
) -> None:
    session.add(
        LLMCall(
            provider="gemini",
            model="gemini-2.5-pro",
            task_type="discovery",
            outcome="schema_violation_repaired",
        )
    )
    await session.commit()

    errors, total = (await costs.provider_outcome_rates(session, minutes=30))["gemini"]

    assert (errors, total) == (0, 1)


# --- observability.dashboard.collect: the full assembly -----------------------------------


async def test_collect_assembles_a_full_snapshot_without_error(session: AsyncSession) -> None:
    """Not exhaustive over every field -- proves the whole real assembly (every
    new repository/store/costs function, called together against real rows)
    actually runs end-to-end, which no single narrower test above does."""
    product_id = await _make_product(session, "collect-smoke-product")
    await repository.enqueue_task(
        session,
        kind=TaskKind.RECONCILE_REVENUE,
        engine=EngineName.FINANCE,
        idempotency_key="collect-smoke-task",
        product_id=product_id,
    )
    await repository.append_ledger(
        session, external_event_id="collect-evt", kind="revenue", amount_cents=2500
    )
    await session.commit()

    settings = Settings(database_url=DATABASE_URL, gemini_api_key="test-key")
    report = PreflightReport()  # empty: nothing disabled

    snapshot = await dashboard_metrics.collect(session, settings=settings, report=report)

    assert snapshot.balance_cents == 2500
    assert any(t.id for t in snapshot.tasks)
    assert any(p.id == product_id for p in snapshot.products)
    assert len(snapshot.engines) == len(EngineName)
    # Serializes cleanly -- this is exactly what the FastAPI route returns.
    as_dict = snapshot.as_dict()
    assert as_dict["balance_cents"] == 2500
