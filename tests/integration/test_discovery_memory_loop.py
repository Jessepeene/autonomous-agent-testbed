"""Closing Discovery's memory loop (Phase 3b of the hardening plan).

Found live during a risk audit: `discovery/graph.py::run()` never read or wrote
`memory_store` at all before this. This exercises the full cycle end-to-end
against a real Postgres session — a pre-seeded lesson at `engine:discovery`
scope must reach the *prompt* (never `system`, which is the tunable value
served by `learning/prompts.py::get_prompt` — corrupting it would poison trial
attribution for GENERATOR_SYSTEM/DISCRIMINATOR_SYSTEM), and a rejected idea
must produce a new lesson at that same scope so the next cycle can read it.

`build_sources` is monkeypatched to a fake, in-memory `Source` so this never
makes a real network call: unlike GitHub/Reddit/Product Hunt (gated on
settings-based credentials a test `Settings()` never sets), HackerNewsSource
is unconditionally `available`, so any un-mitigated call to `run()` would hit
the real Hacker News API. `discovery_scout_enabled=False` avoids the second,
separate real-network/agent-spawn path for the same reason.

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
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from factory.engines.discovery import graph as dgraph
from factory.engines.discovery import score
from factory.engines.discovery.sources.base import Signal
from factory.hitl.telegram import NullNotifier
from factory.llm.client import LLMClient
from factory.llm.providers.base import Provider
from factory.llm.providers.fake import FakeProvider
from factory.llm.router import ProviderRegistry
from factory.memory import store as memory_store
from factory.sandbox.disabled import DisabledSandbox
from factory.settings import EngineName, Settings
from factory.state.orm import Base, Task, new_id
from factory.state.schemas import (
    DiscriminatedIdeas,
    GeneratedIdea,
    IdeaBatch,
    ScoredIdea,
    TaskKind,
    TaskStatus,
)
from factory.supervisor.context import FactoryContext

DATABASE_URL = os.environ.get("DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not DATABASE_URL, reason="DATABASE_URL is not set; integration tests need Postgres"
)

SIGNAL_MARKER = "zzzsigmark77"
MEMORY_MARKER = "zzzmemtoken88"
REJECT_MARKER = "zzzrejectniche99"


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


class _FakeSource:
    """A `Source` (structural Protocol) that returns canned signals with no
    network call — stands in for `build_sources`'s real HTTP-backed sources."""

    def __init__(self, signals: list[Signal]) -> None:
        self.name = "fake"
        self._signals = signals

    @property
    def available(self) -> bool:
        return True

    async def fetch(self, *, limit: int = 40) -> list[Signal]:
        return self._signals


def _fake_signals() -> list[Signal]:
    # Titles carry every token the test cares about, guaranteeing they survive
    # `discovery/graph.py::run`'s `cluster.summary()[:200]` truncation when
    # building `cluster_query` — bodies are realistic filler, not load-bearing.
    return [
        Signal(
            title=f"{SIGNAL_MARKER} dispatch spreadsheet manually loads tracking",
            body=(
                "Every dispatcher at our brokerage spends hours manually reconciling "
                "loads in a spreadsheet because nothing talks to the carrier portal."
            ),
            url="https://example.test/thread/1",
            source="test",
            engagement=40,
        ),
        Signal(
            title=f"{SIGNAL_MARKER} team uses dispatch spreadsheet manually for loads",
            body=(
                "Our dispatch team wastes hours every week manually copying load data "
                "between the TMS and a shared spreadsheet, there is no good tool for this."
            ),
            url="https://example.test/thread/2",
            source="test",
            engagement=35,
        ),
    ]


