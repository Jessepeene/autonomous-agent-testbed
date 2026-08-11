"""The spam-complaint circuit breaker, end to end.

Needs Postgres:

    docker compose up -d postgres
    alembic upgrade head
    pytest tests/integration -q
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from factory.engines.marketing import deliverability
from factory.settings import Settings, get_settings
from factory.state import repository
from factory.state.orm import Base, Interrupt, Lead, MarketingFreeze, OutreachLog
from factory.state.schemas import InterruptReason

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


def _sensitive_settings(**overrides: object) -> Settings:
    """A cheap-to-trip breaker so tests don't need hundreds of rows."""
    base = get_settings().model_copy(
        update={"deliverability_min_sample_size": 5, "spam_complaint_rate_threshold": 0.2}
    )
    return base.model_copy(update=overrides) if overrides else base


async def _seed_lead(session: AsyncSession) -> str:
    lead = Lead(
        first_name="Ada",
        last_name="Lovelace",
        job_title="Head of Finance",
        company="Analytical Engines BV",
        work_email="ada@analytical-engines.test",
        source_url="https://example.test/team",
        scraped_at=datetime(2026, 1, 1, tzinfo=UTC),
        lia_passed=True,
    )
    session.add(lead)
    await session.flush()
    return lead.id


async def _seed_sent(session: AsyncSession, lead_id: str, count: int) -> list[str]:
    ids = []
    for _ in range(count):
        outreach_id = await repository.record_outreach(
            session, lead_id=lead_id, subject="hi", status="sent"
        )
        ids.append(outreach_id)
    return ids


async def test_circuit_breaker_trips_when_the_threshold_is_crossed(session: AsyncSession) -> None:
    settings = _sensitive_settings()  # min_sample=5, threshold=20%
    lead_id = await _seed_lead(session)
    sent_ids = await _seed_sent(session, lead_id, 5)
    await session.commit()

    # 1/5 = 20% >= 20% threshold on the last complaint.
    outcome = await deliverability.process_event(
        session,
        {"event-data": {"event": "complained", "user-variables": {"outreach_log_id": sent_ids[0]}}},
        settings=settings,
    )
    await session.commit()

    assert outcome.handled is True
    freeze = await repository.active_marketing_freeze(session)
    assert freeze is not None
    assert freeze.interrupt_id is not None

    interrupt = await session.get(Interrupt, freeze.interrupt_id)
    assert interrupt is not None
    assert interrupt.reason == "deliverability_frozen"


async def test_circuit_breaker_does_not_trip_below_the_minimum_sample_size(
    session: AsyncSession,
) -> None:
    settings = _sensitive_settings()  # min_sample=5
    lead_id = await _seed_lead(session)
    sent_ids = await _seed_sent(session, lead_id, 2)  # below the min sample size
    await session.commit()

    await deliverability.process_event(
        session,
        {"event-data": {"event": "complained", "user-variables": {"outreach_log_id": sent_ids[0]}}},
        settings=settings,
    )
    await session.commit()

    assert await repository.active_marketing_freeze(session) is None


async def test_a_second_complaint_does_not_duplicate_the_freeze(session: AsyncSession) -> None:
    settings = _sensitive_settings()
    lead_id = await _seed_lead(session)
    sent_ids = await _seed_sent(session, lead_id, 5)
    await session.commit()

    for outreach_id in sent_ids[:2]:
        await deliverability.process_event(
            session,
            {
                "event-data": {
                    "event": "complained",
                    "user-variables": {"outreach_log_id": outreach_id},
                }
            },
            settings=settings,
        )
        await session.commit()

    from sqlalchemy import func, select

    count = (await session.execute(select(func.count()).select_from(MarketingFreeze))).scalar_one()
    assert count == 1


async def test_a_second_freeze_while_one_is_active_is_a_clean_no_op(
    session: AsyncSession,
) -> None:
    """Regression test: `index_where`'s ORM comparison used to compile to a bound
    parameter, which Postgres's ON CONFLICT inference never matches against the
    partial index's own literal predicate -- every second call raised
    `InvalidColumnReferenceError` instead of returning `None` as the docstring
    always claimed. Never caught before because no test actually called this
    function twice."""
    first = await repository.create_marketing_freeze(
        session, reason="first freeze", complaint_rate=0.5
    )
    assert first is not None

    second = await repository.create_marketing_freeze(
        session, reason="second freeze", complaint_rate=0.6
    )

    assert second is None


async def test_ensure_not_frozen_raises_while_a_freeze_is_active(session: AsyncSession) -> None:
    await repository.create_marketing_freeze(session, reason="test freeze", complaint_rate=0.5)
    await session.commit()

    with pytest.raises(deliverability.MarketingFrozen):
        await deliverability.ensure_not_frozen(session)


async def test_ensure_not_frozen_passes_when_nothing_is_active(session: AsyncSession) -> None:
    await deliverability.ensure_not_frozen(session)  # must not raise


