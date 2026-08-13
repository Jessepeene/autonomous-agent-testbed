"""Reply-driven APPROVE/REJECT for a flagged idea.

Before this, `compliance/graph.py`'s FLAG_FOR_REVIEW escalation asked a human to
"Reply APPROVE to allow the build, or REJECT to discard the idea" -- but nothing
read the reply at all. The idea just sat `flagged` forever regardless of what a
human said: invisible to every pipeline count (it is neither "pending" nor
"approved"), but never actually resolved into a terminal outcome either.

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
from factory.settings import EngineName
from factory.state import repository
from factory.state.orm import Base, Idea, Task
from factory.state.schemas import InterruptReason, ScoredIdea, TaskKind

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


async def _make_flagged_idea(session: AsyncSession, name: str) -> tuple[str, str]:
    """Mirrors compliance/graph.py's real FLAG_FOR_REVIEW branch: idea set to
    "flagged", an interrupt raised with task_id set and no product_id (no product
    exists yet at the idea-flagging stage)."""
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
    task_id = await repository.enqueue_task(
        session,
        kind=TaskKind.ASSESS_COMPLIANCE,
        engine=EngineName.COMPLIANCE,
        idempotency_key=f"compliance:idea:{idea_id}",
        payload={"idea_id": idea_id},
        idea_id=idea_id,
    )
    assert task_id is not None
    await repository.set_idea_status(session, idea_id, "flagged")
    interrupt_id = await repository.open_interrupt(
        session,
        reason=InterruptReason.LEGAL_UNCERTAINTY,
        subject=f"Idea needs legal review: {name}",
        detail="Confidence: 0.40\nArguable liability, human should decide.",
        headline="⚖️ Legal review needed — new idea",
        reply_hint="Reply APPROVE to let this idea proceed to build, or REJECT to discard it.",
        task_id=task_id,
    )
    assert interrupt_id is not None
    return idea_id, interrupt_id


async def test_reject_reply_marks_the_idea_rejected(session: AsyncSession) -> None:
    idea_id, interrupt_id = await _make_flagged_idea(session, "bookkeeping-reject")
    notifier = _ScriptedNotifier([f"#{interrupt_id[:8]} REJECT, too much liability"])

    resolved = await collect_replies(session, notifier)

    assert resolved == 1
    idea = await session.get(Idea, idea_id)
    assert idea is not None
    assert idea.status == "rejected"


async def test_approve_reply_marks_the_idea_approved_and_resumes_the_pipeline(
    session: AsyncSession,
) -> None:
    idea_id, interrupt_id = await _make_flagged_idea(session, "bookkeeping-approve")
    notifier = _ScriptedNotifier([f"#{interrupt_id[:8]} APPROVE, acceptable risk"])

    resolved = await collect_replies(session, notifier)

    assert resolved == 1
    idea = await session.get(Idea, idea_id)
    assert idea is not None
    assert idea.status == "approved"

    tasks = (
        (
            await session.execute(
                select(Task).where(
                    Task.idea_id == idea_id, Task.kind == TaskKind.ASSESS_COMPLIANCE.value
                )
            )
        )
        .scalars()
        .all()
    )
    # The original flagging task plus the reply-driven re-entry task.
    assert len(tasks) == 2


async def test_empty_reply_leaves_the_idea_flagged(session: AsyncSession) -> None:
    idea_id, interrupt_id = await _make_flagged_idea(session, "bookkeeping-noop")
    notifier = _ScriptedNotifier([f"#{interrupt_id[:8]}"])

    resolved = await collect_replies(session, notifier)

    assert resolved == 1
    idea = await session.get(Idea, idea_id)
    assert idea is not None
    assert idea.status == "flagged"
