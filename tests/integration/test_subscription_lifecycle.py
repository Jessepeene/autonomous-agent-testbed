"""Subscription lifecycle, dunning, and the append_ledger invariant.

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
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from factory.engines.finance.webhooks import process_event
from factory.state import repository
from factory.state.orm import Base, Customer, DunningEvent, LedgerEntry, Subscription
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


def _subscription_event(
    event_id: str,
    event_type: str,
    *,
    stripe_subscription_id: str = "sub_1",
    stripe_customer_id: str = "cus_1",
    product_id: str | None = None,
    status: str = "active",
    price_cents: int = 4900,
    cancel_at_period_end: bool = False,
    cancellation_reason: str | None = None,
) -> dict:
    obj: dict = {
        "id": stripe_subscription_id,
        "customer": stripe_customer_id,
        "status": status,
        "cancel_at_period_end": cancel_at_period_end,
        "current_period_end": int((datetime.now(UTC) + timedelta(days=30)).timestamp()),
        "items": {"data": [{"price": {"unit_amount": price_cents}, "quantity": 1}]},
    }
    if product_id is not None:
        obj["metadata"] = {"product_id": product_id}
    if cancellation_reason is not None:
        obj["cancellation_details"] = {"reason": cancellation_reason}
    return {"id": event_id, "type": event_type, "data": {"object": obj}}


def _invoice_failed_event(
    event_id: str,
    *,
    stripe_invoice_id: str,
    stripe_subscription_id: str = "sub_1",
    stripe_customer_id: str = "cus_1",
) -> dict:
    obj = {
        "id": stripe_invoice_id,
        "subscription": stripe_subscription_id,
        "customer": stripe_customer_id,
    }
    return {"id": event_id, "type": "invoice.payment_failed", "data": {"object": obj}}


def _invoice_paid_event(
    event_id: str,
    *,
    stripe_invoice_id: str,
    stripe_subscription_id: str = "sub_1",
    stripe_customer_id: str = "cus_1",
    amount_paid: int = 4900,
) -> dict:
    obj = {
        "id": stripe_invoice_id,
        "subscription": stripe_subscription_id,
        "customer": stripe_customer_id,
        "amount_paid": amount_paid,
    }
    return {"id": event_id, "type": "invoice.paid", "data": {"object": obj}}


# --- customer.subscription.created / .updated -------------------------------------


async def test_subscription_created_upserts_customer_and_subscription(
    session: AsyncSession,
) -> None:
    product_id = await _make_product(session, "sub-created")
    await session.commit()

    outcome = await process_event(
        session,
        _subscription_event("evt_1", "customer.subscription.created", product_id=product_id),
    )
    await session.commit()

    assert outcome.handled is True
    row = await repository.get_subscription_by_stripe_id(session, "sub_1")
    assert row is not None
    assert row.status == "active"
    assert row.price_cents == 4900
    assert row.product_id == product_id
    assert row.churn_kind is None

    customer = (
        await session.execute(select(Customer).where(Customer.stripe_customer_id == "cus_1"))
    ).scalar_one_or_none()
    assert customer is not None


async def test_subscription_updated_refreshes_status_and_price(
    session: AsyncSession,
) -> None:
    """Not incidental: without this, a plan change or any other update reason
    would leave mrr_cents silently stale."""
    product_id = await _make_product(session, "sub-updated")
    await process_event(
        session,
        _subscription_event(
            "evt_1", "customer.subscription.created", product_id=product_id, price_cents=4900
        ),
    )
    await session.commit()

    await process_event(
        session,
        _subscription_event(
            "evt_2",
            "customer.subscription.updated",
            product_id=product_id,
            price_cents=9900,
            status="active",
        ),
    )
    await session.commit()

    row = await repository.get_subscription_by_stripe_id(session, "sub_1")
    assert row is not None
    assert row.price_cents == 9900


async def test_updated_with_cancel_at_period_end_does_not_set_churn_kind(
    session: AsyncSession,
) -> None:
    """cancel_at_period_end=true only *schedules* a future cancellation --
    Stripe still allows reversing it before the period ends. Treating this as
    churn already would be wrong, not just premature."""
    product_id = await _make_product(session, "sub-scheduled-cancel")
    await process_event(
        session,
        _subscription_event("evt_1", "customer.subscription.created", product_id=product_id),
    )
    await session.commit()

    await process_event(
        session,
        _subscription_event(
            "evt_2",
            "customer.subscription.updated",
            product_id=product_id,
            cancel_at_period_end=True,
        ),
    )
    await session.commit()

    row = await repository.get_subscription_by_stripe_id(session, "sub_1")
    assert row is not None
    assert row.cancel_at_period_end is True
    assert row.churn_kind is None
    assert row.churned_at is None


# --- customer.subscription.deleted: the only churn_kind write path ----------------


async def test_deleted_with_payment_failed_reason_is_involuntary_churn(
    session: AsyncSession,
) -> None:
    product_id = await _make_product(session, "sub-involuntary")
    await process_event(
        session,
        _subscription_event("evt_1", "customer.subscription.created", product_id=product_id),
    )
    await session.commit()

    await process_event(
        session,
        _subscription_event(
            "evt_2",
            "customer.subscription.deleted",
            product_id=product_id,
            status="canceled",
            cancellation_reason="payment_failed",
        ),
    )
    await session.commit()

    row = await repository.get_subscription_by_stripe_id(session, "sub_1")
    assert row is not None
    assert row.status == "canceled"
    assert row.churn_kind == "involuntary"
    assert row.churned_at is not None


async def test_deleted_with_a_customer_requested_reason_is_voluntary_churn(
    session: AsyncSession,
) -> None:
    product_id = await _make_product(session, "sub-voluntary")
    await process_event(
        session,
        _subscription_event("evt_1", "customer.subscription.created", product_id=product_id),
    )
    await session.commit()

    await process_event(
        session,
        _subscription_event(
            "evt_2",
            "customer.subscription.deleted",
            product_id=product_id,
            status="canceled",
            cancellation_reason="cancellation_requested",
        ),
    )
    await session.commit()

    row = await repository.get_subscription_by_stripe_id(session, "sub_1")
    assert row is not None
    assert row.churn_kind == "voluntary"


async def test_deleted_with_no_reason_at_all_defaults_to_voluntary(
    session: AsyncSession,
) -> None:
    """The conservative default when the evidence doesn't clearly say
    involuntary -- never guess payment_failed without Stripe actually saying so."""
    product_id = await _make_product(session, "sub-no-reason")
    await process_event(
        session,
        _subscription_event("evt_1", "customer.subscription.created", product_id=product_id),
    )
    await session.commit()

    await process_event(
        session,
        _subscription_event(
            "evt_2", "customer.subscription.deleted", product_id=product_id, status="canceled"
        ),
    )
    await session.commit()

    row = await repository.get_subscription_by_stripe_id(session, "sub_1")
    assert row is not None
    assert row.churn_kind == "voluntary"


# --- dunning: open, bump, recover, and the zombie-row fix --------------------------


async def test_payment_failed_opens_a_dunning_row(session: AsyncSession) -> None:
    product_id = await _make_product(session, "dunning-open")
    await process_event(
        session,
        _subscription_event("evt_1", "customer.subscription.created", product_id=product_id),
    )
    await session.commit()

    outcome = await process_event(session, _invoice_failed_event("evt_2", stripe_invoice_id="in_1"))
    await session.commit()

    assert outcome.handled is True
    row = (
        await session.execute(select(DunningEvent).where(DunningEvent.stripe_invoice_id == "in_1"))
    ).scalar_one()
    assert row.status == "open"
    assert row.attempt_count == 1


async def test_repeated_retries_for_the_same_invoice_bump_one_row(
    session: AsyncSession,
) -> None:
    product_id = await _make_product(session, "dunning-retry")
    await process_event(
        session,
        _subscription_event("evt_1", "customer.subscription.created", product_id=product_id),
    )
    await session.commit()

    await process_event(session, _invoice_failed_event("evt_2", stripe_invoice_id="in_1"))
    await process_event(session, _invoice_failed_event("evt_3", stripe_invoice_id="in_1"))
    await process_event(session, _invoice_failed_event("evt_4", stripe_invoice_id="in_1"))
    await session.commit()

    rows = (
        (
            await session.execute(
                select(DunningEvent).where(DunningEvent.stripe_invoice_id == "in_1")
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].attempt_count == 3


async def test_a_failed_invoice_before_the_subscription_is_known_is_a_routine_skip(
    session: AsyncSession,
) -> None:
    """A benign delivery-order race, same reasoning as the ledger's own FK
    avoidance -- must not raise, just decline for now."""
    outcome = await process_event(
        session, _invoice_failed_event("evt_1", stripe_invoice_id="in_orphan")
    )
    assert outcome.handled is False
    assert (await session.execute(select(DunningEvent))).scalars().all() == []


async def test_invoice_paid_recovers_an_open_dunning_row(session: AsyncSession) -> None:
    product_id = await _make_product(session, "dunning-recovered")
    await process_event(
        session,
        _subscription_event("evt_1", "customer.subscription.created", product_id=product_id),
    )
    await process_event(session, _invoice_failed_event("evt_2", stripe_invoice_id="in_1"))
    await session.commit()

    await process_event(session, _invoice_paid_event("evt_3", stripe_invoice_id="in_1"))
    await session.commit()

    row = (
        await session.execute(select(DunningEvent).where(DunningEvent.stripe_invoice_id == "in_1"))
    ).scalar_one()
    assert row.status == "recovered"
    assert row.closed_at is not None


async def test_subscription_deleted_closes_a_still_open_dunning_row(
    session: AsyncSession,
) -> None:
    """The zombie-row fix: a customer who never recovers never fires
    invoice.paid -- without this, the row leaks open forever."""
    product_id = await _make_product(session, "dunning-zombie")
    await process_event(
        session,
        _subscription_event("evt_1", "customer.subscription.created", product_id=product_id),
    )
    await process_event(session, _invoice_failed_event("evt_2", stripe_invoice_id="in_1"))
    await session.commit()

    await process_event(
        session,
        _subscription_event(
            "evt_3",
            "customer.subscription.deleted",
            product_id=product_id,
            status="canceled",
            cancellation_reason="payment_failed",
        ),
    )
    await session.commit()

    row = (
        await session.execute(select(DunningEvent).where(DunningEvent.stripe_invoice_id == "in_1"))
    ).scalar_one()
    assert row.status == "terminal"
    assert row.closed_at is not None


# --- metrics ------------------------------------------------------------------------


async def test_mrr_cents_sums_only_active_subscriptions(session: AsyncSession) -> None:
    product_id = await _make_product(session, "mrr-active")
    await process_event(
        session,
        _subscription_event(
            "evt_1",
            "customer.subscription.created",
            stripe_subscription_id="sub_a",
            product_id=product_id,
            price_cents=4900,
        ),
    )
    await process_event(
        session,
        _subscription_event(
            "evt_2",
            "customer.subscription.created",
            stripe_subscription_id="sub_b",
            stripe_customer_id="cus_2",
            product_id=product_id,
            price_cents=9900,
        ),
    )
    await session.commit()
    await process_event(
        session,
        _subscription_event(
            "evt_3",
            "customer.subscription.deleted",
            stripe_subscription_id="sub_b",
            stripe_customer_id="cus_2",
            product_id=product_id,
            status="canceled",
        ),
    )
    await session.commit()

    assert await repository.mrr_cents(session) == 4900
    assert await repository.active_subscription_count(session) == 1


async def test_churning_products_within_the_window(session: AsyncSession) -> None:
    product_id = await _make_product(session, "churning-window")
    await process_event(
        session,
        _subscription_event("evt_1", "customer.subscription.created", product_id=product_id),
    )
    await session.commit()

    since = datetime.now(UTC) - timedelta(days=30)
    assert await repository.churning_products(session, since=since) == []

    await process_event(
        session,
        _subscription_event(
            "evt_2", "customer.subscription.deleted", product_id=product_id, status="canceled"
        ),
    )
    await session.commit()

    assert await repository.churning_products(session, since=since) == [product_id]


# --- FactoryMetrics.collect() actually surfaces the new fields --------------------


async def test_factory_metrics_collect_surfaces_mrr_and_churn(session: AsyncSession) -> None:
    from factory.observability.metrics import collect

    product_id = await _make_product(session, "metrics-collect")
    await process_event(
        session,
        _subscription_event(
            "evt_1", "customer.subscription.created", product_id=product_id, price_cents=4900
        ),
    )
    await session.commit()

    metrics = await collect(session)

    assert metrics.mrr_cents == 4900
    assert metrics.active_subscriptions == 1
    assert metrics.involuntary_churn_30d == 0
    assert metrics.voluntary_churn_30d == 0


# --- the append_ledger invariant ----------------------------------------------------


async def test_append_ledger_is_never_called_for_any_subscription_or_dunning_event(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Subscription lifecycle events move no money -- see finance/webhooks.py's
    own module docstring and state/repository.py's subscriptions section
    header. Elevated from a comment to an enforced test: a docstring cannot
    stop a future maintainer from wiring a ledger call into a new handler by
    copying a nearby pattern."""

    async def _forbidden(*args: object, **kwargs: object) -> bool:
        raise AssertionError("append_ledger must never be called for a state-notification event")

    monkeypatch.setattr(repository, "append_ledger", _forbidden)

    product_id = await _make_product(session, "ledger-invariant")

    events = [
        (
            "evt_1",
            _subscription_event("evt_1", "customer.subscription.created", product_id=product_id),
        ),
        (
            "evt_2",
            _subscription_event("evt_2", "customer.subscription.updated", product_id=product_id),
        ),
        (
            "evt_3",
            _subscription_event(
                "evt_3",
                "customer.subscription.deleted",
                product_id=product_id,
                status="canceled",
                cancellation_reason="payment_failed",
            ),
        ),
        ("evt_4", _invoice_failed_event("evt_4", stripe_invoice_id="in_invariant")),
    ]
    for _event_id, event in events:
        outcome = await process_event(session, event)
        assert outcome.handled is True

    # No LedgerEntry row exists -- proof positive, not just "no exception was
    # raised" (the monkeypatch already guarantees that part).
    assert (await session.execute(select(LedgerEntry))).scalars().all() == []
    assert (await session.execute(select(Subscription))).scalars().all() != []
