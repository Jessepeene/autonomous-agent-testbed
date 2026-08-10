"""Stripe webhook product attribution — needs Postgres for the ledger table.

docker compose up -d postgres
alembic upgrade head
pytest tests/integration -q
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from factory.engines.finance.webhooks import process_event
from factory.state import repository
from factory.state.orm import Base, LedgerEntry
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


async def _make_product(session: AsyncSession, name: str) -> str:
    idea = ScoredIdea(
        problem_statement="Bookkeepers reconcile invoices by hand every week.",
        target_niche=name,
        source_url="https://example.test/thread",
        monetization_hypothesis="Bookkeepers pay EUR 49/month per seat.",
        pain_severity=0.8,
        willingness_to_pay=0.7,
        build_feasibility=0.6,
        adoption_ease=0.8,
    )
    idea_id = await repository.upsert_idea(session, idea)
    assert idea_id is not None
    return await repository.create_product(session, name=name, idea_id=idea_id)


def _revenue_event(event_id: str, *, client_reference_id: str | None = None) -> dict:
    obj: dict = {"amount_total": 4900}
    if client_reference_id is not None:
        obj["client_reference_id"] = client_reference_id
    return {"id": event_id, "type": "checkout.session.completed", "data": {"object": obj}}


async def test_revenue_is_attributed_to_the_product_that_generated_it(
    session: AsyncSession,
) -> None:
    # `client_reference_id` is a real FK (`ledger.product_id -> products.id`) in
    # production it is always the id of the product that generated the Checkout
    # Session (see `deploy.py`'s env injection), so the fixture must satisfy the
    # same constraint rather than use an arbitrary string.
    product_id = await _make_product(session, "Reconciliation Assistant")
    await session.commit()

    outcome = await process_event(session, _revenue_event("evt_1", client_reference_id=product_id))
    await session.commit()

    assert outcome.handled is True
    row = (await session.execute(select(LedgerEntry))).scalar_one()
    assert row.product_id == product_id


async def test_revenue_with_no_attributable_product_is_still_recorded(
    session: AsyncSession,
) -> None:
    """A product deployed before this contract existed must never lose revenue just
    because it cannot be attributed."""
    outcome = await process_event(session, _revenue_event("evt_2"))
    await session.commit()

    assert outcome.handled is True
    row = (await session.execute(select(LedgerEntry))).scalar_one()
    assert row.product_id is None
    assert row.amount_cents == 4900
