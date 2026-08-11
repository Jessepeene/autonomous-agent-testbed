"""The self-hosted analytics beacon: POST /analytics/{product_id} -> page_views.

Nothing collected traffic data for a live or validating product before this --
`ValidationSignal` records interest (an email), not visits. A third-party
analytics provider's query API contract can't be verified from inside this
codebase, so this reuses the pattern already established for
`/interest/{product_id}` -> `validation_signals` instead: an unauthenticated
public beacon endpoint the product's own generated frontend calls, landing in a
table this factory fully controls.

Needs Postgres:

    docker compose up -d postgres
    alembic upgrade head
    pytest tests/integration -q
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from factory.state import repository
from factory.state.orm import Base
from factory.state.schemas import ScoredIdea

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
    """The /analytics endpoint opens sessions through the process-wide cached
    engine, a different connection than this file's own `session` fixture -- see
    test_validation_gate.py's identical fixture for why this reset matters."""
    yield
    from factory.state import db as db_module

    await db_module.dispose_engine()


async def _client() -> httpx.AsyncClient:
    from factory.api.app import app

    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def _make_product(session: AsyncSession, niche: str) -> str:
    idea = ScoredIdea(
        problem_statement="Bookkeepers reconcile invoices by hand every week.",
        target_niche=niche,
        source_url="https://example.test/thread",
        monetization_hypothesis="Bookkeepers pay EUR 49/month per seat.",
        pain_severity=0.8,
        willingness_to_pay=0.7,
        build_feasibility=0.6,
        adoption_ease=0.8,
    )
    idea_id = await repository.upsert_idea(session, idea)
    assert idea_id is not None
    return await repository.create_product(session, name=niche, idea_id=idea_id)


async def test_a_view_is_recorded(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", DATABASE_URL or "")
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    product_id = await _make_product(session, "analytics-basic")
    await session.commit()

    async with await _client() as client:
        response = await client.post(
            f"/analytics/{product_id}", json={"path": "/", "referrer": "https://google.test"}
        )

    assert response.status_code == 200
    assert response.json() == {"received": True}
    assert await repository.tally_page_views(session, product_id) == 1


async def test_repeat_views_are_not_deduplicated(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unlike /interest, a page view is anonymous -- there is no identity to dedup
    on, and every real visit is a real signal worth counting."""
    monkeypatch.setenv("DATABASE_URL", DATABASE_URL or "")
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    product_id = await _make_product(session, "analytics-repeat")
    await session.commit()

    async with await _client() as client:
        for _ in range(3):
            response = await client.post(f"/analytics/{product_id}", json={"path": "/"})
            assert response.status_code == 200

    assert await repository.tally_page_views(session, product_id) == 3


async def test_referrer_and_variant_are_optional(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DATABASE_URL", DATABASE_URL or "")
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    product_id = await _make_product(session, "analytics-minimal")
    await session.commit()

    async with await _client() as client:
        response = await client.post(f"/analytics/{product_id}", json={"path": "/pricing"})

    assert response.status_code == 200
    assert await repository.tally_page_views(session, product_id) == 1


async def test_404s_on_an_unknown_product(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", DATABASE_URL or "")
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")

    async with await _client() as client:
        response = await client.post("/analytics/does-not-exist", json={"path": "/"})

    assert response.status_code == 404


async def test_rejects_a_missing_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", DATABASE_URL or "")
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")

    async with await _client() as client:
        response = await client.post("/analytics/some-product-id", json={})

    assert response.status_code == 422


async def test_rejects_an_oversized_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", DATABASE_URL or "")
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")

    async with await _client() as client:
        response = await client.post("/analytics/some-product-id", json={"path": "/" + "a" * 500})

    assert response.status_code == 422


async def test_tally_can_be_scoped_by_time_window(session: AsyncSession) -> None:
    product_id = await _make_product(session, "analytics-window")
    await repository.record_page_view(session, product_id=product_id, path="/old")
    await session.flush()
    # Backdate this row to simulate a view from well before the query window.
    from sqlalchemy import update

    from factory.state.orm import PageView

    await session.execute(
        update(PageView)
        .where(PageView.product_id == product_id)
        .values(created_at=datetime.now(UTC) - timedelta(days=10))
    )
    await repository.record_page_view(session, product_id=product_id, path="/new")

    assert await repository.tally_page_views(session, product_id) == 2
    recent = await repository.tally_page_views(
        session, product_id, since=datetime.now(UTC) - timedelta(days=1)
    )
    assert recent == 1


async def test_tally_can_be_scoped_by_variant(session: AsyncSession) -> None:
    product_id = await _make_product(session, "analytics-variant")
    await repository.record_page_view(session, product_id=product_id, path="/", variant="a")
    await repository.record_page_view(session, product_id=product_id, path="/", variant="a")
    await repository.record_page_view(session, product_id=product_id, path="/", variant="b")

    assert await repository.tally_page_views(session, product_id, variant="a") == 2
    assert await repository.tally_page_views(session, product_id, variant="b") == 1
    assert await repository.tally_page_views(session, product_id) == 3


async def test_a_view_for_one_product_is_never_counted_for_another(
    session: AsyncSession,
) -> None:
    product_a = await _make_product(session, "analytics-scope-a")
    product_b = await _make_product(session, "analytics-scope-b")
    await repository.record_page_view(session, product_id=product_a, path="/")

    assert await repository.tally_page_views(session, product_a) == 1
    assert await repository.tally_page_views(session, product_b) == 0
