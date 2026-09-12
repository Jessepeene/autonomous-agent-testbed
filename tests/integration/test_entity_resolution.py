"""Entity-resolution caching, end to end.

Needs Postgres:

    docker compose up -d postgres
    alembic upgrade head
    pytest tests/integration -q
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

import httpx
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from factory.engines.compliance.entity import resolve_entity_type
from factory.llm.providers.base import AgentTurn, Provider, ToolCall, Usage
from factory.llm.providers.fake import FakeProvider
from factory.state.orm import Base
from factory.state.schemas import EntityType, EntityVerdict
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


def _agent_turn(text: str) -> AgentTurn:
    return AgentTurn(
        text=text,
        tool_calls=[],
        stop_reason="end_turn",
        provider=Provider.GEMINI,
        model="gemini-2.5-flash",
        usage=Usage(input_tokens=10, output_tokens=10),
    )


def _tool_call_turn() -> AgentTurn:
    return AgentTurn(
        text="",
        tool_calls=[ToolCall(id="1", name="http_get", arguments={"url": "https://engines.test"})],
        stop_reason="tool_use",
        provider=Provider.GEMINI,
        model="gemini-2.5-flash",
        usage=Usage(input_tokens=10, output_tokens=10),
    )


@pytest.fixture(autouse=True)
def _stub_http_get(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every "successful investigation" fixture below now includes a real
    `http_get` tool call (Tier 1 finding #2, system-wide verification plan:
    `resolve_entity_type` requires evidence the RESEARCHER actually fetched
    something before trusting its account of it) -- stub the real network
    call it would otherwise make."""

    async def fake_get(self: httpx.AsyncClient, url: str, **kwargs: object) -> httpx.Response:
        return httpx.Response(
            200, text="<html>imprint page</html>", request=httpx.Request("GET", url)
        )

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)


async def test_first_resolution_runs_the_agent_and_caches_the_verdict(
    ctx: FactoryContext, gemini_fake: FakeProvider, session: AsyncSession
) -> None:
    gemini_fake.register_turns(
        _tool_call_turn(),
        _agent_turn("The imprint page names 'Analytical Engines B.V.', a registered company."),
    )
    gemini_fake.register(
        EntityVerdict,
        EntityVerdict(
            entity_type=EntityType.CORPORATE,
            confidence=0.9,
            evidence="imprint page names a registered BV",
        ),
    )

    verdict = await resolve_entity_type(
        ctx, session, company="Analytical Engines BV", email_domain="engines.test"
    )
    await session.commit()

    assert verdict.entity_type is EntityType.CORPORATE
    # 2, not 1: the agent run is two turns (tool-call, then final answer) --
    # each turn is its own `converse()` call/conversation entry. The separate
    # EntityVerdict extraction call goes through `.parse()`, tracked in
    # `.calls`, not `.conversations`.
    assert len(gemini_fake.conversations) == 2


async def test_second_lookup_for_the_same_domain_hits_the_cache(
    ctx: FactoryContext, gemini_fake: FakeProvider, session: AsyncSession
) -> None:
    """No second turn or extraction response is queued — if the agent ran again,
    `FakeProvider` would raise, failing the test."""
    gemini_fake.register_turns(
        _tool_call_turn(), _agent_turn("Registered company, per its imprint page.")
    )
    gemini_fake.register(
        EntityVerdict,
        EntityVerdict(
            entity_type=EntityType.CORPORATE, confidence=0.9, evidence="imprint names a BV"
        ),
    )
    first = await resolve_entity_type(
        ctx, session, company="Engines BV", email_domain="engines.test"
    )
    await session.commit()

    second = await resolve_entity_type(
        ctx, session, company="Engines BV", email_domain="engines.test"
    )

    assert first.entity_type is second.entity_type is EntityType.CORPORATE
    # 2, not 1 -- see the comment on the first test in this file: one 2-turn
    # agent run. The cached second lookup adds no further conversations.
    assert len(gemini_fake.conversations) == 2


async def test_a_different_domain_is_resolved_independently(
    ctx: FactoryContext, gemini_fake: FakeProvider, session: AsyncSession
) -> None:
    gemini_fake.register_turns(_tool_call_turn(), _agent_turn("Found a BV in the imprint."))
    gemini_fake.register(
        EntityVerdict,
        EntityVerdict(
            entity_type=EntityType.CORPORATE, confidence=0.9, evidence="imprint names a BV"
        ),
    )
    await resolve_entity_type(ctx, session, company="Engines BV", email_domain="engines.test")
    await session.commit()

    gemini_fake.register_turns(
        _tool_call_turn(), _agent_turn("This looks like a personal blog, not a company.")
    )
    gemini_fake.register(
        EntityVerdict,
        EntityVerdict(
            entity_type=EntityType.SOLE_PROPRIETOR_OR_INDIVIDUAL,
            confidence=0.8,
            evidence="personal blog, no company registration found",
        ),
    )
    other = await resolve_entity_type(
        ctx, session, company="Someone's Blog", email_domain="someones-blog.test"
    )

    assert other.entity_type is EntityType.SOLE_PROPRIETOR_OR_INDIVIDUAL
    # 4, not 2 -- two independent 2-turn agent runs, see the comment on the
    # first test in this file.
    assert len(gemini_fake.conversations) == 4


async def test_an_agent_that_does_not_complete_caches_unknown_not_a_retry_forever(
    ctx: FactoryContext,
    gemini_fake: FakeProvider,
    anthropic_fake: FakeProvider,
    session: AsyncSession,
) -> None:
    from factory.llm.providers.base import ProviderUnavailable

    anthropic_fake.available_flag = False
    for _ in range(3):
        gemini_fake.register_turns(ProviderUnavailable(Provider.GEMINI, "503", status=503))

    verdict = await resolve_entity_type(
        ctx, session, company="Engines BV", email_domain="engines.test"
    )
    await session.commit()

    assert verdict.entity_type is EntityType.UNKNOWN
    # Cached as unknown — a second call must not re-attempt the agent (no further
    # turns are queued; if it tried, `FakeProvider` would raise).
    again = await resolve_entity_type(
        ctx, session, company="Engines BV", email_domain="engines.test"
    )
    assert again.entity_type is EntityType.UNKNOWN


async def test_a_confident_answer_with_no_real_investigation_resolves_to_unknown(
    ctx: FactoryContext, gemini_fake: FakeProvider, session: AsyncSession
) -> None:
    """Tier 1 finding #2, system-wide verification plan: a RESEARCHER that
    never actually calls `http_get` and confidently states "found a KVK
    registration, this is a BV" must not be trusted -- `result.succeeded`
    alone (a final turn, no tool calls, non-empty text) previously satisfied
    this exactly as well as a genuine investigation, and CORPORATE at real
    confidence would have waved a possibly-protected individual through to
    the LIA. No `http_get` call is queued here at all -- if the code tried to
    make one, the network stub `_stub_http_get` would still intercept it, but
    the point under test is that `resolve_entity_type` never gets far enough
    to try, because `result.used_tool("http_get")` is false on a zero-tool
    turn."""
    gemini_fake.register_turns(
        _agent_turn("Found a KVK registration; this is clearly a registered BV.")
    )

    verdict = await resolve_entity_type(
        ctx, session, company="Suspiciously Confident BV", email_domain="unverified.test"
    )
    await session.commit()

    assert verdict.entity_type is EntityType.UNKNOWN
    assert verdict.confidence == 0.0
