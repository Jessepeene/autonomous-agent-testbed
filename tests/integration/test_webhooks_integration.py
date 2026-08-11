"""Stripe webhook product attribution — needs Postgres for the ledger table.

docker compose up -d postgres
alembic upgrade head
pytest tests/integration -q
"""

from __future__ import annotations

import asyncio
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


# --- regression coverage: 2026-08-10 full-codebase audit -----------------------


async def test_invoice_payment_succeeded_is_not_double_booked_against_invoice_paid(
    session: AsyncSession,
) -> None:
    """Stripe fires both `invoice.paid` and `invoice.payment_succeeded` for the same
    successful invoice payment -- distinct event ids, same money. Only one of the
    two may be in REVENUE_EVENTS or every invoice payment is recorded twice."""
    event = {
        "id": "evt_invoice_paid",
        "type": "invoice.paid",
        "data": {"object": {"amount_paid": 4900}},
    }
    duplicate_signal = {
        "id": "evt_invoice_payment_succeeded",
        "type": "invoice.payment_succeeded",
        "data": {"object": {"amount_paid": 4900}},
    }

    first = await process_event(session, event)
    second = await process_event(session, duplicate_signal)
    await session.commit()

    assert first.handled is True
    assert second.handled is False
    rows = (await session.execute(select(LedgerEntry))).scalars().all()
    assert len(rows) == 1
    assert rows[0].amount_cents == 4900


async def test_payment_intent_succeeded_is_not_double_booked_against_checkout_completed(
    session: AsyncSession,
) -> None:
    checkout = _revenue_event("evt_checkout")
    payment_intent_echo = {
        "id": "evt_payment_intent",
        "type": "payment_intent.succeeded",
        "data": {"object": {"amount_received": 4900}},
    }

    first = await process_event(session, checkout)
    second = await process_event(session, payment_intent_echo)
    await session.commit()

    assert first.handled is True
    assert second.handled is False
    rows = (await session.execute(select(LedgerEntry))).scalars().all()
    assert len(rows) == 1


async def test_checkout_completed_with_unsettled_async_payment_is_not_booked(
    session: AsyncSession,
) -> None:
    """A SEPA/ACH Checkout session completes with payment_status="unpaid" -- the
    money has not arrived and may still fail. Booking here would record revenue
    that might never materialise."""
    event = {
        "id": "evt_async_pending",
        "type": "checkout.session.completed",
        "data": {"object": {"amount_total": 4900, "payment_status": "unpaid"}},
    }

    outcome = await process_event(session, event)
    await session.commit()

    assert outcome.handled is False
    rows = (await session.execute(select(LedgerEntry))).scalars().all()
    assert rows == []


async def test_async_payment_succeeded_books_revenue_once_it_actually_settles(
    session: AsyncSession,
) -> None:
    event = {
        "id": "evt_async_settled",
        "type": "checkout.session.async_payment_succeeded",
        "data": {"object": {"amount_total": 4900, "payment_status": "paid"}},
    }

    outcome = await process_event(session, event)
    await session.commit()

    assert outcome.handled is True
    row = (await session.execute(select(LedgerEntry))).scalar_one()
    assert row.amount_cents == 4900


def _refund_event(event_id: str, *, cumulative: int, this_refund: int) -> dict:
    return {
        "id": event_id,
        "type": "charge.refunded",
        "data": {
            "object": {
                "amount_refunded": cumulative,
                "refunds": {"data": [{"amount": this_refund}]},
            }
        },
    }


