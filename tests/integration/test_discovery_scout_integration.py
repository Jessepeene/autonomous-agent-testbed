"""DISCOVERY_SCOUT: the agentic complement to Discovery's fixed HN/GitHub/Reddit/
Product Hunt sources -- see `engines/discovery/scout.py` and `tools/discovery.py`.

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

from factory.engines.discovery.scout import scout_signals
from factory.hitl.telegram import NullNotifier
from factory.llm.client import LLMClient
from factory.llm.providers.base import AgentTurn, Provider, ToolCall, Usage
from factory.llm.providers.fake import FakeProvider
from factory.llm.router import ProviderRegistry
from factory.sandbox.disabled import DisabledSandbox
from factory.settings import EngineName, Settings
from factory.state import repository
from factory.state.orm import Base, Task, new_id
from factory.state.schemas import TaskKind, TaskStatus
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


@pytest.fixture(autouse=True)
def _stub_http_get_for_signal_grounding(monkeypatch: pytest.MonkeyPatch) -> None:
    """`submit_signal` now fetches the real URL and checks the quote against
    it (Tier 2 finding #9, system-wide verification plan) -- stubbed here so
    every test in this file that submits `_submit_signal_turn`'s default
    quote (or a variant it also carries) is grounded without a real network
    call. Tests exercising the grounding check itself override this per-test.
    """
    from factory.tools import builtin

    async def fake_http_get(url: str) -> dict[str, object]:
        return {
            "status_code": 200,
            "url": url,
            "body": "<html>I spent hours manually reconciling this every single week.</html>",
        }

    monkeypatch.setattr(builtin, "http_get", fake_http_get)


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
        enabled_engines={EngineName.DISCOVERY},
        worker_id="discovery-scout-test",
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


def _completion(text: str) -> AgentTurn:
    return AgentTurn(
        text=text,
        tool_calls=[],
        stop_reason="end_turn",
        provider=Provider.GEMINI,
        model="gemini-2.5-flash",
        usage=Usage(input_tokens=10, output_tokens=10),
    )


def _submit_signal_turn(task_id: str, **overrides: object) -> AgentTurn:
    arguments: dict[str, object] = {
        "task_id": task_id,
        "title": "Nobody has a good tool for X",
        "quote": "I spent hours manually reconciling this every single week.",
        "url": "https://example.test/thread/1",
        "source_label": "indiehackers",
        # >=25 so a lone signal survives `cluster_signals`'s singleton engagement
        # gate (`engines/discovery/cluster.py`) -- otherwise it never even reaches
        # a cluster, which would be testing clustering, not the source merge.
        "engagement": 30,
    }
    arguments.update(overrides)
    return AgentTurn(
        text="",
        tool_calls=[ToolCall(id="call-1", name="submit_signal", arguments=arguments)],
        stop_reason="tool_use",
        provider=Provider.GEMINI,
        model="gemini-2.5-flash",
        usage=Usage(input_tokens=10, output_tokens=10),
    )


# --- repository: record_scout_signal / drain_scout_signals -------------------------


async def test_drain_scout_signals_returns_and_deletes_only_its_own_task(
    session: AsyncSession,
) -> None:
    await repository.record_scout_signal(
        session,
        task_id="task-a",
        title="t1",
        body="b1",
        url="https://example.test/1",
        source="indiehackers",
        engagement=5,
    )
    await repository.record_scout_signal(
        session,
        task_id="task-b",
        title="t2",
        body="b2",
        url="https://example.test/2",
        source="devto",
    )
    await session.flush()

    drained = await repository.drain_scout_signals(session, "task-a")
    assert [row.title for row in drained] == ["t1"]

    # Draining is one-shot: a second call for the same task finds nothing left.
    assert await repository.drain_scout_signals(session, "task-a") == []
    # The other task's row is untouched.
    other = await repository.drain_scout_signals(session, "task-b")
    assert [row.title for row in other] == ["t2"]


# --- tools/discovery.py: submit_signal, called directly ----------------------------


async def test_submit_signal_records_a_row_scoped_to_task_id(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    from factory.settings import get_settings
    from factory.tools import builtin
    from factory.tools.discovery import submit_signal

    monkeypatch.setenv("DATABASE_URL", DATABASE_URL or "")
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    get_settings.cache_clear()

    quote = "We still do this in a spreadsheet every Friday."

    async def fake_http_get(url: str) -> dict[str, object]:
        return {"status_code": 200, "url": url, "body": f"<html>{quote}</html>"}

    monkeypatch.setattr(builtin, "http_get", fake_http_get)

    result = await submit_signal(
        task_id="task-x",
        title="Manual invoice reconciliation",
        quote=quote,
        url="https://example.test/review/1",
        source_label="g2_reviews",
        engagement=3,
    )
    assert result["recorded"] is True

    rows = await repository.drain_scout_signals(session, "task-x")
    assert len(rows) == 1
    assert rows[0].source == "g2_reviews"
    assert rows[0].engagement == 3

    get_settings.cache_clear()


async def test_submit_signal_rejects_a_quote_not_actually_on_the_page(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tier 2 finding #9, system-wide verification plan: DISCOVERY_SCOUT has
    the identical `http` tool and identical hallucination risk as
    `marketing/providers/website.py`'s extraction, which already has this
    exact backstop."""
    from factory.settings import get_settings
    from factory.tools import builtin
    from factory.tools.discovery import submit_signal

    monkeypatch.setenv("DATABASE_URL", DATABASE_URL or "")
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    get_settings.cache_clear()

    async def fake_http_get(url: str) -> dict[str, object]:
        return {"status_code": 200, "url": url, "body": "<html>completely unrelated content</html>"}

    monkeypatch.setattr(builtin, "http_get", fake_http_get)

    result = await submit_signal(
        task_id="task-z",
        title="Manual invoice reconciliation",
        quote="A sentence that was never actually on this page.",
        url="https://example.test/review/1",
        source_label="g2_reviews",
    )

    assert "error" in result
    assert await repository.drain_scout_signals(session, "task-z") == []

    get_settings.cache_clear()


async def test_submit_signal_rejects_a_non_http_url(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    from factory.settings import get_settings
    from factory.tools.discovery import submit_signal

    monkeypatch.setenv("DATABASE_URL", DATABASE_URL or "")
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    get_settings.cache_clear()

    result = await submit_signal(
        task_id="task-y",
        title="t",
        quote="q",
        url="not-a-url",
        source_label="devto",
    )
    assert "error" in result
    assert await repository.drain_scout_signals(session, "task-y") == []

    get_settings.cache_clear()


# --- engines/discovery/scout.py: scout_signals() ------------------------------------


async def test_scout_signals_returns_what_the_agent_submitted(
    session: AsyncSession, tmp_path: Path
) -> None:
    from factory.tools import discovery  # noqa: F401 - import populates REGISTRY

    task = _discovery_task()
    provider = FakeProvider(name=Provider.GEMINI)
    provider.register_turns(
        _submit_signal_turn(task.id),
        _completion("Found one strong signal and reported it."),
    )
    ctx = _ctx(workspace_root=tmp_path, gemini=provider)

    signals = await scout_signals(ctx, session, task)

    assert len(signals) == 1
    assert signals[0].source == "scout:indiehackers"
    assert "reconciling" in signals[0].body
    assert signals[0].url == "https://example.test/thread/1"


async def test_scout_signals_drains_the_buffer_so_nothing_leaks_to_the_next_cycle(
    session: AsyncSession, tmp_path: Path
) -> None:
    from factory.tools import discovery  # noqa: F401 - import populates REGISTRY

    task = _discovery_task()
    provider = FakeProvider(name=Provider.GEMINI)
    provider.register_turns(
        _submit_signal_turn(task.id),
        _completion("Done."),
    )
    ctx = _ctx(workspace_root=tmp_path, gemini=provider)

    await scout_signals(ctx, session, task)

    assert await repository.drain_scout_signals(session, task.id) == []


async def test_scout_signals_returns_empty_and_never_calls_the_model_when_disabled(
    session: AsyncSession, tmp_path: Path
) -> None:
    task = _discovery_task()
    # Deliberately no queued turn: if the provider were called, FakeProvider raises.
    provider = FakeProvider(name=Provider.GEMINI)
    ctx = _ctx(workspace_root=tmp_path, gemini=provider, discovery_scout_enabled=False)

    signals = await scout_signals(ctx, session, task)

    assert signals == []
    assert provider.conversations == []


async def test_scout_signals_tolerates_finding_nothing(
    session: AsyncSession, tmp_path: Path
) -> None:
    """Submitting nothing because nothing real was found is a legitimate outcome
    (see the role's own system prompt) -- must not be treated as a failure."""
    from factory.tools import discovery  # noqa: F401 - import populates REGISTRY

    task = _discovery_task()
    provider = FakeProvider(name=Provider.GEMINI)
    provider.register_turns(_completion("Looked in several places; nothing real to report."))
    ctx = _ctx(workspace_root=tmp_path, gemini=provider)

    signals = await scout_signals(ctx, session, task)

    assert signals == []


# --- engines/discovery/graph.py::run() merges fixed + scouted signals --------------


async def test_run_merges_scouted_signals_with_zero_fixed_sources(
    session: AsyncSession, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With every fixed source unconfigured (`build_sources` returns none), a
    scouted signal must still reach the clusterer -- Discovery's flow must not
    depend on the fixed sources being present. The generator is scripted to
    return no ideas so the discriminator short-circuits (`score.py`'s own
    documented behaviour) and this stays a test of the merge, not of scoring."""
    from factory.engines.discovery import graph as discovery_graph
    from factory.state.schemas import IdeaBatch
    from factory.tools import discovery  # noqa: F401 - import populates REGISTRY

    monkeypatch.setattr(discovery_graph, "build_sources", lambda settings: [])

    task = _discovery_task()
    provider = FakeProvider(name=Provider.GEMINI)
    provider.register_turns(
        _submit_signal_turn(task.id),
        _completion("Found one strong signal and reported it."),
    )
    provider.register(IdeaBatch, IdeaBatch(ideas=[]))
    ctx = _ctx(workspace_root=tmp_path, gemini=provider)

    result = await discovery_graph.run(ctx, session, task)

    assert "1 signals (1 scouted)" in result
