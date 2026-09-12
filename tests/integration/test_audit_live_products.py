"""The `audit-live-products` maintenance item is fully deterministic for the
uptime question now -- a real HTTP check against `Product.deploy_url`, not an
AUDITOR agent investigating and having its free-text answer string-matched
(Tier 0 finding #10, system-wide verification plan). The one fetch also backs
a separate content-freshness check (Tier 0 finding #0b): nothing else ever
looks at a LIVE product's actual content again after it ships, so a page that
was correct at launch (no stale year, no placeholder text) can go stale with
nothing ever noticing. See `maintenance.py::_audit_live_products`/
`check_deploy_url`/`_content_freshness_issue`.

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

from factory.engines import maintenance
from factory.hitl.telegram import NullNotifier
from factory.llm.client import LLMClient
from factory.llm.providers.base import Provider
from factory.llm.providers.fake import FakeProvider
from factory.llm.router import ProviderRegistry
from factory.sandbox.disabled import DisabledSandbox
from factory.settings import EngineName, Settings
from factory.state import repository
from factory.state.orm import Base, Task
from factory.state.schemas import (
    ProductStatus,
    ReviewCategory,
    ReviewFinding,
    ReviewVerdict,
    ScoredIdea,
    TaskKind,
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


def _ctx(*, provider: FakeProvider, tmp_path: Path) -> FactoryContext:
    settings = Settings(
        database_url=DATABASE_URL, anthropic_api_key="test-key", workspace_root=tmp_path
    )
    llm = LLMClient(ProviderRegistry({Provider.ANTHROPIC: provider}), settings=settings)
    return FactoryContext(
        settings=settings,
        llm=llm,
        notifier=NullNotifier(),
        sandbox=DisabledSandbox(),
        enabled_engines={EngineName.DEVELOPMENT},
        worker_id="audit-live-products-test",
    )


def _approving_provider() -> FakeProvider:
    provider = FakeProvider(name=Provider.ANTHROPIC)
    provider.register(
        ReviewVerdict, ReviewVerdict(category=ReviewCategory.QUALITY, approved=True, findings=[])
    )
    return provider


async def _make_live_product(session: AsyncSession, *, name: str, deploy_url: str | None) -> str:
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
    product_id = await repository.create_product(session, name=name, idea_id=idea_id)
    await repository.set_product_status(session, product_id, ProductStatus.LIVE)
    if deploy_url is not None:
        product = await repository.get_product(session, product_id)
        assert product is not None
        product.deploy_url = deploy_url
        await session.flush()
    return product_id


# --- uptime check (finding #10) -------------------------------------------------


async def test_a_healthy_product_gets_no_patch_task(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    async def fake_get(self: httpx.AsyncClient, url: str, **kwargs: object) -> httpx.Response:
        return httpx.Response(
            200, text="<html>a normal homepage</html>", request=httpx.Request("GET", url)
        )

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    product_id = await _make_live_product(
        session, name="healthy-product", deploy_url="https://healthy.example.test"
    )
    await session.commit()
    ctx = _ctx(provider=_approving_provider(), tmp_path=tmp_path)

    summary = await maintenance._audit_live_products(ctx, session)
    await session.commit()

    assert "1 checked, 0 degraded" in summary
    tasks = (
        (await session.execute(select(Task).where(Task.product_id == product_id))).scalars().all()
    )
    assert not any(t.kind == TaskKind.PATCH_PRODUCT.value for t in tasks)


async def test_a_5xx_response_queues_a_real_patch_task_without_needing_the_llm(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A degraded uptime result must short-circuit before the content check --
    there is no page body worth reviewing for a 503, and no LLM call should
    even be attempted (no ReviewVerdict is registered on this provider at all)."""

    async def fake_get(self: httpx.AsyncClient, url: str, **kwargs: object) -> httpx.Response:
        return httpx.Response(503, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    product_id = await _make_live_product(
        session, name="degraded-product", deploy_url="https://degraded.example.test"
    )
    await session.commit()
    ctx = _ctx(provider=FakeProvider(name=Provider.ANTHROPIC), tmp_path=tmp_path)

    summary = await maintenance._audit_live_products(ctx, session)
    await session.commit()

    assert "1 checked, 1 degraded" in summary
    tasks = (
        (await session.execute(select(Task).where(Task.product_id == product_id))).scalars().all()
    )
    patch_tasks = [t for t in tasks if t.kind == TaskKind.PATCH_PRODUCT.value]
    assert len(patch_tasks) == 1
    assert "503" in patch_tasks[0].payload["healing"]


async def test_a_connection_failure_is_treated_as_degraded(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    async def fake_get(self: httpx.AsyncClient, url: str, **kwargs: object) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    product_id = await _make_live_product(
        session, name="unreachable-product", deploy_url="https://unreachable.example.test"
    )
    await session.commit()
    ctx = _ctx(provider=FakeProvider(name=Provider.ANTHROPIC), tmp_path=tmp_path)

    summary = await maintenance._audit_live_products(ctx, session)
    await session.commit()

    assert "1 checked, 1 degraded" in summary
    tasks = (
        (await session.execute(select(Task).where(Task.product_id == product_id))).scalars().all()
    )
    assert any(t.kind == TaskKind.PATCH_PRODUCT.value for t in tasks)


async def test_a_product_with_no_deploy_url_is_skipped_not_flagged(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    called = False

    async def fake_get(self: httpx.AsyncClient, url: str, **kwargs: object) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    await _make_live_product(session, name="no-url-product", deploy_url=None)
    await session.commit()
    ctx = _ctx(provider=_approving_provider(), tmp_path=tmp_path)

    summary = await maintenance._audit_live_products(ctx, session)

    assert "0 checked" in summary
    assert called is False


# --- content-freshness check (finding #0b) --------------------------------------


async def test_a_page_the_quality_reviewer_rejects_queues_a_patch_task(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A product can be fully up (200 OK) and still be flagged -- the uptime
    check and the content-freshness check are independent signals."""

    async def fake_get(self: httpx.AsyncClient, url: str, **kwargs: object) -> httpx.Response:
        return httpx.Response(
            200, text="<footer>© 2024 Acme</footer>", request=httpx.Request("GET", url)
        )

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    provider = FakeProvider(name=Provider.ANTHROPIC)
    provider.register(
        ReviewVerdict,
        ReviewVerdict(
            category=ReviewCategory.QUALITY,
            approved=False,
            findings=[
                ReviewFinding(
                    category=ReviewCategory.QUALITY,
                    severity="medium",
                    file="live homepage",
                    summary="Hardcoded stale copyright year in the footer",
                    failure_scenario="The year never advances, embarrassing the business.",
                    remediation="Compute the year at render time instead.",
                )
            ],
        ),
    )
    product_id = await _make_live_product(
        session, name="stale-product", deploy_url="https://stale.example.test"
    )
    await session.commit()
    ctx = _ctx(provider=provider, tmp_path=tmp_path)

    summary = await maintenance._audit_live_products(ctx, session)
    await session.commit()

    assert "1 checked, 1 degraded" in summary
    tasks = (
        (await session.execute(select(Task).where(Task.product_id == product_id))).scalars().all()
    )
    patch_tasks = [t for t in tasks if t.kind == TaskKind.PATCH_PRODUCT.value]
    assert len(patch_tasks) == 1
    assert "stale copyright year" in patch_tasks[0].payload["healing"]


async def test_an_empty_body_skips_the_content_check_without_erroring(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    async def fake_get(self: httpx.AsyncClient, url: str, **kwargs: object) -> httpx.Response:
        return httpx.Response(200, text="", request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    product_id = await _make_live_product(
        session, name="empty-body-product", deploy_url="https://empty.example.test"
    )
    await session.commit()
    # No ReviewVerdict registered: if the empty body were sent for review, the
    # FakeProvider would raise "no queued response" and fail this test.
    ctx = _ctx(provider=FakeProvider(name=Provider.ANTHROPIC), tmp_path=tmp_path)

    summary = await maintenance._audit_live_products(ctx, session)
    await session.commit()

    assert "1 checked, 0 degraded" in summary
    tasks = (
        (await session.execute(select(Task).where(Task.product_id == product_id))).scalars().all()
    )
    assert not any(t.kind == TaskKind.PATCH_PRODUCT.value for t in tasks)
