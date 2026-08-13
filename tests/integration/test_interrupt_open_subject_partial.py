"""`uq_interrupts_open_subject` must actually mean "open" -- the root-cause
fix behind 0017_interrupt_open_subject_partial.py, migration 0017.

The old plain `UniqueConstraint(reason, subject, status)` made a given
(reason, subject) globally unique *forever*, across every status, not just
while `status='open'`. The moment any interrupt with a given (reason,
subject) was resolved, no later, unrelated occurrence of that exact pair
could ever be resolved either -- `resolve_interrupt()`'s UPDATE collided with
the row already sitting at that (reason, subject, 'resolved') tuple. Found
live for HEAL_EXHAUSTED; a broader audit found the same latent crash in
several other call sites that never gave their subject any per-occurrence
uniqueness at all.

Needs Postgres:

    docker compose up -d postgres
    alembic upgrade head
    pytest tests/integration -q
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from factory.state import repository
from factory.state.orm import Base
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


async def test_two_separate_occurrences_of_the_same_subject_can_both_resolve(
    session: AsyncSession,
) -> None:
    """The actual regression: a bare, non-unique subject (exactly the shape
    deliverability.py, supervisor/audit.py, and main.py's preflight all use)
    raised twice, resolved twice, in two separate episodes months apart in
    spirit. Under the old plain UniqueConstraint this crashed with
    UniqueViolationError the moment the second one tried to resolve."""
    first_id = await repository.open_interrupt(
        session,
        reason=InterruptReason.DATA_INTEGRITY,
        subject="Recurring problem, identical subject both times",
        detail="first occurrence",
        headline="Test escalation",
        reply_hint="No reply needed — informational only.",
        needs_reply=False,
    )
    assert first_id is not None
    await repository.resolve_interrupt(session, first_id, "acknowledged")
    await session.flush()

    # A second, unrelated occurrence of the exact same (reason, subject) --
    # legitimate under the partial index, since the first is no longer 'open'.
    second_id = await repository.open_interrupt(
        session,
        reason=InterruptReason.DATA_INTEGRITY,
        subject="Recurring problem, identical subject both times",
        detail="second occurrence, unrelated to the first",
        headline="Test escalation",
        reply_hint="No reply needed — informational only.",
        needs_reply=False,
    )
    assert second_id is not None
    assert second_id != first_id

    # The real regression: resolving the second one must not raise.
    await repository.resolve_interrupt(session, second_id, "acknowledged again")
    await session.flush()


async def test_two_open_interrupts_with_the_same_subject_still_collapse_into_one(
    session: AsyncSession,
) -> None:
    """The dedup behaviour the constraint exists for at all must still work --
    the fix narrows the constraint's scope, it must not remove it."""
    first_id = await repository.open_interrupt(
        session,
        reason=InterruptReason.DATA_INTEGRITY,
        subject="Still-open problem",
        detail="first attempt",
        headline="Test escalation",
        reply_hint="No reply needed — informational only.",
        needs_reply=False,
    )
    assert first_id is not None

    second_id = await repository.open_interrupt(
        session,
        reason=InterruptReason.DATA_INTEGRITY,
        subject="Still-open problem",
        detail="second attempt while the first is still open",
        headline="Test escalation",
        reply_hint="No reply needed — informational only.",
        needs_reply=False,
    )

    assert second_id is None  # collapsed into the first, exactly as before
