"""The top-level campaign approval gate, end to end.

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

from factory.engines.marketing import campaign
from factory.settings import EngineName
from factory.state import repository
from factory.state.orm import Base, Interrupt, Lead
from factory.state.schemas import ScoredIdea, TaskKind

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


async def _make_task(session: AsyncSession, product_id: str) -> str:
    task_id = await repository.enqueue_task(
        session,
        kind=TaskKind.SEND_OUTREACH,
        engine=EngineName.MARKETING,
        idempotency_key=f"outreach:{product_id}:1",
        payload={"product_id": product_id},
        product_id=product_id,
    )
    assert task_id is not None
    return task_id


def _lead() -> Lead:
    return Lead(
        first_name="Ada",
        last_name="Lovelace",
        job_title="Head of Finance",
        company="Analytical Engines BV",
        work_email="ada@analytical-engines.test",
        source_url="https://example.test/team",
        scraped_at=datetime(2026, 1, 1, tzinfo=UTC),
        lia_passed=True,
    )


async def test_first_attempt_creates_a_pending_approval_and_raises_an_interrupt(
    session: AsyncSession,
) -> None:
    product_id = await _make_product(session, "Reconciliation Assistant")
    task_id = await _make_task(session, product_id)

    with pytest.raises(campaign.CampaignApprovalPending):
        await campaign.ensure_approval(
            session,
            product_id=product_id,
            product_name="Reconciliation Assistant",
            task_id=task_id,
            candidates=[_lead()],
        )
    await session.commit()

    from sqlalchemy import select

    from factory.state.orm import CampaignApproval

    approval = (
        await session.execute(select(CampaignApproval).where(CampaignApproval.task_id == task_id))
    ).scalar_one()
    assert approval.status == "pending"
    assert approval.interrupt_id is not None

    interrupt = await session.get(Interrupt, approval.interrupt_id)
    assert interrupt is not None
    assert interrupt.reason == "campaign_approval"
    assert interrupt.status == "open"
    # The reply instruction lives on the interrupt now, not baked into the
    # brief text itself (see hitl/formatting.py) -- the brief is pure
    # PII-safe compliance content, the instruction is separate and explicit.
    assert interrupt.product_id == product_id
    assert "APPROVE" in (interrupt.reply_hint or "")
    assert "REJECT" in (interrupt.reply_hint or "")
    assert interrupt.headline is not None


async def test_a_retried_attempt_does_not_duplicate_the_interrupt(session: AsyncSession) -> None:
    product_id = await _make_product(session, "Reconciliation Assistant")
    task_id = await _make_task(session, product_id)

    for _ in range(3):
        with pytest.raises(campaign.CampaignApprovalPending):
            await campaign.ensure_approval(
                session,
                product_id=product_id,
                product_name="Reconciliation Assistant",
                task_id=task_id,
                candidates=[_lead()],
            )
    await session.commit()

    from sqlalchemy import func, select

    from factory.state.orm import CampaignApproval

    count = (
        await session.execute(
            select(func.count())
            .select_from(CampaignApproval)
            .where(CampaignApproval.task_id == task_id)
        )
    ).scalar_one()
    assert count == 1

    open_interrupts = (
        await session.execute(
            select(func.count())
            .select_from(Interrupt)
            .where(Interrupt.reason == "campaign_approval")
        )
    ).scalar_one()
    assert open_interrupts == 1


async def test_an_approved_reply_unblocks_the_cohort(session: AsyncSession) -> None:
    product_id = await _make_product(session, "Reconciliation Assistant")
    task_id = await _make_task(session, product_id)

    with pytest.raises(campaign.CampaignApprovalPending):
        await campaign.ensure_approval(
            session,
            product_id=product_id,
            product_name="Reconciliation Assistant",
            task_id=task_id,
            candidates=[_lead()],
        )
    await session.commit()

    from sqlalchemy import select

    from factory.state.orm import CampaignApproval

    approval = (
        await session.execute(select(CampaignApproval).where(CampaignApproval.task_id == task_id))
    ).scalar_one()
    assert approval.interrupt_id is not None
    await repository.resolve_interrupt(session, approval.interrupt_id, "APPROVE")
    await session.commit()

    synced = await campaign.sync_decisions(session)
    await session.commit()
    assert synced == 1

    # No exception now — the cohort is cleared to send.
    await campaign.ensure_approval(
        session,
        product_id=product_id,
        product_name="Reconciliation Assistant",
        task_id=task_id,
        candidates=[_lead()],
    )


async def test_an_ambiguous_reply_fails_closed_to_rejected(session: AsyncSession) -> None:
    product_id = await _make_product(session, "Reconciliation Assistant")
    task_id = await _make_task(session, product_id)

    with pytest.raises(campaign.CampaignApprovalPending):
        await campaign.ensure_approval(
            session,
            product_id=product_id,
            product_name="Reconciliation Assistant",
            task_id=task_id,
            candidates=[_lead()],
        )
    await session.commit()

    from sqlalchemy import select

    from factory.state.orm import CampaignApproval

    approval = (
        await session.execute(select(CampaignApproval).where(CampaignApproval.task_id == task_id))
    ).scalar_one()
    assert approval.interrupt_id is not None
    # A human reply that isn't an unambiguous "approve" — a question, not a decision.
    await repository.resolve_interrupt(session, approval.interrupt_id, "why these leads?")
    await session.commit()

    await campaign.sync_decisions(session)
    await session.commit()

    with pytest.raises(campaign.CampaignRejected):
        await campaign.ensure_approval(
            session,
            product_id=product_id,
            product_name="Reconciliation Assistant",
            task_id=task_id,
            candidates=[_lead()],
        )


async def test_the_escalation_survives_being_raised_inside_a_rolled_back_session_scope(
    session: AsyncSession,
) -> None:
    """`_run_engine` (supervisor/graph.py) runs every task inside one `session_scope`,
    which commits on success and rolls back on ANY exception -- including
    `CampaignApprovalPending`, raised deliberately as control flow to defer the task,
    not as a failure. Before the fix, `ensure_approval` never committed before
    raising, so that rollback silently destroyed the interrupt and approval row it
    had just written: the escalation's own `hitl.raised` log line fired, but the row
    never reached the database, and a human was never actually asked -- the task just
    retried forever, re-raising and re-rolling-back the same doomed write every time.
    Found live. This reproduces the exact commit-on-success/rollback-on-exception
    wrapping `session_scope` applies in production."""
    product_id = await _make_product(session, "Reconciliation Assistant")
    task_id = await _make_task(session, product_id)

    try:
        await campaign.ensure_approval(
            session,
            product_id=product_id,
            product_name="Reconciliation Assistant",
            task_id=task_id,
            candidates=[_lead()],
        )
        pytest.fail("expected CampaignApprovalPending")
    except campaign.CampaignApprovalPending:
        await session.rollback()

    from sqlalchemy import select

    from factory.state.orm import CampaignApproval

    approval = (
        await session.execute(select(CampaignApproval).where(CampaignApproval.task_id == task_id))
    ).scalar_one()
    assert approval.status == "pending"
    assert approval.interrupt_id is not None

    interrupt = await session.get(Interrupt, approval.interrupt_id)
    assert interrupt is not None
    assert interrupt.status == "open"
    assert interrupt.reason == "campaign_approval"


async def test_sync_decisions_ignores_still_open_interrupts(session: AsyncSession) -> None:
    product_id = await _make_product(session, "Reconciliation Assistant")
    task_id = await _make_task(session, product_id)

    with pytest.raises(campaign.CampaignApprovalPending):
        await campaign.ensure_approval(
            session,
            product_id=product_id,
            product_name="Reconciliation Assistant",
            task_id=task_id,
            candidates=[_lead()],
        )
    await session.commit()

    synced = await campaign.sync_decisions(session)
    assert synced == 0
