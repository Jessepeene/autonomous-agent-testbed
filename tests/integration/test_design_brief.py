"""`development/design_brief.py::build_design_brief` -- generate once from
the product's real deployed page, cache on `Product.design_brief` (migration
0030), reuse on every later call. DB-dependent (the cache lives on a real
`Product` row), so this lives here rather than in the unit suite -- same
reasoning as `build_qualification_brief`'s own integration tests
(`test_website_provider_integration.py`), which this mirrors closely.

Needs Postgres:

    docker compose up -d postgres
    alembic upgrade head
    pytest tests/integration -q
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from factory.engines.development.design_brief import DesignBrief, build_design_brief
from factory.hitl.telegram import NullNotifier
from factory.llm.client import LLMClient
from factory.llm.providers.base import LLMError, Provider
from factory.llm.providers.fake import FakeProvider
from factory.llm.router import ProviderRegistry
from factory.sandbox.disabled import DisabledSandbox
from factory.settings import EngineName, Settings
from factory.state import repository
from factory.state.orm import Base
from factory.state.schemas import ScoredIdea
from factory.supervisor.context import FactoryContext

DATABASE_URL = os.environ.get("DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not DATABASE_URL, reason="DATABASE_URL is not set; integration tests need Postgres"
)

_DEPLOY_URL = "https://example-product.test/"


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


def _ctx(gemini: FakeProvider, *, workspace_root: Path) -> FactoryContext:
    settings = Settings(
        database_url=DATABASE_URL, gemini_api_key="test-key", workspace_root=workspace_root
    )
    llm = LLMClient(ProviderRegistry({Provider.GEMINI: gemini}), settings=settings)
    return FactoryContext(
        settings=settings,
        llm=llm,
        notifier=NullNotifier(),
        sandbox=DisabledSandbox(),
        enabled_engines={EngineName.DEVELOPMENT},
        worker_id="design-brief-integration-test",
    )


async def _make_product(session: AsyncSession, name: str) -> str:
    idea = ScoredIdea(
        problem_statement="Bookkeepers reconcile invoices by hand every single week.",
        target_niche=name,
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


def _design_brief() -> DesignBrief:
    return DesignBrief(
        palette=["#0B0E14", "#E2A33D"],
        typography="A bold sans-serif headline over a smaller system-UI body.",
        tone="Minimal and technical, aimed at an engineering audience.",
    )


async def test_design_brief_is_generated_once_and_reused(
    session: AsyncSession, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only one fetch and one `DesignBrief` response are queued below -- if
    the second call tried to fetch or regenerate, the mock/`FakeProvider`
    would raise, failing this test."""
    product_id = await _make_product(session, "design-brief-cache-product")
    await session.flush()

    fetch_count = 0

    async def fake_get(self: httpx.AsyncClient, url: str, **kwargs: object) -> httpx.Response:
        nonlocal fetch_count
        fetch_count += 1
        return httpx.Response(
            200,
            text="<html><body style='background:#0B0E14'>Hi</body></html>",
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)

    gemini = FakeProvider()
    gemini.register(DesignBrief, _design_brief())
    ctx = _ctx(gemini, workspace_root=tmp_path)

    first = await build_design_brief(
        ctx, session, product_id=product_id, deploy_url=_DEPLOY_URL
    )
    await session.commit()
    assert first is not None
    assert first.palette == ["#0B0E14", "#E2A33D"]
    assert fetch_count == 1

    second = await build_design_brief(
        ctx, session, product_id=product_id, deploy_url=_DEPLOY_URL
    )

    assert second == first
    assert fetch_count == 1, "a cache hit must not re-fetch the live page"


async def test_design_brief_returns_none_when_the_live_page_cannot_be_fetched(
    session: AsyncSession, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    product_id = await _make_product(session, "design-brief-fetch-failure-product")
    await session.flush()

    async def fake_get(self: httpx.AsyncClient, url: str, **kwargs: object) -> httpx.Response:
        return httpx.Response(503, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)

    gemini = FakeProvider()
    ctx = _ctx(gemini, workspace_root=tmp_path)

    result = await build_design_brief(ctx, session, product_id=product_id, deploy_url=_DEPLOY_URL)

    assert result is None
    product = await repository.get_product(session, product_id)
    assert product is not None
    assert product.design_brief is None


async def test_design_brief_generation_failure_returns_none(
    session: AsyncSession, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    product_id = await _make_product(session, "design-brief-llm-failure-product")
    await session.flush()

    async def fake_get(self: httpx.AsyncClient, url: str, **kwargs: object) -> httpx.Response:
        return httpx.Response(200, text="<html>ok</html>", request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)

    gemini = FakeProvider()
    gemini.register(DesignBrief, LLMError(Provider.GEMINI, "schema violation"))
    ctx = _ctx(gemini, workspace_root=tmp_path)

    result = await build_design_brief(ctx, session, product_id=product_id, deploy_url=_DEPLOY_URL)

    assert result is None
    product = await repository.get_product(session, product_id)
    assert product is not None
    assert product.design_brief is None