async def test_a_resume_reply_clears_the_freeze(session: AsyncSession) -> None:
    freeze = await repository.create_marketing_freeze(
        session, reason="test freeze", complaint_rate=0.5
    )
    assert freeze is not None
    interrupt_id = await repository.open_interrupt(
        session,
        reason=InterruptReason.DELIVERABILITY_FROZEN,
        subject="Marketing frozen: spam complaint rate exceeded",
        detail="test",
    )
    assert interrupt_id is not None
    await repository.set_marketing_freeze_interrupt(session, freeze.id, interrupt_id)
    await repository.resolve_interrupt(session, interrupt_id, "RESUME")
    await session.commit()

    synced = await deliverability.sync_freeze_clearance(session)
    await session.commit()

    assert synced == 1
    assert await repository.active_marketing_freeze(session) is None


async def test_an_ambiguous_reply_stays_frozen_and_reissues_a_fresh_interrupt(
    session: AsyncSession,
) -> None:
    freeze = await repository.create_marketing_freeze(
        session, reason="test freeze", complaint_rate=0.5
    )
    assert freeze is not None
    interrupt_id = await repository.open_interrupt(
        session,
        reason=InterruptReason.DELIVERABILITY_FROZEN,
        subject="Marketing frozen: spam complaint rate exceeded",
        detail="test",
    )
    assert interrupt_id is not None
    await repository.set_marketing_freeze_interrupt(session, freeze.id, interrupt_id)
    await repository.resolve_interrupt(session, interrupt_id, "why is this frozen?")
    await session.commit()

    synced = await deliverability.sync_freeze_clearance(session)
    await session.commit()

    assert synced == 0
    still_active = await repository.active_marketing_freeze(session)
    assert still_active is not None
    # A fresh, open interrupt now exists so the operator can still reply RESUME.
    assert still_active.interrupt_id != interrupt_id
    new_interrupt = await session.get(Interrupt, still_active.interrupt_id)
    assert new_interrupt is not None
    assert new_interrupt.status == "open"


async def test_process_event_records_a_bounce(session: AsyncSession) -> None:
    lead_id = await _seed_lead(session)
    sent_ids = await _seed_sent(session, lead_id, 1)
    await session.commit()

    outcome = await deliverability.process_event(
        session,
        {"event-data": {"event": "failed", "user-variables": {"outreach_log_id": sent_ids[0]}}},
    )
    await session.commit()

    assert outcome.handled is True
    row = await session.get(OutreachLog, sent_ids[0])
    assert row is not None
    assert row.bounced_at is not None


# --- bounce circuit breaker: 2026-08-11 audit fix -------------------------------
#
# bounced_at was recorded on every bounce webhook but never read by anything --
# there was no backstop at all for a sustained high bounce rate, unlike the
# complaint-rate circuit breaker right above.


async def test_bounce_circuit_breaker_trips_when_the_threshold_is_crossed(
    session: AsyncSession,
) -> None:
    settings = _sensitive_settings(bounce_rate_threshold=0.2)
    lead_id = await _seed_lead(session)
    sent_ids = await _seed_sent(session, lead_id, 5)
    await session.commit()

    # 1/5 = 20% >= 20% threshold on the last bounce.
    outcome = await deliverability.process_event(
        session,
        {"event-data": {"event": "failed", "user-variables": {"outreach_log_id": sent_ids[0]}}},
        settings=settings,
    )
    await session.commit()

    assert outcome.handled is True
    freeze = await repository.active_marketing_freeze(session)
    assert freeze is not None
    assert freeze.interrupt_id is not None

    interrupt = await session.get(Interrupt, freeze.interrupt_id)
    assert interrupt is not None
    assert interrupt.reason == "deliverability_frozen"


async def test_bounce_circuit_breaker_does_not_trip_below_the_minimum_sample_size(
    session: AsyncSession,
) -> None:
    settings = _sensitive_settings(bounce_rate_threshold=0.2)
    lead_id = await _seed_lead(session)
    sent_ids = await _seed_sent(session, lead_id, 2)  # below the min sample size
    await session.commit()

    await deliverability.process_event(
        session,
        {"event-data": {"event": "failed", "user-variables": {"outreach_log_id": sent_ids[0]}}},
        settings=settings,
    )
    await session.commit()

    assert await repository.active_marketing_freeze(session) is None


async def test_a_low_complaint_rate_does_not_mask_a_high_bounce_rate(
    session: AsyncSession,
) -> None:
    """The two thresholds are independent -- a comfortably-low complaint rate must
    not prevent the bounce rate from tripping the same breaker."""
    settings = _sensitive_settings(bounce_rate_threshold=0.2)
    lead_id = await _seed_lead(session)
    sent_ids = await _seed_sent(session, lead_id, 5)
    await session.commit()

    outcome = await deliverability.process_event(
        session,
        {"event-data": {"event": "bounced", "user-variables": {"outreach_log_id": sent_ids[0]}}},
        settings=settings,
    )
    await session.commit()

    assert outcome.handled is True
    freeze = await repository.active_marketing_freeze(session)
    assert freeze is not None
    assert "Bounce rate" in freeze.reason
