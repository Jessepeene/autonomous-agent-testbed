"""Heal-cycle failures feed the memory system, and _patch() recalls them.

Before this, a product could fail the exact same heal category over and over --
sandbox timeout, dependency failure, whatever -- with each cycle starting from a
blank slate: `_patch()`'s prompt carried only the current cycle's feedback, never
what earlier cycles already tried and found not to work. `_heal()` now writes a
FAILURE memory on every cycle (product-scoped) plus one more (engine-scoped) if the
whole heal budget is exhausted, and `_patch()` recalls the product-scoped ones
before generating its next attempt.

Needs Postgres:

    docker compose up -d postgres
    alembic upgrade head
    pytest tests/integration -q
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from factory.engines.development import graph as dev_graph
from factory.hitl.telegram import NullNotifier
from factory.llm.client import LLMClient
from factory.llm.providers.base import Provider
from factory.llm.providers.fake import FakeProvider
from factory.llm.router import ProviderRegistry
from factory.memory.store import scopes_for
from factory.sandbox.disabled import DisabledSandbox
from factory.settings import EngineName, Settings
from factory.state import repository
from factory.state.orm import Base, Memory, Product, Task
from factory.state.schemas import ScoredIdea
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
    from factory.settings import get_settings
    from factory.state import db as db_module

    await db_module.dispose_engine()
    get_settings.cache_clear()


def _ctx(*, tmp_path: Path, blacklist_path: Path) -> FactoryContext:
    settings = Settings(
        database_url=DATABASE_URL,
        gemini_api_key="test-key",
        workspace_root=tmp_path,
        blacklist_path=blacklist_path,
        factory_dry_run=True,
    )
    registry = ProviderRegistry({Provider.GEMINI: FakeProvider(name=Provider.GEMINI)})
    llm = LLMClient(registry, settings=settings)
    return FactoryContext(
        settings=settings,
        llm=llm,
        notifier=NullNotifier(),
        sandbox=DisabledSandbox(),
        enabled_engines={EngineName.DEVELOPMENT},
        worker_id="heal-memory-test",
    )


async def _make_product(session: AsyncSession, name: str) -> tuple[str, str]:
    idea = ScoredIdea(
        problem_statement="Bookkeepers reconcile invoices by hand every week.",
        target_niche=name,
        source_url="https://example.test/thread",
        monetization_hypothesis="Bookkeepers pay EUR 49/month per seat.",
        pain_severity=0.8,
        willingness_to_pay=0.7,
        build_feasibility=0.6,
        adoption_ease=0.8,
    )
    idea_id = await repository.upsert_idea(session, idea)
    assert idea_id is not None
    product_id = await repository.create_product(session, name=name, idea_id=idea_id)
    return product_id, idea_id


async def test_a_heal_cycle_writes_a_product_scoped_failure_memory(
    session: AsyncSession, tmp_path: Path, blacklist_file: Path
) -> None:
    ctx = _ctx(tmp_path=tmp_path, blacklist_path=blacklist_file)
    product_id, idea_id = await _make_product(session, "heal-memory-1")

    await dev_graph._heal(
        ctx,
        session,
        product_id,
        "sandbox pip install timed out after 300s",
        idea_id,
        category="dependency_timeout",
    )

    rows = (
        (await session.execute(select(Memory).where(Memory.scope == f"product:{product_id}")))
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert "dependency_timeout" in rows[0].content
    assert rows[0].kind == "failure"


async def test_recurring_same_category_reinforces_rather_than_piling_up(
    session: AsyncSession, tmp_path: Path, blacklist_file: Path
) -> None:
    ctx = _ctx(tmp_path=tmp_path, blacklist_path=blacklist_file)
    product_id, idea_id = await _make_product(session, "heal-memory-2")

    await dev_graph._heal(
        ctx,
        session,
        product_id,
        "sandbox pip install timed out",
        idea_id,
        category="dependency_timeout",
    )
    await dev_graph._heal(
        ctx,
        session,
        product_id,
        "sandbox pip install timed out",
        idea_id,
        category="dependency_timeout",
    )

    # expire_all: remember()'s reinforcement path updates the row via a raw UPDATE,
    # which does not refresh this session's identity map on its own (the fixture
    # sets expire_on_commit=False) -- without this the re-SELECT below would just
    # hand back the first, now-stale in-memory copy instead of the reinforced row.
    session.expire_all()
    rows = (
        (await session.execute(select(Memory).where(Memory.scope == f"product:{product_id}")))
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].hits == 1  # default 0 at creation, +1 for the one reinforcement


async def test_exhausting_the_heal_budget_also_writes_an_engine_scoped_memory(
    session: AsyncSession, tmp_path: Path, blacklist_file: Path
) -> None:
    ctx = _ctx(tmp_path=tmp_path, blacklist_path=blacklist_file)
    product_id, idea_id = await _make_product(session, "heal-memory-3")

    for _ in range(dev_graph.MAX_HEAL_CYCLES):
        await dev_graph._heal(
            ctx,
            session,
            product_id,
            "same devops-adjacent failure",
            idea_id,
            category="dependency_timeout",
        )

    product = await session.get(Product, product_id)
    assert product is not None
    assert product.heal_attempts == dev_graph.MAX_HEAL_CYCLES

    engine_rows = (
        (
            await session.execute(
                select(Memory).where(Memory.scope == f"engine:{EngineName.DEVELOPMENT.value}")
            )
        )
        .scalars()
        .all()
    )
    assert len(engine_rows) == 1
    assert "exhausted" in engine_rows[0].content


async def test_patch_recalls_the_products_own_heal_history_into_its_prompt(
    session: AsyncSession, tmp_path: Path, blacklist_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole point of writing the memory: the *next* patch attempt must actually
    see it, not just leave it sitting in the table for an audit to find later."""
    ctx = _ctx(tmp_path=tmp_path, blacklist_path=blacklist_file)
    product_id, idea_id = await _make_product(session, "heal-memory-4")

    await dev_graph._heal(
        ctx,
        session,
        product_id,
        "Switching to library X caused a circular import.",
        idea_id,
        category="test_failure",
    )
    # Second cycle's REQUIRED REPAIRS text overlaps enough with the first for
    # recall_block's relevance floor (jaccard-based) to surface the first memory.
    (tmp_path / f"product-{product_id[:12]}").mkdir(parents=True, exist_ok=True)
    task_id = await repository.enqueue_task(
        session,
        kind=repository.TaskKind.PATCH_PRODUCT,
        engine=EngineName.DEVELOPMENT,
        idempotency_key=f"patch:{product_id}:2",
        payload={
            "product_id": product_id,
            "idea_id": idea_id,
            "healing": "Switching to library X still causes a circular import.",
        },
        product_id=product_id,
    )
    assert task_id is not None
    # Fetched by id, not claim_next_task: _heal()'s own priority-40 patch task
    # (auto-enqueued above) would otherwise outrank this priority-100 one and get
    # claimed instead.
    task = await session.get(Task, task_id)
    assert task is not None

    seen_problem_statement: dict[str, str] = {}

    async def fake_generate_scaffold(ctx: object, idea: object, **kwargs: object) -> object:
        seen_problem_statement["text"] = idea.problem_statement  # type: ignore[attr-defined]
        raise RuntimeError("stop before any sandbox/verify work -- prompt already captured")

    monkeypatch.setattr(dev_graph, "generate_scaffold", fake_generate_scaffold)

    with pytest.raises(RuntimeError, match="stop before"):
        await dev_graph._patch(ctx, session, task)

    assert "circular import" in seen_problem_statement["text"]
    assert "prior belief" in seen_problem_statement["text"]


