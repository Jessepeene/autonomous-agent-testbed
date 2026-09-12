"""Tier 2 finding #8, system-wide verification plan: `_evidence_count_for`
(`discovery/graph.py`) was computed and stored correctly since the dedup fix
earlier this session, but nothing ever acted on a zero result -- an idea
whose `source_url` doesn't match any real cluster signal (the generator
paraphrased or invented it, despite `GENERATOR_SYSTEM`'s "never invent one")
was stored and queued for compliance exactly like a well-evidenced one.
`run()` now rejects it outright before it ever reaches a database row.

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

from factory.engines.discovery import graph as discovery_graph
from factory.hitl.telegram import NullNotifier
from factory.llm.client import LLMClient
from factory.llm.providers.base import Provider
from factory.llm.providers.fake import FakeProvider
from factory.llm.router import ProviderRegistry
from factory.sandbox.disabled import DisabledSandbox
from factory.settings import EngineName, Settings
from factory.state.orm import Base, Idea, Task, new_id
from factory.state.schemas import GeneratedIdea, IdeaBatch, ScoredIdea, TaskKind, TaskStatus
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


def _ctx(*, workspace_root: Path, gemini: FakeProvider) -> FactoryContext:
    settings = Settings(
        database_url=DATABASE_URL,
        gemini_api_key="test-key",
        workspace_root=workspace_root,
        factory_dry_run=False,
        discovery_scout_enabled=False,
    )
    llm = LLMClient(ProviderRegistry({Provider.GEMINI: gemini}), settings=settings)
    return FactoryContext(
        settings=settings,
        llm=llm,
        notifier=NullNotifier(),
        sandbox=DisabledSandbox(),
        enabled_engines={EngineName.DISCOVERY},
        worker_id="evidence-count-gate-test",
    )


def _discovery_task() -> Task:
    return Task(
        id=new_id(),
        kind=TaskKind.DISCOVER_OPPORTUNITIES.value,
        engine=EngineName.DISCOVERY.value,
        status=TaskStatus.CLAIMED.value,
        priority=200,
        payload={},
        attempt_count=1,
    )


_REAL_SIGNAL_URL = "https://real-source.test/thread/1"
_FABRICATED_SOURCE_URL = "https://never-actually-a-real-signal.test/invented"


def _scored_idea(source_url: str) -> ScoredIdea:
    return ScoredIdea(
        problem_statement="Bookkeepers reconcile invoices by hand every week, wasting hours.",
        target_niche="Bookkeepers at 5-20 person Dutch accounting firms",
        source_url=source_url,
        monetization_hypothesis="Bookkeepers pay EUR 49/month per seat for reconciliation.",
        pain_severity=0.8,
        willingness_to_pay=0.7,
        build_feasibility=0.6,
        adoption_ease=0.8,
    )


async def test_an_idea_whose_source_url_matches_no_real_signal_is_rejected(
    session: AsyncSession, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The generator's `source_url` doesn't match any signal HackerNews (the
    one fixed source left enabled) actually returned -- exactly the
    fingerprint of a paraphrased or invented URL. The idea must never reach a
    database row or a compliance task."""
    from factory.engines.discovery.sources.base import Signal
    from factory.state.schemas import DiscriminatedIdeas

    async def fake_gather(sources: object) -> list[Signal]:
        # `sources` ignored entirely -- this replaces `gather_signals` wholesale
        # so `build_sources`'s real per-source `.available` checks (and the
        # real network calls behind them) never run.
        return [
            Signal(
                title="Nobody has a good tool for X",
                body="I spent hours manually reconciling this every single week.",
                url=_REAL_SIGNAL_URL,
                source="hackernews",
                engagement=30,
            )
        ]

    monkeypatch.setattr(discovery_graph, "gather_signals", fake_gather)

    problem_statement = "Bookkeepers reconcile invoices by hand every week, wasting hours."
    target_niche = "Bookkeepers at 5-20 person Dutch accounting firms"

    provider = FakeProvider(name=Provider.GEMINI)
    provider.register(
        IdeaBatch,
        IdeaBatch(
            ideas=[
                GeneratedIdea(
                    problem_statement=problem_statement,
                    target_niche=target_niche,
                    source_url=_FABRICATED_SOURCE_URL,
                )
            ]
        ),
    )
    provider.register(
        DiscriminatedIdeas, DiscriminatedIdeas(ideas=[_scored_idea(_FABRICATED_SOURCE_URL)])
    )

    ctx = _ctx(workspace_root=tmp_path, gemini=provider)
    task = _discovery_task()

    result = await discovery_graph.run(ctx, session, task)
    await session.commit()

    assert "0 new" in result
    assert "1 rejected for no matching evidence" in result
    rows = (await session.execute(select(Idea))).scalars().all()
    assert rows == []
