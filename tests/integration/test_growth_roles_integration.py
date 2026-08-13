"""Growth roles: GROWTH_ANALYST, SEO_OPTIMIZER, CHANNEL_SCOUT end to end.

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
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from factory.engines.development import graph as dev_graph
from factory.engines.marketing import channel
from factory.engines.marketing import graph as mkt_graph
from factory.hitl.telegram import NullNotifier
from factory.llm.client import LLMClient
from factory.llm.providers.base import AgentTurn, Provider, ToolCall, Usage
from factory.llm.providers.fake import FakeProvider
from factory.llm.router import ProviderRegistry
from factory.sandbox.disabled import DisabledSandbox
from factory.settings import EngineName, Settings
from factory.state import repository
from factory.state.orm import ABTest, Base, ChannelExperiment, Interrupt, Product, Task, new_id
from factory.state.schemas import (
    InterruptReason,
    ProductStatus,
    ScoredIdea,
    TaskKind,
    TaskStatus,
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


@pytest_asyncio.fixture(autouse=True)
async def _reset_global_engine() -> AsyncIterator[None]:
    yield
    from factory.state import db as db_module

    await db_module.dispose_engine()


def _ctx(*, workspace_root: Path, gemini: FakeProvider, **overrides: object) -> FactoryContext:
    settings = Settings(
        database_url=DATABASE_URL,
        gemini_api_key="test-key",
        workspace_root=workspace_root,
        factory_dry_run=False,
        **overrides,
    )
    llm = LLMClient(
        ProviderRegistry({Provider.GEMINI: gemini}), settings=settings, backoff_base=0.0
    )
    return FactoryContext(
        settings=settings,
        llm=llm,
        notifier=NullNotifier(),
        sandbox=DisabledSandbox(),
        enabled_engines={EngineName.DEVELOPMENT, EngineName.MARKETING},
        worker_id="growth-roles-test",
    )


async def _make_live_product(session: AsyncSession, niche: str) -> str:
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
    await repository.set_product_status(session, product_id, ProductStatus.LIVE)
    return product_id


async def _backdate(session: AsyncSession, product_id: str, *, column: str, days: int) -> None:
    """Staleness is measured from `created_at` for a never-reviewed product (see
    `products_needing_growth_review`'s docstring) -- a product created moments ago
    in a test is not "overdue" by construction, so exercising that path needs an
    explicit backdate rather than relying on real wall-clock time to pass."""
    await session.execute(
        update(Product)
        .where(Product.id == product_id)
        .values(**{column: datetime.now(UTC) - timedelta(days=days)})
    )


def _completion(text: str) -> AgentTurn:
    return AgentTurn(
        text=text,
        tool_calls=[],
        stop_reason="end_turn",
        provider=Provider.GEMINI,
        model="gemini-2.5-flash",
        usage=Usage(input_tokens=10, output_tokens=10),
    )


def _tool_call_turn(name: str, arguments: dict[str, object]) -> AgentTurn:
    return AgentTurn(
        text="",
        tool_calls=[ToolCall(id="call-1", name=name, arguments=arguments)],
        stop_reason="tool_use",
        provider=Provider.GEMINI,
        model="gemini-2.5-flash",
        usage=Usage(input_tokens=10, output_tokens=10),
    )


# --- repository: staleness-ordered product listing ---------------------------------


async def test_products_needing_growth_review_puts_never_reviewed_first(
    session: AsyncSession,
) -> None:
    """Both products are old enough to be overdue either way -- `reviewed` was
    last looked at 10 days ago (still stale against a 7-day interval), `never`
    has no review at all so its effective staleness is its (backdated)
    `created_at`, 30 days ago -- more overdue, so it must sort first."""
    reviewed = await _make_live_product(session, "growth-reviewed")
    never = await _make_live_product(session, "growth-never-reviewed")
    await _backdate(session, reviewed, column="created_at", days=30)
    await _backdate(session, reviewed, column="last_growth_review_at", days=10)
    await _backdate(session, never, column="created_at", days=30)
    await session.flush()

    products = await repository.products_needing_growth_review(
        session, older_than=timedelta(days=7), limit=10
    )

    ids = [p.id for p in products]
    assert ids.index(never) < ids.index(reviewed)


async def test_products_needing_growth_review_excludes_recently_reviewed(
    session: AsyncSession,
) -> None:
    product_id = await _make_live_product(session, "growth-fresh")
    await repository.set_growth_reviewed(session, product_id)
    await session.flush()

    products = await repository.products_needing_growth_review(
        session, older_than=timedelta(days=7), limit=10
    )

    assert product_id not in {p.id for p in products}


async def test_products_needing_growth_review_excludes_non_live_products(
    session: AsyncSession,
) -> None:
    idea = ScoredIdea(
        problem_statement="Bookkeepers reconcile invoices by hand every week.",
        target_niche="growth-not-live",
        source_url="https://example.test/thread",
        monetization_hypothesis="Bookkeepers pay EUR 49/month per seat.",
        pain_severity=0.8,
        willingness_to_pay=0.7,
        build_feasibility=0.6,
        adoption_ease=0.8,
    )
    idea_id = await repository.upsert_idea(session, idea)
    assert idea_id is not None
    product_id = await repository.create_product(session, name="Not Live", idea_id=idea_id)
    # Left at PLANNED -- never LIVE.

    products = await repository.products_needing_growth_review(
        session, older_than=timedelta(days=7), limit=10
    )

    assert product_id not in {p.id for p in products}


# --- repository: channel_experiments CRUD and sync_decisions -----------------------


async def test_channel_experiment_lifecycle_from_proposal_to_decision(
    session: AsyncSession,
) -> None:
    product_id = await _make_live_product(session, "channel-lifecycle")

    experiment_id = await repository.create_channel_experiment(
        session,
        product_id=product_id,
        channel="reddit",
        url="https://reddit.com/r/bookkeeping",
        rationale="Active community discussing manual reconciliation pain.",
        draft_content="Here's a free tool that might help with reconciliation...",
    )
    interrupt_id = await repository.open_interrupt(
        session,
        reason=InterruptReason.CHANNEL_APPROVAL,
        subject=f"Channel proposal ({experiment_id[:8]}): reddit",
        detail="...",
        headline="Test escalation",
        reply_hint="Reply APPROVE to authorise, or REJECT to decline.",
    )
    assert interrupt_id is not None
    await repository.set_channel_experiment_interrupt(session, experiment_id, interrupt_id)
    await session.flush()

    pending = await repository.pending_channel_experiments(session)
    assert {e.id for e in pending} == {experiment_id}

    await repository.resolve_interrupt(session, interrupt_id, "APPROVE, looks great")
    await session.flush()

    synced = await channel.sync_decisions(session)
    assert synced == 1

    experiment = await session.get(ChannelExperiment, experiment_id)
    assert experiment is not None
    assert experiment.status == "approved"
    assert experiment.decided_at is not None

    # Resolved and decided -- no longer pending.
    assert await repository.pending_channel_experiments(session) == []


async def test_channel_experiment_rejection_is_fail_closed(session: AsyncSession) -> None:
    product_id = await _make_live_product(session, "channel-reject")
    experiment_id = await repository.create_channel_experiment(
        session,
        product_id=product_id,
        channel="forum",
        url="https://example-forum.test/thread/1",
        rationale="A niche forum thread about the exact problem.",
        draft_content="Sharing a tool I built for this...",
    )
    interrupt_id = await repository.open_interrupt(
        session,
        reason=InterruptReason.CHANNEL_APPROVAL,
        subject=f"Channel proposal ({experiment_id[:8]}): forum",
        detail="...",
        headline="Test escalation",
        reply_hint="Reply APPROVE to authorise, or REJECT to decline.",
    )
    assert interrupt_id is not None
    await repository.set_channel_experiment_interrupt(session, experiment_id, interrupt_id)
    await repository.resolve_interrupt(session, interrupt_id, "not sure, let's skip this one")
    await session.flush()

    await channel.sync_decisions(session)

    experiment = await session.get(ChannelExperiment, experiment_id)
    assert experiment is not None
    assert experiment.status == "rejected"


# --- tools/growth.py: called directly -----------------------------------------------


async def test_read_product_growth_reports_views_and_signals(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    from factory.settings import get_settings
    from factory.tools.growth import read_product_growth

    monkeypatch.setenv("DATABASE_URL", DATABASE_URL or "")
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    get_settings.cache_clear()

    product_id = await _make_live_product(session, "tool-read-growth")
    await repository.record_page_view(session, product_id=product_id, path="/")
    await repository.record_page_view(session, product_id=product_id, path="/")
    await repository.insert_validation_signal(
        session, product_id=product_id, email="a@example.test"
    )
    await session.commit()

    result = await read_product_growth(product_id)

    assert result["views_last_7_days"] == 2
    assert result["validation_signals_total"] == 1
    assert result["running_ab_test"] is None

    get_settings.cache_clear()


async def test_start_ab_test_action_starts_and_then_refuses_a_second(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    from factory.settings import get_settings
    from factory.tools.growth import start_ab_test_action

    monkeypatch.setenv("DATABASE_URL", DATABASE_URL or "")
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    get_settings.cache_clear()

    product_id = await _make_live_product(session, "tool-start-ab")
    await session.commit()

    first = await start_ab_test_action(product_id, "Emphasize speed", "Emphasize price")
    assert first["started"] is True

    second = await start_ab_test_action(product_id, "Another angle", "Yet another angle")
    assert second["started"] is False

    get_settings.cache_clear()


async def test_request_seo_pass_action_refuses_a_non_live_product(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    from factory.settings import get_settings
    from factory.tools.growth import request_seo_pass_action

    monkeypatch.setenv("DATABASE_URL", DATABASE_URL or "")
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    get_settings.cache_clear()

    idea = ScoredIdea(
        problem_statement="Bookkeepers reconcile invoices by hand every week.",
        target_niche="tool-seo-not-live",
        source_url="https://example.test/thread",
        monetization_hypothesis="Bookkeepers pay EUR 49/month per seat.",
        pain_severity=0.8,
        willingness_to_pay=0.7,
        build_feasibility=0.6,
        adoption_ease=0.8,
    )
    idea_id = await repository.upsert_idea(session, idea)
    assert idea_id is not None
    product_id = await repository.create_product(session, name="Not Live", idea_id=idea_id)
    await session.commit()

    result = await request_seo_pass_action(product_id, "The pricing page has almost no copy.")

    assert "error" in result

    get_settings.cache_clear()


async def test_propose_channel_creates_a_pending_experiment_and_interrupt(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    from factory.settings import get_settings
    from factory.tools.growth import propose_channel

    monkeypatch.setenv("DATABASE_URL", DATABASE_URL or "")
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    get_settings.cache_clear()

    product_id = await _make_live_product(session, "tool-propose-channel")
    await session.commit()

    result = await propose_channel(
        product_id,
        "reddit",
        "https://reddit.com/r/bookkeeping",
        "Active thread asking for exactly this kind of tool.",
        "Sharing something I built that automates reconciliation...",
    )

    assert result["proposed"] is True
    assert result["awaiting_human_approval"] is True

    experiment = await session.get(ChannelExperiment, result["experiment_id"])
    assert experiment is not None
    assert experiment.status == "proposed"
    assert experiment.interrupt_id is not None

    interrupt = await session.get(Interrupt, experiment.interrupt_id)
    assert interrupt is not None
    assert interrupt.reason == InterruptReason.CHANNEL_APPROVAL.value

    get_settings.cache_clear()


# --- development/graph.py::_growth_review -------------------------------------------


def _growth_review_task(product_id: str) -> Task:
    return Task(
        id=new_id(),
        kind=TaskKind.GROWTH_REVIEW.value,
        engine=EngineName.DEVELOPMENT.value,
        status=TaskStatus.CLAIMED.value,
        priority=65,
        payload={"product_id": product_id},
        product_id=product_id,
        attempt_count=1,
    )


async def test_growth_review_spawns_growth_analyst_and_marks_reviewed(
    session: AsyncSession, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from factory.settings import get_settings
    from factory.tools import growth  # noqa: F401 - import populates REGISTRY

    # The scripted tool call below (start_ab_test_action) opens its own session
    # through the process-wide cached engine, not this file's `session` fixture --
    # same reasoning as test_run_in_sandbox_lock.py.
    monkeypatch.setenv("DATABASE_URL", DATABASE_URL or "")
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    get_settings.cache_clear()

    product_id = await _make_live_product(session, "growth-review-e2e")
    await session.commit()

    provider = FakeProvider(name=Provider.GEMINI)
    provider.register_turns(
        _tool_call_turn(
            "start_ab_test_action",
            {
                "product_id": product_id,
                "variant_a_brief": "Emphasize how fast reconciliation becomes.",
                "variant_b_brief": "Emphasize the money saved on bookkeeping hours.",
            },
        ),
        _completion("Traffic was steady but conversion was low, so I started an AB test."),
    )
    ctx = _ctx(workspace_root=tmp_path, gemini=provider)

    result = await dev_graph.run(ctx, session, _growth_review_task(product_id))

    assert "growth_review" in result
    product = await session.get(Product, product_id)
    assert product is not None
    assert product.last_growth_review_at is not None

    ab_tests = (
        (await session.execute(select(ABTest).where(ABTest.product_id == product_id)))
        .scalars()
        .all()
    )
    assert len(ab_tests) == 1

    get_settings.cache_clear()


# --- marketing/graph.py::_channel_scout_review --------------------------------------


def _channel_scout_task(product_id: str) -> Task:
    return Task(
        id=new_id(),
        kind=TaskKind.CHANNEL_SCOUT_REVIEW.value,
        engine=EngineName.MARKETING.value,
        status=TaskStatus.CLAIMED.value,
        priority=70,
        payload={"product_id": product_id},
        product_id=product_id,
        attempt_count=1,
    )


async def test_channel_scout_review_spawns_scout_and_marks_scouted(
    session: AsyncSession, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from factory.settings import get_settings
    from factory.tools import growth  # noqa: F401 - import populates REGISTRY

    monkeypatch.setenv("DATABASE_URL", DATABASE_URL or "")
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    get_settings.cache_clear()

    product_id = await _make_live_product(session, "channel-scout-e2e")
    await session.commit()

    provider = FakeProvider(name=Provider.GEMINI)
    provider.register_turns(
        _tool_call_turn(
            "propose_channel",
            {
                "product_id": product_id,
                "channel": "reddit",
                "url": "https://reddit.com/r/bookkeeping",
                "rationale": "A recent thread asking for exactly this.",
                "draft_content": "Sharing a tool that automates reconciliation...",
            },
        ),
        _completion("Found a strong candidate and proposed it."),
    )
    ctx = _ctx(workspace_root=tmp_path, gemini=provider)

    result = await mkt_graph.run(ctx, session, _channel_scout_task(product_id))

    assert "channel_scout" in result
    product = await session.get(Product, product_id)
    assert product is not None
    assert product.last_channel_scout_at is not None

    experiments = await repository.recent_channel_experiments(session, product_id)
    assert len(experiments) == 1
    assert experiments[0].channel == "reddit"

    get_settings.cache_clear()


# --- marketing/graph.py::_measure_channel_experiment --------------------------------


def _measure_task(experiment_id: str, product_id: str) -> Task:
    return Task(
        id=new_id(),
        kind=TaskKind.MEASURE_CHANNEL_EXPERIMENT.value,
        engine=EngineName.MARKETING.value,
        status=TaskStatus.CLAIMED.value,
        priority=130,
        payload={"experiment_id": experiment_id},
        product_id=product_id,
        attempt_count=1,
    )


async def test_measure_channel_experiment_tallies_matching_referrers(
    session: AsyncSession, tmp_path: Path
) -> None:
    product_id = await _make_live_product(session, "measure-channel-e2e")
    experiment_id = await repository.create_channel_experiment(
        session,
        product_id=product_id,
        channel="reddit",
        url="https://www.reddit.com/r/bookkeeping/comments/abc123",
        rationale="...",
        draft_content="...",
    )
    await repository.decide_channel_experiment(session, experiment_id, "approved")
    # Backdate decided_at so the referrer window covers page views recorded now.
    await session.execute(
        update(ChannelExperiment)
        .where(ChannelExperiment.id == experiment_id)
        .values(decided_at=datetime.now(UTC) - timedelta(hours=1))
    )
    await repository.record_page_view(
        session, product_id=product_id, path="/", referrer="https://www.reddit.com/r/bookkeeping/"
    )
    await repository.record_page_view(
        session, product_id=product_id, path="/", referrer="https://www.reddit.com/r/bookkeeping/"
    )
    await repository.record_page_view(
        session, product_id=product_id, path="/", referrer="https://unrelated.example/"
    )
    await session.flush()

    provider = FakeProvider(name=Provider.GEMINI)
    ctx = _ctx(workspace_root=tmp_path, gemini=provider)

    result = await mkt_graph.run(ctx, session, _measure_task(experiment_id, product_id))

    assert "concluded" in result
    experiment = await session.get(ChannelExperiment, experiment_id)
    assert experiment is not None
    assert experiment.status == "concluded"
    assert experiment.outcome is not None
    assert "2 visit" in experiment.outcome


# --- development/graph.py::_patch with category="growth_seo" -----------------------


def _growth_seo_patch_task(product_id: str, idea_id: str) -> Task:
    return Task(
        id=new_id(),
        kind=TaskKind.PATCH_PRODUCT.value,
        engine=EngineName.DEVELOPMENT.value,
        status=TaskStatus.CLAIMED.value,
        priority=60,
        payload={
            "product_id": product_id,
            "idea_id": idea_id,
            "healing": "The pricing page has almost no body copy.",
            "category": "growth_seo",
        },
        product_id=product_id,
        attempt_count=1,
    )


class _AvailableButNoDocker:
    """Reports itself available (so `run()`'s top-level sandbox gate lets a
    PATCH_PRODUCT task through) without ever actually being called -- this test
    only needs `_should_repair_surgically`'s separate `ctx.docker_available` check
    to fail, which happens before any sandbox execution."""

    available = True

    async def execute(self, command: str, *, workspace: str, **kwargs: object) -> object:
        raise AssertionError("must not be called -- the growth_seo pass is skipped first")


async def test_growth_seo_patch_skips_without_regenerating_when_docker_unavailable(
    session: AsyncSession, tmp_path: Path
) -> None:
    """No Docker means no surgical repair is possible -- unlike every other patch
    category, growth_seo must never fall through to a full regeneration over a
    discretionary optimisation. The product must come out exactly as it went in."""
    from factory.state.orm import Idea

    product_id = await _make_live_product(session, "growth-seo-skip")
    product = await session.get(Product, product_id)
    assert product is not None
    idea = await session.get(Idea, product.idea_id)
    assert idea is not None
    await repository.set_product_status(
        session, product_id, ProductStatus.LIVE, test_command="pytest", workspace_path="ws-skip"
    )
    await session.flush()

    provider = FakeProvider(name=Provider.GEMINI)
    ctx = _ctx(workspace_root=tmp_path, gemini=provider)
    ctx.sandbox = _AvailableButNoDocker()  # type: ignore[assignment]
    assert ctx.docker_available is False

    result = await dev_graph.run(ctx, session, _growth_seo_patch_task(product_id, product.idea_id))

    assert "skipped" in result
    refreshed = await session.get(Product, product_id)
    assert refreshed is not None
    assert refreshed.status == ProductStatus.LIVE.value
