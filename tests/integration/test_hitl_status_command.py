"""`/status`: a human can ask the factory for a live snapshot over Telegram,
instead of only ever receiving push notifications.

Needs Postgres:

    docker compose up -d postgres
    alembic upgrade head
    pytest tests/integration -q
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from factory.hitl.commands import build_status_report
from factory.hitl.interrupts import collect_replies
from factory.state import repository
from factory.state.orm import Base
from factory.state.schemas import InterruptReason, ProductStatus, ScoredIdea

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


class _ScriptedNotifier:
    def __init__(self, replies: list[str]) -> None:
        self._replies = replies
        self.sent: list[str] = []

    @property
    def available(self) -> bool:
        return True

    async def send(self, text: str) -> bool:
        self.sent.append(text)
        return True

    async def poll_replies(self) -> list[str]:
        replies, self._replies = self._replies, []
        return replies


async def _make_product(
    session: AsyncSession, name: str, status: ProductStatus = ProductStatus.VALIDATING
) -> str:
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
    product_id = await repository.create_product(session, name=name, idea_id=idea_id)
    await repository.set_product_status(session, product_id, status)
    return product_id


# --- build_status_report -------------------------------------------------------------


async def test_reports_no_active_products_and_the_open_interrupt_count(
    session: AsyncSession,
) -> None:
    interrupt_id = await repository.open_interrupt(
        session,
        reason=InterruptReason.MISSING_CREDENTIAL,
        subject="STRIPE_API_KEY missing",
        detail="set it",
    )
    assert interrupt_id is not None
    await session.flush()

    report = await build_status_report(session)

    assert "No active products yet" in report
    assert "Open escalations: 1" in report


async def test_reports_a_products_name_stage_and_numbers(session: AsyncSession) -> None:
    product_id = await _make_product(session, "status-report-product")
    await repository.record_page_view(session, product_id=product_id, path="/")
    await repository.record_page_view(session, product_id=product_id, path="/")
    await repository.insert_validation_signal(
        session, product_id=product_id, email="a@example.test"
    )
    await session.flush()

    report = await build_status_report(session)

    assert "status-report-product" in report
    assert "validating" in report
    assert "views (7d): 2" in report
    assert "signals: 1" in report
    assert "leads: 0" in report


async def test_excludes_retired_products(session: AsyncSession) -> None:
    product_id = await _make_product(session, "status-report-retired")
    retired = await repository.retire_product(session, product_id)
    assert retired is True
    await session.flush()

    report = await build_status_report(session)

    assert "status-report-retired" not in report
    assert "No active products yet" in report


async def test_old_page_views_fall_outside_the_seven_day_window(session: AsyncSession) -> None:
    from sqlalchemy import update

    from factory.state.orm import PageView

    product_id = await _make_product(session, "status-report-stale-views")
    await repository.record_page_view(session, product_id=product_id, path="/")
    await session.execute(
        update(PageView).values(created_at=datetime.now(UTC) - timedelta(days=30))
    )
    await session.flush()

    report = await build_status_report(session)

    assert "views (7d): 0" in report


# --- collect_replies dispatches /status without touching any interrupt --------------


async def test_collect_replies_answers_status_without_resolving_anything(
    session: AsyncSession,
) -> None:
    await _make_product(session, "status-dispatch-product")
    interrupt_id = await repository.open_interrupt(
        session,
        reason=InterruptReason.MISSING_CREDENTIAL,
        subject="unrelated escalation",
        detail="set it",
    )
    assert interrupt_id is not None
    await session.flush()

    notifier = _ScriptedNotifier(["/status"])
    resolved = await collect_replies(session, notifier)

    assert resolved == 0
    assert len(notifier.sent) == 1
    assert "status-dispatch-product" in notifier.sent[0]
    assert "Open escalations: 1" in notifier.sent[0]

    # The unrelated interrupt must still be open -- /status must not resolve anything.
    still_open = await repository.stale_open_interrupts(session, older_than=timedelta(seconds=0))
    assert any(row.id == interrupt_id for row in still_open)
