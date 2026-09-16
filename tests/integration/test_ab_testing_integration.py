"""Live AB testing: starting a test, generating both variants, tallying outcomes,
and concluding once one arm is significantly ahead (or the time budget runs out).

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

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from factory.engines.development import graph as dev_graph
from factory.hitl.telegram import NullNotifier
from factory.llm.client import LLMClient
from factory.llm.providers.base import Provider
from factory.llm.providers.fake import FakeProvider
from factory.llm.router import ProviderRegistry, provider_for
from factory.sandbox.disabled import DisabledSandbox
from factory.sandbox.runner import SandboxResult
from factory.settings import EngineName, Settings, TaskType
from factory.state import repository
from factory.state.orm import ABTest, Base, Task, new_id
from factory.state.schemas import ProductStatus, ScoredIdea, TaskKind, TaskStatus
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
    yield
    from factory.state import db as db_module

    await db_module.dispose_engine()


def _ctx(
    *, workspace_root: Path, builder: FakeProvider | None = None, **overrides: object
) -> FactoryContext:
    settings = Settings(
        database_url=DATABASE_URL,
        gemini_api_key="test-key",
        workspace_root=workspace_root,
        **overrides,
    )
    # Registered against whoever the router *currently* sends LANDING_PAGE to, rather
    # than a vendor named here. This test asserts on the builder's prompt, and it was
    # silently asserting on nobody's: it registered an Anthropic fake back when
    # LANDING_PAGE routed to Anthropic, the routing moved to DeepSeek, and the
    # assertion kept passing over a provider that was never called until the missing
    # registration finally surfaced it. Asking the router closes that loop -- a future
    # routing change moves the fake with it instead of quietly un-testing this.
    providers: dict[Provider, FakeProvider] = {
        Provider.GEMINI: FakeProvider(name=Provider.GEMINI),
        Provider.DEEPSEEK: FakeProvider(name=Provider.DEEPSEEK),
    }
    if builder is not None:
        providers[provider_for(TaskType.LANDING_PAGE, settings)] = builder
    llm = LLMClient(ProviderRegistry(providers), settings=settings)
    return FactoryContext(
        settings=settings,
        llm=llm,
        notifier=NullNotifier(),
        sandbox=DisabledSandbox(),
        enabled_engines={EngineName.DEVELOPMENT},
        worker_id="ab-test-integration",
    )


async def _make_product(session: AsyncSession, niche: str) -> tuple[str, str]:
    idea = ScoredIdea(
        problem_statement="Bookkeepers reconcile invoices by hand every week.",
        target_niche=niche,
        source_url="https://example.test/thread",
        monetization_hypothesis="Bookkeepers pay EUR 49/month per seat.",
        pain_severity=0.8,
        willingness_to_pay=0.7,
        build_feasibility=0.6,
        adoption_ease=0.8,
    )
    idea_id = await repository.upsert_idea(session, idea)
    assert idea_id is not None
    product_id = await repository.create_product(session, name=niche, idea_id=idea_id)
    await repository.set_product_status(session, product_id, ProductStatus.VALIDATING)
    return idea_id, product_id


# --- create_ab_test: at most one running per product ------------------------------


async def test_create_ab_test_returns_an_id(session: AsyncSession) -> None:
    _, product_id = await _make_product(session, "ab-create")

    ab_test_id = await repository.create_ab_test(
        session, product_id=product_id, variant_a_brief="a", variant_b_brief="b"
    )

    assert ab_test_id is not None
    ab_test = await session.get(ABTest, ab_test_id)
    assert ab_test is not None
    assert ab_test.status == "running"


async def test_a_second_running_test_for_the_same_product_is_refused(
    session: AsyncSession,
) -> None:
    """The partial unique index (migration 0013), not application logic, is what
    actually enforces this -- a race between two workers can never start two
    concurrent tests for the same product."""
    _, product_id = await _make_product(session, "ab-double")
    first = await repository.create_ab_test(
        session, product_id=product_id, variant_a_brief="a1", variant_b_brief="b1"
    )
    await session.flush()
    assert first is not None

    second = await repository.create_ab_test(
        session, product_id=product_id, variant_a_brief="a2", variant_b_brief="b2"
    )

    assert second is None


async def test_a_new_test_can_start_once_the_first_has_concluded(
    session: AsyncSession,
) -> None:
    _, product_id = await _make_product(session, "ab-sequential")
    first = await repository.create_ab_test(
        session, product_id=product_id, variant_a_brief="a1", variant_b_brief="b1"
    )
    assert first is not None
    await repository.conclude_ab_test(session, first, winner="a")
    await session.flush()

    second = await repository.create_ab_test(
        session, product_id=product_id, variant_a_brief="a2", variant_b_brief="b2"
    )

    assert second is not None
    assert second != first


# --- start_ab_test: creates the row and enqueues a regeneration -------------------


async def test_start_ab_test_enqueues_a_landing_page_regeneration(
    session: AsyncSession,
) -> None:
    idea_id, product_id = await _make_product(session, "ab-start")

    ab_test_id = await dev_graph.start_ab_test(
        session,
        product_id=product_id,
        idea_id=idea_id,
        variant_a_brief="Fast",
        variant_b_brief="Cheap",
    )

    assert ab_test_id is not None
    tasks = (
        (
            await session.execute(
                select(Task).where(
                    Task.product_id == product_id, Task.kind == TaskKind.BUILD_LANDING_PAGE.value
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(tasks) == 1
    assert tasks[0].payload["idea_id"] == idea_id


async def test_start_ab_test_returns_none_when_one_is_already_running(
    session: AsyncSession,
) -> None:
    idea_id, product_id = await _make_product(session, "ab-start-conflict")
    first = await dev_graph.start_ab_test(
        session, product_id=product_id, idea_id=idea_id, variant_a_brief="a", variant_b_brief="b"
    )
    assert first is not None

    second = await dev_graph.start_ab_test(
        session, product_id=product_id, idea_id=idea_id, variant_a_brief="c", variant_b_brief="d"
    )

    assert second is None


# --- tally_ab_test_outcomes --------------------------------------------------------


async def test_tally_splits_views_and_signals_by_variant(session: AsyncSession) -> None:
    _, product_id = await _make_product(session, "ab-tally")
    since = datetime.now(UTC) - timedelta(hours=1)

    await repository.record_page_view(session, product_id=product_id, path="/", variant="a")
    await repository.record_page_view(session, product_id=product_id, path="/", variant="a")
    await repository.record_page_view(session, product_id=product_id, path="/", variant="b")
    await repository.insert_validation_signal(
        session,
        product_id=product_id,
        email="a1@example.test",
        variant="a",
        require_confirmation=False,
    )
    await repository.insert_validation_signal(
        session,
        product_id=product_id,
        email="b1@example.test",
        variant="b",
        require_confirmation=False,
    )

    outcomes = await repository.tally_ab_test_outcomes(session, product_id, since=since)

    assert outcomes["a"] == (2, 1)
    assert outcomes["b"] == (1, 1)


# --- _build_landing_page picks up a running test -----------------------------------


class _AlwaysFailingSandbox:
    """Reports itself available (so `run()`'s gate lets the task through as far as
    `_build_landing_page`) but fails the very first thing `_verify_and_ship` does
    with it -- dependency install. That routes to `_heal`'s `dependency_failure`
    category, which returns a plain string rather than raising, so this never
    needs to exercise Bug Council or deploy just to observe the prompt
    `generate_landing_page` was called with (which happens earlier, during
    generation, well before any sandbox interaction)."""

    available = True

    async def execute(
        self,
        command: str,
        *,
        workspace: str,
        timeout_seconds: int | None = None,
        network: bool = False,
        env: dict[str, str] | None = None,
    ) -> SandboxResult:
        return SandboxResult(exit_code=1, stdout="", stderr="no network in this fake")


async def test_build_landing_page_generates_both_variants_when_a_test_is_running(
    session: AsyncSession, tmp_path: Path
) -> None:
    from factory.state.schemas import GeneratedFile, ScaffoldPlan

    idea_id, product_id = await _make_product(session, "ab-build")
    await repository.create_ab_test(
        session, product_id=product_id, variant_a_brief="Fast", variant_b_brief="Cheap"
    )
    await session.flush()

    provider = FakeProvider(name=provider_for(TaskType.LANDING_PAGE))
    provider.register(
        ScaffoldPlan,
        ScaffoldPlan(
            product_name="Widget",
            summary="s" * 25,
            files=[GeneratedFile(path="app/index.html", contents="<html></html>")],
            test_command="pytest",
        ),
    )
    ctx = _ctx(workspace_root=tmp_path, builder=provider)
    ctx.sandbox = _AlwaysFailingSandbox()  # type: ignore[assignment]

    task = Task(
        id=new_id(),
        kind=TaskKind.BUILD_LANDING_PAGE.value,
        engine=EngineName.DEVELOPMENT.value,
        status=TaskStatus.CLAIMED.value,
        priority=90,
        payload={"idea_id": idea_id, "product_id": product_id},
        idea_id=idea_id,
        product_id=product_id,
        attempt_count=1,
    )
    result = await dev_graph.run(ctx, session, task)

    assert "needs repair" in result
    prompt = provider.calls[-1]["prompt"]
    assert "Fast" in prompt
    assert "Cheap" in prompt
    assert "live AB test" in prompt


# --- EVALUATE_AB_TEST: the conclusion decision -------------------------------------


def _eval_task(ab_test_id: str, product_id: str) -> Task:
    return Task(
        id=new_id(),
        kind=TaskKind.EVALUATE_AB_TEST.value,
        engine=EngineName.DEVELOPMENT.value,
        status=TaskStatus.CLAIMED.value,
        priority=110,
        payload={"ab_test_id": ab_test_id},
        product_id=product_id,
        attempt_count=1,
    )


async def _seed_outcomes(
    session: AsyncSession, product_id: str, *, a: tuple[int, int], b: tuple[int, int]
) -> None:
    a_views, a_signals = a
    b_views, b_signals = b
    for _ in range(a_views):
        await repository.record_page_view(session, product_id=product_id, path="/", variant="a")
    for i in range(a_signals):
        await repository.insert_validation_signal(
            session,
            product_id=product_id,
            email=f"a{i}@example.test",
            variant="a",
            require_confirmation=False,
        )
    for _ in range(b_views):
        await repository.record_page_view(session, product_id=product_id, path="/", variant="b")
    for i in range(b_signals):
        await repository.insert_validation_signal(
            session,
            product_id=product_id,
            email=f"b{i}@example.test",
            variant="b",
            require_confirmation=False,
        )


async def test_an_inconclusive_test_with_low_volume_stays_running(
    session: AsyncSession, tmp_path: Path
) -> None:
    _, product_id = await _make_product(session, "ab-eval-running")
    ab_test_id = await repository.create_ab_test(
        session, product_id=product_id, variant_a_brief="a", variant_b_brief="b"
    )
    assert ab_test_id is not None
    await _seed_outcomes(session, product_id, a=(5, 1), b=(5, 1))
    ctx = _ctx(workspace_root=tmp_path, ab_test_min_views_per_arm=30)

    result = await dev_graph.run(ctx, session, _eval_task(ab_test_id, product_id))

    assert "still running" in result
    ab_test = await session.get(ABTest, ab_test_id)
    assert ab_test is not None
    assert ab_test.status == "running"


async def test_a_significant_result_concludes_the_test_with_a_winner(
    session: AsyncSession, tmp_path: Path
) -> None:
    _, product_id = await _make_product(session, "ab-eval-winner")
    ab_test_id = await repository.create_ab_test(
        session, product_id=product_id, variant_a_brief="a", variant_b_brief="b"
    )
    assert ab_test_id is not None
    await _seed_outcomes(session, product_id, a=(200, 8), b=(200, 80))
    ctx = _ctx(workspace_root=tmp_path, ab_test_min_views_per_arm=30)

    result = await dev_graph.run(ctx, session, _eval_task(ab_test_id, product_id))

    assert "concluded" in result
    assert "winner=b" in result
    ab_test = await session.get(ABTest, ab_test_id)
    assert ab_test is not None
    assert ab_test.status == "concluded"
    assert ab_test.winner == "b"
    assert ab_test.concluded_at is not None


async def test_a_stalled_test_past_its_time_budget_concludes_inconclusive(
    session: AsyncSession, tmp_path: Path
) -> None:
    _, product_id = await _make_product(session, "ab-eval-expired")
    ab_test_id = await repository.create_ab_test(
        session, product_id=product_id, variant_a_brief="a", variant_b_brief="b"
    )
    assert ab_test_id is not None
    await session.execute(
        update(ABTest)
        .where(ABTest.id == ab_test_id)
        .values(started_at=datetime.now(UTC) - timedelta(days=30))
    )
    await _seed_outcomes(session, product_id, a=(5, 1), b=(5, 1))  # never reaches the min
    ctx = _ctx(workspace_root=tmp_path, ab_test_min_views_per_arm=30, ab_test_max_days=14)

    result = await dev_graph.run(ctx, session, _eval_task(ab_test_id, product_id))

    assert "concluded" in result
    ab_test = await session.get(ABTest, ab_test_id)
    assert ab_test is not None
    assert ab_test.status == "concluded"
    assert ab_test.winner == "inconclusive"


async def test_evaluating_an_already_concluded_test_is_a_no_op(
    session: AsyncSession, tmp_path: Path
) -> None:
    _, product_id = await _make_product(session, "ab-eval-noop")
    ab_test_id = await repository.create_ab_test(
        session, product_id=product_id, variant_a_brief="a", variant_b_brief="b"
    )
    assert ab_test_id is not None
    await repository.conclude_ab_test(session, ab_test_id, winner="a")
    ctx = _ctx(workspace_root=tmp_path)

    result = await dev_graph.run(ctx, session, _eval_task(ab_test_id, product_id))

    assert "not running" in result


# --- /interest endpoint accepts and stores variant ---------------------------------


async def _client() -> httpx.AsyncClient:
    from factory.api.app import app

    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def test_interest_endpoint_stores_the_reported_variant(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DATABASE_URL", DATABASE_URL or "")
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    _, product_id = await _make_product(session, "ab-interest")
    await session.commit()

    async with await _client() as client:
        response = await client.post(
            f"/interest/{product_id}",
            json={"email": "lead@example.test", "variant": "b"},
        )

    assert response.status_code == 200
    assert await repository.tally_validation_signals(session, product_id, variant="b") == 1
    assert await repository.tally_validation_signals(session, product_id, variant="a") == 0
