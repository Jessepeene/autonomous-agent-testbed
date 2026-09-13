"""Reply-driven guidance/REJECT for a heal-exhausted product.

Before this, `graph.py`'s heal-exhaustion escalation asked a human to "Reply with
guidance, or REJECT to retire this product" -- but nothing read the reply at all. The
product just sat in `PLANNED` forever regardless of what a human said, the same class
of gap as the task-level retry feature, just with no `task_id` to hang the fix on
(the product was deliberately reset to `PLANNED` instead of leaving one task in
place, so `Interrupt.product_id` carries the link instead).

Needs Postgres for the JSONB task payloads:

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

from factory.hitl.interrupts import collect_replies
from factory.state import repository
from factory.state.orm import Base, Product, Task
from factory.state.schemas import InterruptReason, ProductStatus, ScoredIdea, TaskKind, TaskStatus

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


async def _heal_exhausted_interrupt(session: AsyncSession, product_id: str, subject: str) -> str:
    """Mirrors graph.py's real heal-exhaustion escalation: no task_id, product_id set."""
    interrupt_id = await repository.open_interrupt(
        session,
        reason=InterruptReason.LEGAL_UNCERTAINTY,
        subject=subject,
        detail="Automated repair did not converge.",
        headline="\U0001f527 Repair stuck — needs your input",
        reply_hint="Reply with guidance for one more attempt, or REJECT to retire this product.",
        product_id=product_id,
    )
    assert interrupt_id is not None
    return interrupt_id


async def test_reject_reply_retires_the_product(session: AsyncSession) -> None:
    product_id = await _make_product(session, "invoice-bot")
    interrupt_id = await _heal_exhausted_interrupt(session, product_id, "failed 3 heal cycles")
    notifier = _ScriptedNotifier([f"#{interrupt_id[:8]} REJECT, not worth pursuing"])

    resolved = await collect_replies(session, notifier)

    assert resolved == 1
    product = await session.get(Product, product_id)
    assert product is not None
    assert product.status == ProductStatus.RETIRED.value


async def test_guidance_reply_enqueues_exactly_one_patch_task(session: AsyncSession) -> None:
    product_id = await _make_product(session, "invoice-bot-2")
    interrupt_id = await _heal_exhausted_interrupt(session, product_id, "failed 3 heal cycles")
    guidance = "The /auth/register endpoint must return a token key in its JSON body."
    notifier = _ScriptedNotifier([f"#{interrupt_id[:8]} {guidance}"])

    resolved = await collect_replies(session, notifier)

    assert resolved == 1
    product = await session.get(Product, product_id)
    assert product is not None
    assert product.status != ProductStatus.RETIRED.value

    tasks = (
        (
            await session.execute(
                select(Task).where(
                    Task.product_id == product_id, Task.kind == TaskKind.PATCH_PRODUCT.value
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(tasks) == 1
    assert tasks[0].status == TaskStatus.PENDING.value

    # The reply is *added* to the escalation, not substituted for it.
    #
    # This used to assert equality, which passively encoded the bug: the operator's
    # words became the repair agent's entire brief and the escalation that prompted
    # them was dropped. Observed live with a reply of "Try again" -- eight Bug
    # Council findings naming specific files and lines vanished, and the agent read
    # the whole codebase hunting for what it was meant to do, exactly as an engineer
    # handed a two-word ticket would.
    healing = tasks[0].payload["healing"]
    assert guidance in healing
    assert "failed 3 heal cycles" in healing


async def test_a_second_escalation_after_a_guided_retry_can_also_be_guided(
    session: AsyncSession,
) -> None:
    """The idempotency key must not collide across two separate guided attempts for
    the same product -- otherwise the second REJECT/guidance reply would silently
    resolve the interrupt while enqueuing nothing."""
    product_id = await _make_product(session, "invoice-bot-3")
    first_interrupt = await _heal_exhausted_interrupt(session, product_id, "failed 3 heal cycles")
    notifier = _ScriptedNotifier([f"#{first_interrupt[:8]} fix the token bug"])
    assert await collect_replies(session, notifier) == 1

    # Simulate the guided patch failing again and re-escalating with a fresh interrupt.
    second_interrupt = await _heal_exhausted_interrupt(
        session, product_id, "failed 3 heal cycles (again)"
    )
    notifier2 = _ScriptedNotifier([f"#{second_interrupt[:8]} try a different auth library"])
    assert await collect_replies(session, notifier2) == 1

    tasks = (
        (
            await session.execute(
                select(Task).where(
                    Task.product_id == product_id, Task.kind == TaskKind.PATCH_PRODUCT.value
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(tasks) == 2


async def test_empty_reply_neither_retires_nor_enqueues(session: AsyncSession) -> None:
    product_id = await _make_product(session, "invoice-bot-4")
    interrupt_id = await _heal_exhausted_interrupt(session, product_id, "failed 3 heal cycles")
    notifier = _ScriptedNotifier([f"#{interrupt_id[:8]}"])

    resolved = await collect_replies(session, notifier)

    assert resolved == 1
    product = await session.get(Product, product_id)
    assert product is not None
    assert product.status == ProductStatus.PLANNED.value

    tasks = (
        (await session.execute(select(Task).where(Task.product_id == product_id))).scalars().all()
    )
    assert tasks == []