async def test_a_different_products_heal_history_is_never_recalled(
    session: AsyncSession, tmp_path: Path, blacklist_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = _ctx(tmp_path=tmp_path, blacklist_path=blacklist_file)
    product_a, idea_a = await _make_product(session, "heal-memory-a")
    product_b, idea_b = await _make_product(session, "heal-memory-b")

    await dev_graph._heal(
        ctx,
        session,
        product_a,
        "Product A's own unrelated failure signature xyzzy123",
        idea_a,
        category="test_failure",
    )

    (tmp_path / f"product-{product_b[:12]}").mkdir(parents=True, exist_ok=True)
    task_id = await repository.enqueue_task(
        session,
        kind=repository.TaskKind.PATCH_PRODUCT,
        engine=EngineName.DEVELOPMENT,
        idempotency_key=f"patch:{product_b}:1",
        payload={"product_id": product_b, "idea_id": idea_b, "healing": "Product B's own issue."},
        product_id=product_b,
    )
    assert task_id is not None
    # Fetched by id, not claim_next_task: product A's own priority-40 patch task
    # (auto-enqueued by _heal() above) would otherwise outrank this priority-100
    # one and get claimed instead -- which would make this test pass for the wrong
    # reason (recalling product A's memory while "patching" product A, not because
    # of any real cross-product leak).
    task = await session.get(Task, task_id)
    assert task is not None

    seen: dict[str, str] = {}

    async def fake_generate_scaffold(ctx: object, idea: object, **kwargs: object) -> object:
        seen["text"] = idea.problem_statement  # type: ignore[attr-defined]
        raise RuntimeError("stop before any sandbox/verify work")

    monkeypatch.setattr(dev_graph, "generate_scaffold", fake_generate_scaffold)

    with pytest.raises(RuntimeError, match="stop before"):
        await dev_graph._patch(ctx, session, task)

    assert "xyzzy123" not in seen["text"]


async def test_scopes_for_a_product_excludes_a_different_products_scope() -> None:
    """Cheap, DB-free regression guard for the safety property the two tests above
    rely on: recall only ever reads the scopes it is handed."""
    scopes = scopes_for(product_id="product-a")
    assert "product:product-b" not in scopes
