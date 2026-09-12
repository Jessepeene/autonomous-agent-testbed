"""`GET /dashboard/products/{id}` -- the product detail view. Business plan
comes from a real `Idea` join (`repository.get_product_with_idea`, no new
generation); design brief is read back from `Product.design_brief`
(migration 0030) if one has been generated.

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

from factory.engines.development.design_brief import DesignBrief
from factory.observability import dashboard as dashboard_metrics
from factory.settings import get_settings
from factory.state import repository
from factory.state.orm import Base
from factory.state.schemas import ProductStatus, ScoredIdea

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
    get_settings.cache_clear()


async def _client() -> httpx.AsyncClient:
    from factory.api.app import app

    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def _make_product(session: AsyncSession, niche: str, **product_kwargs: object) -> str:
    idea = ScoredIdea(
        problem_statement="Bookkeepers reconcile invoices by hand every single week.",
        target_niche=niche,
        source_url="https://example.test/thread",
        monetization_hypothesis="EUR 49/month, bought after the first month-end close.",
        pain_severity=0.8,
        willingness_to_pay=0.7,
        build_feasibility=0.6,
        adoption_ease=0.8,
    )
    idea_id = await repository.upsert_idea(session, idea)
    assert idea_id is not None
    product_id = await repository.create_product(session, name=niche, idea_id=idea_id)
    if product_kwargs:
        await repository.set_product_status(
            session, product_id, ProductStatus.LIVE, **product_kwargs
        )
    return product_id


# --- observability.dashboard.collect_product_detail -------------------------------------


async def test_collect_product_detail_returns_none_for_an_unknown_product(
    session: AsyncSession,
) -> None:
    assert await dashboard_metrics.collect_product_detail(session, "not-a-real-id") is None


async def test_collect_product_detail_includes_the_real_business_plan(
    session: AsyncSession,
) -> None:
    product_id = await _make_product(session, "product-detail-business-plan")
    await session.flush()

    detail = await dashboard_metrics.collect_product_detail(session, product_id)

    assert detail is not None
    assert detail.business_plan.target_niche == "product-detail-business-plan"
    assert "Bookkeepers reconcile invoices" in detail.business_plan.problem_statement
    assert detail.business_plan.monetization_hypothesis.startswith("EUR 49/month")
    assert detail.business_plan.pain_severity == 0.8


async def test_collect_product_detail_has_no_design_brief_when_none_was_generated(
    session: AsyncSession,
) -> None:
    product_id = await _make_product(session, "product-detail-no-brief")
    await session.flush()

    detail = await dashboard_metrics.collect_product_detail(session, product_id)

    assert detail is not None
    assert detail.design_brief is None


async def test_collect_product_detail_includes_a_cached_design_brief(
    session: AsyncSession,
) -> None:
    product_id = await _make_product(session, "product-detail-with-brief")
    brief = DesignBrief(
        palette=["#0B0E14", "#E2A33D"],
        typography="A bold sans-serif headline over a system-UI body.",
        tone="Minimal and technical.",
    )
    await repository.set_design_brief(session, product_id, brief.model_dump_json())
    await session.flush()

    detail = await dashboard_metrics.collect_product_detail(session, product_id)

    assert detail is not None
    assert detail.design_brief is not None
    assert detail.design_brief.palette == ["#0B0E14", "#E2A33D"]
    assert detail.design_brief.tone == "Minimal and technical."


# --- GET /dashboard/products/{id}: the full endpoint -------------------------------------


async def test_product_detail_endpoint_404s_on_an_unknown_product(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", DATABASE_URL or "")
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("DASHBOARD_AUTH_TOKEN", "correct-horse-battery-staple")

    async with await _client() as client:
        response = await client.get(
            "/dashboard/products/not-a-real-id",
            headers={"Authorization": "Bearer correct-horse-battery-staple"},
        )

    assert response.status_code == 404


async def test_product_detail_endpoint_returns_the_real_business_plan_and_live_link(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DATABASE_URL", DATABASE_URL or "")
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("DASHBOARD_AUTH_TOKEN", "correct-horse-battery-staple")
    product_id = await _make_product(
        session, "product-detail-endpoint", deploy_url="https://example-product.test"
    )
    await session.commit()

    async with await _client() as client:
        response = await client.get(
            f"/dashboard/products/{product_id}",
            headers={"Authorization": "Bearer correct-horse-battery-staple"},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["deploy_url"] == "https://example-product.test"
    assert body["business_plan"]["target_niche"] == "product-detail-endpoint"
    assert body["design_brief"] is None