async def test_partial_refunds_net_by_increment_not_cumulative_total(
    session: AsyncSession,
) -> None:
    """A $100 charge refunded in two parts ($30 then the remaining $70) must clawback
    $100 total, not $30 + $100 = $130 (the naive cumulative-field reading, which
    would drive the balance negative and raise on a charge this small)."""
    revenue = await process_event(
        session,
        {
            "id": "evt_charge_100",
            "type": "checkout.session.completed",
            "data": {"object": {"amount_total": 10000}},
        },
    )
    assert revenue.handled is True
    await session.commit()

    first_refund = await process_event(
        session, _refund_event("evt_refund_1", cumulative=3000, this_refund=3000)
    )
    second_refund = await process_event(
        session, _refund_event("evt_refund_2", cumulative=10000, this_refund=7000)
    )
    await session.commit()

    assert first_refund.handled is True
    assert first_refund.amount_cents == 3000
    assert second_refund.handled is True
    assert second_refund.amount_cents == 7000

    balance, _ = await repository.current_balance(session)
    rows = (await session.execute(select(LedgerEntry))).scalars().all()
    refund_rows = [r for r in rows if r.kind == "refund"]
    assert sum(-r.amount_cents for r in refund_rows) == 10000
    assert balance == 0


async def test_won_dispute_reverses_the_original_debit(session: AsyncSession) -> None:
    revenue = await process_event(session, _revenue_event("evt_rev_for_dispute"))
    assert revenue.handled is True
    await session.commit()

    created = await process_event(
        session,
        {
            "id": "evt_dispute_created",
            "type": "charge.dispute.created",
            "data": {"object": {"amount": 4900, "status": "needs_response"}},
        },
    )
    assert created.handled is True
    await session.commit()

    balance_after_dispute, _ = await repository.current_balance(session)
    assert balance_after_dispute == 0

    won = await process_event(
        session,
        {
            "id": "evt_dispute_closed",
            "type": "charge.dispute.closed",
            "data": {"object": {"amount": 4900, "status": "won"}},
        },
    )
    await session.commit()

    assert won.handled is True
    balance_after_win, _ = await repository.current_balance(session)
    assert balance_after_win == 4900


async def test_lost_dispute_closed_event_is_not_treated_as_a_reversal(
    session: AsyncSession,
) -> None:
    outcome = await process_event(
        session,
        {
            "id": "evt_dispute_lost",
            "type": "charge.dispute.closed",
            "data": {"object": {"amount": 4900, "status": "lost"}},
        },
    )
    await session.commit()

    assert outcome.handled is False
    rows = (await session.execute(select(LedgerEntry))).scalars().all()
    assert rows == []


async def test_refund_exceeding_balance_is_declined_not_raised(session: AsyncSession) -> None:
    """A refund for a charge whose original payment was never recorded must not
    crash the webhook endpoint -- that leads to Stripe retrying, exhausting
    retries, and auto-disabling the endpoint, silently stopping all future revenue
    recording."""
    outcome = await process_event(
        session, _refund_event("evt_orphan_refund", cumulative=9900, this_refund=9900)
    )
    await session.commit()

    assert outcome.handled is False
    assert "ledger rejected" in outcome.detail
    rows = (await session.execute(select(LedgerEntry))).scalars().all()
    assert rows == []


async def test_currency_is_recorded_from_the_event_not_hardcoded_usd(
    session: AsyncSession,
) -> None:
    event = {
        "id": "evt_eur",
        "type": "checkout.session.completed",
        "data": {"object": {"amount_total": 4900, "currency": "eur"}},
    }
    outcome = await process_event(session, event)
    await session.commit()

    assert outcome.handled is True
    row = (await session.execute(select(LedgerEntry))).scalar_one()
    assert row.currency == "EUR"


async def test_concurrent_ledger_appends_do_not_lose_an_event() -> None:
    """Two webhook deliveries handled concurrently must not both read the same
    stale balance and silently drop one event's contribution -- the exact race the
    advisory lock in `append_ledger` exists to serialize."""
    engine = create_async_engine(DATABASE_URL or "", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)

    async def _append(event_id: str, amount: int) -> None:
        async with maker() as db:
            await repository.append_ledger(
                db, external_event_id=event_id, kind="revenue", amount_cents=amount
            )
            await db.commit()

    await asyncio.gather(_append("evt_concurrent_a", 4900), _append("evt_concurrent_b", 4900))

    async with maker() as db:
        balance, _ = await repository.current_balance(db)
    await engine.dispose()

    assert balance == 9800
