"""`POST /dashboard/products/{id}/design-brief/generate` and the
`GENERATE_DESIGN_BRIEF` task it enqueues -- the API never calls an LLM
itself (see `design_brief.py`'s own module docstring for why); it writes
intent, and `development/graph.py::run`'s new dispatch branch does the real
work. This is what lets a product that went live before this feature
existed get a design brief without a rebuild.

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
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from factory.engines.development import graph as dev_graph
from factory.engines.development.design_brief import DesignBrief
from factory.hitl.telegram import NullNotifier
from factory.llm.client import LLMClient
from factory.llm.providers.base import Provider
from factory.llm.providers.fake import FakeProvider
from factory.llm.router import ProviderRegistry
from factory.sandbox.disabled import DisabledSandbox
from factory.settings import EngineName, Settings, get_settings
from factory.state import repository
from factory.state.orm import Base, Task, new_id
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
    get_settings.cache_clear()


async def _client() -> httpx.AsyncClient:
    from factory.api.app import app

    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


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
        worker_id="generate-design-brief-test",
    )


async def _make_product(
    session: AsyncSession, niche: str, *, deploy_url: str | None = None
) -> str:
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
    if deploy_url:
        await repository.set_product_status(
            session, product_id, ProductStatus.LIVE, deploy_url=deploy_url
        )
    return product_id


def _generate_task(product_id: str) -> Task:
    return Task(
        id=new_id(),
        kind=TaskKind.GENERATE_DESIGN_BRIEF.value,
        engine=EngineName.DEVELOPMENT.value,
        status=TaskStatus.CLAIMED.value,
        priority=200,
        payload={"product_id": product_id},
        product_id=product_id,
        attempt_count=1,
    )


# --- development/graph.py::run dispatch for GENERATE_DESIGN_BRIEF ---------------------


async def test_generate_design_brief_task_generates_and_caches(
    session: AsyncSession, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    product_id = await _make_product(
        session, "generate-brief-task", deploy_url="https://example-product.test"
    )
    await session.flush()

    async def fake_get(self: httpx.AsyncClient, url: str, **kwargs: object) -> httpx.Response:
        return httpx.Response(
            200, text="<html>a real page</html>", request=httpx.Request("GET", url)
        )

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)

    gemini = FakeProvider()
    gemini.register(
        DesignBrief,
        DesignBrief(
            palette=["#0B0E14", "#E2A33D"],
            typography="A bold sans-serif headline.",
            tone="Minimal and technical.",
        ),
    )
    ctx = _ctx(gemini, workspace_root=tmp_path)

    result = await dev_graph.run(ctx, session, _generate_task(product_id))

    assert "generated" in result
    product = await repository.get_product(session, product_id)
    assert product is not None
    assert product.design_brief is not None
    brief = DesignBrief.model_validate_json(product.design_brief)
    assert brief.palette == ["#0B0E14", "#E2A33D"]


async def test_generate_design_brief_task_reports_no_deploy_url(
    session: AsyncSession, tmp_path: Path
) -> None:
    """A product with no `deploy_url` at all -- never got past the planned
    stage -- has nothing to describe."""
    product_id = await _make_product(session, "generate-brief-no-url")
    await session.flush()

    ctx = _ctx(FakeProvider(), workspace_root=tmp_path)

    result = await dev_graph.run(ctx, session, _generate_task(product_id))

    assert "no live deploy_url" in result


# --- POST /dashboard/products/{id}/design-brief/generate: the full endpoint ------------


async def test_generate_endpoint_400s_when_the_product_has_no_deploy_url(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DATABASE_URL", DATABASE_URL or "")
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("DASHBOARD_AUTH_TOKEN", "correct-horse-battery-staple")
    product_id = await _make_product(session, "generate-endpoint-no-url")
    await session.commit()

    async with await _client() as client:
        response = await client.post(
            f"/dashboard/products/{product_id}/design-brief/generate",
            headers={"Authorization": "Bearer correct-horse-battery-staple"},
        )

    assert response.status_code == 400


async def test_generate_endpoint_404s_on_an_unknown_product(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", DATABASE_URL or "")
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("DASHBOARD_AUTH_TOKEN", "correct-horse-battery-staple")

    async with await _client() as client:
        response = await client.post(
            "/dashboard/products/not-a-real-id/design-brief/generate",
            headers={"Authorization": "Bearer correct-horse-battery-staple"},
        )

    assert response.status_code == 404


async def test_generate_endpoint_enqueues_a_task_for_a_live_product(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DATABASE_URL", DATABASE_URL or "")
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("DASHBOARD_AUTH_TOKEN", "correct-horse-battery-staple")
    product_id = await _make_product(
        session, "generate-endpoint-live", deploy_url="https://example-product.test"
    )
    await session.commit()

    async with await _client() as client:
        response = await client.post(
            f"/dashboard/products/{product_id}/design-brief/generate",
            headers={"Authorization": "Bearer correct-horse-battery-staple"},
        )

    assert response.status_code == 200
    assert response.json() == {"status": "queued"}
    tasks = (
        (
            await session.execute(
                select(Task).where(
                    Task.kind == TaskKind.GENERATE_DESIGN_BRIEF.value,
                    Task.product_id == product_id,
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(tasks) == 1


async def test_generate_endpoint_is_idempotent_on_repeat_clicks(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DATABASE_URL", DATABASE_URL or "")
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("DASHBOARD_AUTH_TOKEN", "correct-horse-battery-staple")
    product_id = await _make_product(
        session, "generate-endpoint-repeat", deploy_url="https://example-product.test"
    )
    await session.commit()

    async with await _client() as client:
        headers = {"Authorization": "Bearer correct-horse-battery-staple"}
        first = await client.post(
            f"/dashboard/products/{product_id}/design-brief/generate", headers=headers
        )
        second = await client.post(
            f"/dashboard/products/{product_id}/design-brief/generate", headers=headers
        )

    assert first.status_code == 200
    assert second.status_code == 200
    tasks = (
        (
            await session.execute(
                select(Task).where(
                    Task.kind == TaskKind.GENERATE_DESIGN_BRIEF.value,
                    Task.product_id == product_id,
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(tasks) == 1
