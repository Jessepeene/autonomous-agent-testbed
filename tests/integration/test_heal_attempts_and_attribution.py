"""Heal-attempt audit trail and outreach-to-revenue attribution.

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

from factory.engines.marketing.attribution import reconcile
from factory.state import repository
from factory.state.orm import Base, Lead, OutreachLog
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
    )
    idea_id = await repository.upsert_idea(session, idea)
    assert idea_id is not None
    return await repository.create_product(session, name=name, idea_id=idea_id)


def _lead(email: str) -> Lead:
    return Lead(
        first_name="Ada",
        last_name="Lovelace",
        job_title="Head of Finance",
        company="Analytical Engines BV",
        work_email=email,
        source_url="https://example.test/team",
        scraped_at=datetime(2026, 1, 1, tzinfo=UTC),
        lia_passed=True,
    )


# --- heal_attempts --------------------------------------------------------------


async def test_a_multi_cycle_heal_sequence_leaves_a_correct_audit_trail(
    session: AsyncSession,
) -> None:
    product_id = await _make_product(session, "Reconciliation Assistant")

    # Attempt 1: build fails tests.
    await repository.record_heal_attempt(
        session,
        product_id=product_id,
        task_id=None,
        attempt_number=1,
        healing_text="fix the failing test",
        failed_categories=["test_failure"],
    )
    # Attempt 2 begins: attempt 1 didn't work, resolve it, create a new pending one.
    await repository.resolve_pending_heal_attempts(session, product_id, "failed_again")
    await repository.record_heal_attempt(
        session,
        product_id=product_id,
        task_id=None,
        attempt_number=2,
        healing_text="fix the council rejection",
        failed_categories=["council_rejection:security"],
    )
    # Attempt 2 succeeds.
    await repository.resolve_pending_heal_attempts(session, product_id, "healed")
    await session.commit()

    from sqlalchemy import select

    from factory.state.orm import HealAttempt

    rows = (
        (
            await session.execute(
                select(HealAttempt)
                .where(HealAttempt.product_id == product_id)
                .order_by(HealAttempt.attempt_number)
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 2
    assert rows[0].attempt_number == 1
    assert rows[0].outcome == "failed_again"
    assert rows[0].resolved_at is not None
    assert rows[1].attempt_number == 2
    assert rows[1].outcome == "healed"


async def test_escalation_resolves_the_final_attempt_as_escalated(
    session: AsyncSession,
) -> None:
    product_id = await _make_product(session, "Reconciliation Assistant")

    await repository.record_heal_attempt(
        session,
        product_id=product_id,
        task_id=None,
        attempt_number=3,
        healing_text="last attempt, still failing",
        failed_categories=["deploy_failure"],
    )
    await repository.resolve_pending_heal_attempts(session, product_id, "escalated")
    await session.commit()

    from sqlalchemy import select

    from factory.state.orm import HealAttempt

    row = (
        await session.execute(select(HealAttempt).where(HealAttempt.product_id == product_id))
    ).scalar_one()
    assert row.outcome == "escalated"


# --- outreach attribution --------------------------------------------------------


async def test_a_touch_inside_the_window_is_attributed(session: AsyncSession) -> None:
    product_id = await _make_product(session, "Reconciliation Assistant")
    lead = _lead("ada@example.test")
    session.add(lead)
    await session.flush()

    sent_at = datetime.now(UTC) - timedelta(days=3)
    outreach_id = await repository.record_outreach(
        session,
        lead_id=lead.id,
        subject="A tool for reconciliation",
        status="sent",
        product_id=product_id,
    )
    await session.execute(
        OutreachLog.__table__.update().where(OutreachLog.id == outreach_id).values(sent_at=sent_at)
    )
    recorded = await repository.append_ledger(
        session,
        external_event_id="evt_1",
        kind="revenue",
        amount_cents=4900,
        product_id=product_id,
    )
    assert recorded
    await session.commit()

    report = await reconcile(session)
    await session.commit()

    assert report.checked == 1
    assert report.attributed == 1
    row = await session.get(OutreachLog, outreach_id)
    assert row is not None
    assert row.ledger_entry_id is not None
    assert row.converted_at is not None


async def test_a_touch_outside_the_window_is_not_attributed(session: AsyncSession) -> None:
    product_id = await _make_product(session, "Reconciliation Assistant")
    lead = _lead("ada@example.test")
    session.add(lead)
    await session.flush()

    sent_at = datetime.now(UTC) - timedelta(days=20)  # outside the 14-day window
    outreach_id = await repository.record_outreach(
        session,
        lead_id=lead.id,
        subject="A tool for reconciliation",
        status="sent",
        product_id=product_id,
    )
    await session.execute(
        OutreachLog.__table__.update().where(OutreachLog.id == outreach_id).values(sent_at=sent_at)
    )
    await repository.append_ledger(
        session,
        external_event_id="evt_2",
        kind="revenue",
        amount_cents=4900,
        product_id=product_id,
    )
    await session.commit()

    report = await reconcile(session)

    assert report.checked == 1
    assert report.attributed == 0
    row = await session.get(OutreachLog, outreach_id)
    assert row is not None
    assert row.ledger_entry_id is None


async def test_a_touch_for_a_different_product_is_not_attributed(session: AsyncSession) -> None:
    product_a = await _make_product(session, "Product A")
    product_b = await _make_product(session, "Product B")
    lead = _lead("ada@example.test")
    session.add(lead)
    await session.flush()

    outreach_id = await repository.record_outreach(
        session,
        lead_id=lead.id,
        subject="About Product A",
        status="sent",
        product_id=product_a,
    )
    await repository.append_ledger(
        session,
        external_event_id="evt_3",
        kind="revenue",
        amount_cents=4900,
        product_id=product_b,
    )
    await session.commit()

    report = await reconcile(session)

    assert report.attributed == 0
    row = await session.get(OutreachLog, outreach_id)
    assert row is not None
    assert row.ledger_entry_id is None


async def test_an_unattributed_ledger_entry_is_skipped_without_crashing(
    session: AsyncSession,
) -> None:
    """product_id IS NULL — a revenue event from a product deployed before the
    attribution contract existed. Must be skipped, not crash the reconciliation job."""
    await repository.append_ledger(
        session,
        external_event_id="evt_4",
        kind="revenue",
        amount_cents=4900,
    )
    await session.commit()

    report = await reconcile(session)

    assert report.checked == 0  # the query itself excludes null-product_id rows
    assert report.attributed == 0


async def test_the_most_recent_touch_in_window_wins_not_the_first(
    session: AsyncSession,
) -> None:
    product_id = await _make_product(session, "Reconciliation Assistant")
    lead = _lead("ada@example.test")
    session.add(lead)
    await session.flush()

    early_id = await repository.record_outreach(
        session,
        lead_id=lead.id,
        subject="First touch",
        status="sent",
        product_id=product_id,
    )
    late_id = await repository.record_outreach(
        session,
        lead_id=lead.id,
        subject="Second touch",
        status="sent",
        product_id=product_id,
    )
    await session.execute(
        OutreachLog.__table__.update()
        .where(OutreachLog.id == early_id)
        .values(sent_at=datetime.now(UTC) - timedelta(days=10))
    )
    await session.execute(
        OutreachLog.__table__.update()
        .where(OutreachLog.id == late_id)
        .values(sent_at=datetime.now(UTC) - timedelta(days=2))
    )
    await repository.append_ledger(
        session,
        external_event_id="evt_5",
        kind="revenue",
        amount_cents=4900,
        product_id=product_id,
    )
    await session.commit()

    await reconcile(session)

    late_row = await session.get(OutreachLog, late_id)
    early_row = await session.get(OutreachLog, early_id)
    assert late_row is not None and late_row.ledger_entry_id is not None
    assert early_row is not None and early_row.ledger_entry_id is None


async def test_a_second_revenue_row_for_the_same_product_is_not_reconciled_again(
    session: AsyncSession,
) -> None:
    """Only the first revenue row per product is a candidate — a renewal isn't a new
    'outreach worked' signal."""
    product_id = await _make_product(session, "Reconciliation Assistant")
    lead = _lead("ada@example.test")
    session.add(lead)
    await session.flush()

    outreach_id = await repository.record_outreach(
        session,
        lead_id=lead.id,
        subject="Touch",
        status="sent",
        product_id=product_id,
    )
    await repository.append_ledger(
        session,
        external_event_id="evt_first",
        kind="revenue",
        amount_cents=4900,
        product_id=product_id,
    )
    await session.commit()
    await reconcile(session)
    await session.commit()

    # A renewal a month later.
    await repository.append_ledger(
        session,
        external_event_id="evt_renewal",
        kind="revenue",
        amount_cents=4900,
        product_id=product_id,
    )
    await session.commit()

    report = await reconcile(session)

    assert report.checked == 0  # the first row is already claimed; no new candidates
    row = await session.get(OutreachLog, outreach_id)
    assert row is not None
    assert row.ledger_entry_id is not None