def _ctx(*, workspace_root: Path, gemini: FakeProvider, **overrides: object) -> FactoryContext:
    settings = Settings(
        database_url=DATABASE_URL,
        gemini_api_key="test-key",
        workspace_root=workspace_root,
        factory_dry_run=False,
        discovery_scout_enabled=False,
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
        enabled_engines={EngineName.DISCOVERY},
        worker_id="discovery-memory-loop-test",
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


def _idea_batch() -> IdeaBatch:
    return IdeaBatch(
        ideas=[
            GeneratedIdea(
                problem_statement=(
                    "Dispatch teams manually reconcile freight loads in spreadsheets "
                    "every week, wasting hours that could go to actually dispatching."
                ),
                target_niche="Dispatch leads at boutique freight brokerages",
                source_url="https://example.test/thread/1",
            ),
            GeneratedIdea(
                problem_statement=(
                    "Small freight brokers have no shared system for tracking loads, "
                    "so dispatchers keep re-deriving state from a fragile spreadsheet."
                ),
                target_niche=f"Ops leads at {REJECT_MARKER} freight brokers",
                source_url="https://example.test/thread/2",
            ),
        ]
    )


def _discriminated_ideas() -> DiscriminatedIdeas:
    survivor = ScoredIdea(
        problem_statement=(
            "Dispatch teams manually reconcile freight loads in spreadsheets "
            "every week, wasting hours that could go to actually dispatching."
        ),
        target_niche="Dispatch leads at boutique freight brokerages",
        source_url="https://example.test/thread/1",
        monetization_hypothesis=(
            "Ops managers at freight brokerages pay $99/month per seat once they "
            "see fewer missed loads from the shared board."
        ),
        pain_severity=0.9,
        willingness_to_pay=0.85,
        build_feasibility=0.8,
        adoption_ease=0.8,
        requires_human_sales=False,
        requires_human_onboarding=False,
        self_serve_price_usd=99.0,
    )
    rejected = ScoredIdea(
        problem_statement=(
            "Small freight brokers have no shared system for tracking loads, "
            "so dispatchers keep re-deriving state from a fragile spreadsheet."
        ),
        target_niche=f"Ops leads at {REJECT_MARKER} freight brokers",
        source_url="https://example.test/thread/2",
        monetization_hypothesis=(
            "Unclear who would pay; teams already tolerate the workaround so "
            "willingness to pay looks low."
        ),
        pain_severity=0.2,
        willingness_to_pay=0.15,
        build_feasibility=0.3,
        adoption_ease=0.2,
        rejection_reason=(
            "Too small a niche, dispatchers already tolerate the spreadsheet workaround."
        ),
    )
    return DiscriminatedIdeas(ideas=[survivor, rejected])


async def test_run_injects_recalled_memory_into_prompt_never_system(
    session: AsyncSession, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(dgraph, "build_sources", lambda settings: [_FakeSource(_fake_signals())])

    await memory_store.remember(
        session,
        content=(
            "Compliance rejected an idea about a dispatch spreadsheet tool: teams "
            f"already track loads manually and incumbents already serve this. "
            f"Reference: {MEMORY_MARKER}"
        ),
        kind=memory_store.MemoryKind.FAILURE,
        scope=memory_store.scope_for(engine=EngineName.DISCOVERY),
        origin="test",
    )
    await session.commit()

    gemini = FakeProvider(name=Provider.GEMINI)
    gemini.register(IdeaBatch, _idea_batch())
    gemini.register(DiscriminatedIdeas, _discriminated_ideas())
    ctx = _ctx(workspace_root=tmp_path, gemini=gemini)

    result = await dgraph.run(ctx, session, _discovery_task())

    assert "1 new" in result

    idea_call = next(c for c in gemini.calls if c["schema"] == "IdeaBatch")
    disc_call = next(c for c in gemini.calls if c["schema"] == "DiscriminatedIdeas")

    # Byte-identical to the tunable row's served value -- no prior tunable_parameters
    # row exists yet, so get_prompt bootstraps from (and returns) the code constant.
    assert idea_call["system"] == score.GENERATOR_SYSTEM
    assert disc_call["system"] == score.DISCRIMINATOR_SYSTEM

    assert MEMORY_MARKER in idea_call["prompt"]
    assert MEMORY_MARKER in disc_call["prompt"]
    assert MEMORY_MARKER not in idea_call["system"]
    assert MEMORY_MARKER not in disc_call["system"]


async def test_run_writes_an_aggregate_lesson_discovery_can_read_next_cycle(
    session: AsyncSession, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(dgraph, "build_sources", lambda settings: [_FakeSource(_fake_signals())])

    gemini = FakeProvider(name=Provider.GEMINI)
    gemini.register(IdeaBatch, _idea_batch())
    gemini.register(DiscriminatedIdeas, _discriminated_ideas())
    ctx = _ctx(workspace_root=tmp_path, gemini=gemini)

    result = await dgraph.run(ctx, session, _discovery_task())
    await session.commit()

    assert "1 new" in result

    recalled = await memory_store.recall(
        session,
        query=f"{REJECT_MARKER} rejected scored ideas",
        scopes=memory_store.scopes_for(engine=EngineName.DISCOVERY),
    )
    assert any(REJECT_MARKER in m.content for m in recalled)
    assert any(m.kind == memory_store.MemoryKind.LESSON.value for m in recalled)
    # The rejection reason the discriminator gave is carried too -- the point of
    # remembering rejections at all is to stop re-proposing the same dead end.
    assert any("spreadsheet workaround" in m.content for m in recalled)
