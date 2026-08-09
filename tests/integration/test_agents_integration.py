"""The `spawn_agent` supervisor node, end to end against a real database.

Unlike the unit-level spawner tests (which call `spawn()` directly with a session
handed in), this exercises `_spawn_agent()` itself — the thin node wrapper that owns
the transaction and the unknown-role fallback. Both paths write through
`session_scope`, which needs Postgres for the JSONB task payloads involved in the
backlog fallback, so this lives here rather than in the unit suite.

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
from sqlalchemy.ext.asyncio import create_async_engine

from factory.hitl.telegram import NullNotifier
from factory.llm.client import LLMClient
from factory.llm.providers.base import AgentTurn, Provider, Usage
from factory.llm.providers.fake import FakeProvider
from factory.llm.router import ProviderRegistry
from factory.sandbox.disabled import DisabledSandbox
from factory.settings import EngineName, Settings
from factory.state import db as db_module
from factory.state.orm import Base
from factory.state.schemas import NextAction
from factory.supervisor.context import FactoryContext
from factory.supervisor.graph import SupervisorState, _spawn_agent

DATABASE_URL = os.environ.get("DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not DATABASE_URL, reason="DATABASE_URL is not set; integration tests need Postgres"
)


@pytest_asyncio.fixture
async def prepared_schema() -> AsyncIterator[None]:
    engine = create_async_engine(DATABASE_URL or "", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    await engine.dispose()
    yield
    # `session_scope` reads a process-wide cached engine keyed by nothing but "the
    # first settings it saw" — reset it so each test binds fresh to this test's URL
    # instead of leaking a stale connection pool into the next one.
    await db_module.dispose_engine()


@pytest.fixture
def gemini_fake() -> FakeProvider:
    return FakeProvider(Provider.GEMINI)


@pytest.fixture
def db_ctx(prepared_schema: None, gemini_fake: FakeProvider, tmp_path: Path) -> FactoryContext:
    settings = Settings(
        database_url=DATABASE_URL,
        gemini_api_key="test-key",
        workspace_root=tmp_path,
    )
    llm = LLMClient(ProviderRegistry({Provider.GEMINI: gemini_fake}), settings=settings)
    return FactoryContext(
        settings=settings,
        llm=llm,
        notifier=NullNotifier(),
        sandbox=DisabledSandbox(),
        enabled_engines={EngineName.SUPERVISOR},
        docker_available=False,
        worker_id="integration-test",
    )


async def test_spawn_agent_node_runs_the_requested_role(
    db_ctx: FactoryContext, gemini_fake: FakeProvider
) -> None:
    gemini_fake.register_turns(
        AgentTurn(
            text="Costs spiked because effort was set to max on a routine task.",
            tool_calls=[],
            stop_reason="end_turn",
            provider=Provider.GEMINI,
            model="gemini-2.5-flash",
            usage=Usage(input_tokens=5, output_tokens=5),
        )
    )
    state: SupervisorState = {
        "action": NextAction.SPAWN_AGENT.value,
        "agent_role": "analyst",
        "agent_objective": "figure out why costs spiked",
    }

    outcome = await _spawn_agent(state, ctx=db_ctx)

    assert "agent/analyst" in outcome["note"]
    assert "completed" in outcome["note"]


async def test_spawn_agent_node_falls_back_to_backlog_on_an_unknown_role(
    db_ctx: FactoryContext, gemini_fake: FakeProvider
) -> None:
    """A hallucinated role must not lose the tick — it degrades to maintenance."""
    state: SupervisorState = {
        "action": NextAction.SPAWN_AGENT.value,
        "agent_role": "prompt_engineer",
        "agent_objective": "anything",
    }

    outcome = await _spawn_agent(state, ctx=db_ctx)

    assert "backlog" in outcome["note"]
    assert gemini_fake.conversations == []
