"""Phase 5b: the review that reads the company as a whole, not one product.

Everything else that judges anything in this factory judges one product or one
department. This is the first thing that reads a streak of failures across several
different products and asks whether the objective itself is being served -- so the
properties worth protecting are different from a release gate's: the trigger must
fire on a real condition and not a clock, the four-way finding must include
`undetermined` as a real answer rather than a forced guess, and a review must always
reach a human, because no autonomous owner for any of its three findings exists yet.

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
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from factory import attribution
from factory.hitl.telegram import NullNotifier
from factory.llm.client import LLMClient
from factory.llm.providers.base import Provider
from factory.llm.providers.fake import FakeProvider
from factory.llm.router import ProviderRegistry
from factory.sandbox.disabled import DisabledSandbox
from factory.settings import EngineName, Settings
from factory.state import repository
from factory.state.orm import Base, Decision, Interrupt, Task, new_id
from factory.state.schemas import StrategicReviewVerdict, TaskKind
from factory.strategic_review import (
    TRIGGER_STREAK,
    founding_paragraph_scorecard,
    rethink_or_abandon_streak,
    run_strategic_review,
    should_trigger,
)
from factory.supervisor.context import FactoryContext

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


def _ctx(gemini: FakeProvider) -> FactoryContext:
    settings = Settings(database_url=DATABASE_URL, gemini_api_key="test-key")
    llm = LLMClient(ProviderRegistry({Provider.GEMINI: gemini}), settings=settings)
    return FactoryContext(
        settings=settings,
        llm=llm,
        notifier=NullNotifier(),
        sandbox=DisabledSandbox(),
        enabled_engines={EngineName.SUPERVISOR},
        worker_id="strategic-review-test",
    )


async def _release_decision(
    session: AsyncSession, *, choice: str, product_id: str, age_hours: int = 0
) -> Decision:
    """A minimal but real release decision -- a real completed task behind it, so
    `decision_return` (which walks the task's subtree) has something real to find
    rather than nothing."""
    task_id = await repository.enqueue_task(
        session,
        kind=TaskKind.PATCH_PRODUCT,
        engine=EngineName.DEVELOPMENT,
        idempotency_key=f"test-release:{new_id()}",
        payload={},
        product_id=product_id,
    )
    assert task_id is not None
    await repository.complete_task(session, task_id)

    decision_id = await attribution.record_decision(
        session,
        kind="release",
        subject_kind="product",
        subject_id=product_id,
        choice=choice,
        rationale=f"test rationale for {choice}",
        made_by_engine=EngineName.DEVELOPMENT.value,
        made_by_task_id=task_id,
        alternatives=["ship", "fix_first", "rethink", "abandon"],
    )
    if age_hours:
        await session.execute(
            update(Decision)
            .where(Decision.id == decision_id)
            .values(created_at=datetime.now(UTC) - timedelta(hours=age_hours))
        )
    row = await session.get(Decision, decision_id)
    assert row is not None
    return row


class TestTheStreak:
    async def test_a_run_of_ships_never_triggers(self, session: AsyncSession) -> None:
        for _ in range(TRIGGER_STREAK + 2):
            await _release_decision(session, choice="ship", product_id=new_id())
        await session.commit()
        assert await should_trigger(session) is None

    async def test_below_threshold_does_not_trigger(self, session: AsyncSession) -> None:
        for _ in range(TRIGGER_STREAK - 1):
            await _release_decision(session, choice="rethink", product_id=new_id())
        await session.commit()
        assert await should_trigger(session) is None

    async def test_at_threshold_triggers(self, session: AsyncSession) -> None:
        for _ in range(TRIGGER_STREAK):
            await _release_decision(session, choice="rethink", product_id=new_id())
        await session.commit()
        streak = await should_trigger(session)
        assert streak is not None
        assert len(streak) == TRIGGER_STREAK

    async def test_a_ship_in_between_breaks_the_streak(self, session: AsyncSession) -> None:
        """The whole point of 'consecutive': one product shipping does not get
        cancelled out by unrelated failures before it -- the streak counts only
        what has happened *since* something last worked."""
        await _release_decision(session, choice="abandon", product_id=new_id(), age_hours=5)
        await _release_decision(session, choice="ship", product_id=new_id(), age_hours=4)
        for _ in range(TRIGGER_STREAK):
            await _release_decision(session, choice="rethink", product_id=new_id())
        await session.commit()

        streak = await rethink_or_abandon_streak(session)
        assert len(streak) == TRIGGER_STREAK
        assert all(d.choice == "rethink" for d in streak)

    async def test_mixed_rethink_and_abandon_both_count(self, session: AsyncSession) -> None:
        """The roadmap's own wording is 'rethink/abandon', not 'rethink, then a
        separate count for abandon' -- either verdict says the same thing, that
        shipping was not the answer."""
        await _release_decision(session, choice="rethink", product_id=new_id())
        await _release_decision(session, choice="abandon", product_id=new_id())
        await _release_decision(session, choice="rethink", product_id=new_id())
        await session.commit()
        assert await should_trigger(session) is not None

    async def test_newest_decision_is_first(self, session: AsyncSession) -> None:
        """`should_trigger`'s own contract: the caller keys the enqueue on
        `streak[0].id`, so it must be the newest one, not the oldest."""
        oldest = await _release_decision(
            session, choice="rethink", product_id=new_id(), age_hours=3
        )
        await _release_decision(session, choice="rethink", product_id=new_id(), age_hours=2)
        newest = await _release_decision(session, choice="rethink", product_id=new_id())
        await session.commit()

        streak = await should_trigger(session)
        assert streak is not None
        assert streak[0].id == newest.id
        assert streak[-1].id == oldest.id


class TestTheScorecard:
    async def test_no_objective_reads_not_met(self, session: AsyncSession) -> None:
        clauses = await founding_paragraph_scorecard(session)
        immutable = next(c for c in clauses if c.clause == "The objective is immutable.")
        assert immutable.status == "not_met"

    async def test_a_live_objective_reads_met(self, session: AsyncSession) -> None:
        await repository.record_objective(session, statement="Test objective.", set_by="jesse")
        await session.commit()
        clauses = await founding_paragraph_scorecard(session)
        immutable = next(c for c in clauses if c.clause == "The objective is immutable.")
        assert immutable.status == "met"
        assert "jesse" in immutable.evidence

    async def test_zero_customers_is_undetermined_not_failed(self, session: AsyncSession) -> None:
        """The whole discipline this phase exists to carry forward from Phase 2.12:
        no evidence is not the same as negative evidence, and a scorecard that
        cannot tell them apart would manufacture a false failing grade against a
        company that has simply not sold anything yet."""
        clauses = await founding_paragraph_scorecard(session)
        feedback = next(
            c for c in clauses if c.clause == "The organisation learns from real-world feedback."
        )
        assert feedback.status == "undetermined"

    async def test_first_ever_review_says_so(self, session: AsyncSession) -> None:
        clauses = await founding_paragraph_scorecard(session)
        cycle = next(
            c for c in clauses if c.clause == "A recurring strategic review cycle runs."
        )
        assert cycle.status == "not_met"
        assert "#1" in cycle.evidence


class TestRunningTheReview:
    def _verdict(self, finding: str) -> StrategicReviewVerdict:
        return StrategicReviewVerdict(
            assessment="The company has failed to ship three products in a row, "
            "each for a different stated reason, with no real customers yet.",
            finding=finding,  # type: ignore[arg-type]
            rationale=f"test rationale for {finding}",
        )

    async def test_records_a_company_level_decision(self, session: AsyncSession) -> None:
        for _ in range(TRIGGER_STREAK):
            await _release_decision(session, choice="rethink", product_id=new_id())
        await session.commit()

        task_id = await repository.enqueue_task(
            session,
            kind=TaskKind.STRATEGIC_REVIEW,
            engine=EngineName.SUPERVISOR,
            idempotency_key="test-strategic-review",
            payload={},
        )
        assert task_id is not None
        task = await session.get(Task, task_id)
        assert task is not None

        gemini = FakeProvider()
        gemini.register(StrategicReviewVerdict, self._verdict("failed_strategy"))
        result = await run_strategic_review(_ctx(gemini), session, task)
        assert "failed_strategy" in result

        row = (
            await session.execute(select(Decision).where(Decision.kind == "strategic_review"))
        ).scalar_one()
        assert row.choice == "failed_strategy"
        assert row.subject_kind == "company"

    async def test_undetermined_is_recorded_as_a_real_finding_not_a_failure(
        self, session: AsyncSession
    ) -> None:
        for _ in range(TRIGGER_STREAK):
            await _release_decision(session, choice="abandon", product_id=new_id())
        await session.commit()
        task_id = await repository.enqueue_task(
            session,
            kind=TaskKind.STRATEGIC_REVIEW,
            engine=EngineName.SUPERVISOR,
            idempotency_key="test-strategic-review-undetermined",
            payload={},
        )
        assert task_id is not None
        task = await session.get(Task, task_id)
        assert task is not None

        gemini = FakeProvider()
        gemini.register(StrategicReviewVerdict, self._verdict("undetermined"))
        await run_strategic_review(_ctx(gemini), session, task)

        row = (
            await session.execute(select(Decision).where(Decision.kind == "strategic_review"))
        ).scalar_one()
        assert row.choice == "undetermined"

    async def test_always_escalates_since_no_autonomous_owner_exists(
        self, session: AsyncSession
    ) -> None:
        for _ in range(TRIGGER_STREAK):
            await _release_decision(session, choice="rethink", product_id=new_id())
        await session.commit()
        task_id = await repository.enqueue_task(
            session,
            kind=TaskKind.STRATEGIC_REVIEW,
            engine=EngineName.SUPERVISOR,
            idempotency_key="test-strategic-review-escalates",
            payload={},
        )
        assert task_id is not None
        task = await session.get(Task, task_id)
        assert task is not None

        gemini = FakeProvider()
        gemini.register(StrategicReviewVerdict, self._verdict("execution_problem"))
        await run_strategic_review(_ctx(gemini), session, task)

        interrupt = (
            await session.execute(
                select(Interrupt).where(Interrupt.reason == "strategic_review")
            )
        ).scalar_one()
        assert interrupt.status == "open"
        assert "execution_problem" in interrupt.detail


class TestTheEnqueueItself:
    async def test_a_new_streak_head_is_a_fresh_enqueue(self, session: AsyncSession) -> None:
        """Re-checking `should_trigger` every tick must not duplicate the task once
        the company's state has not changed since the last check -- the same
        lifetime-versus-occurrence discipline as every other idempotency key in
        this codebase."""
        for _ in range(TRIGGER_STREAK):
            await _release_decision(session, choice="rethink", product_id=new_id())
        await session.commit()

        streak = await should_trigger(session)
        assert streak is not None
        first = await repository.enqueue_task(
            session,
            kind=TaskKind.STRATEGIC_REVIEW,
            engine=EngineName.SUPERVISOR,
            idempotency_key=f"strategic-review:{streak[0].id}",
            payload={},
        )
        assert first is not None

        # Nothing about the streak changed -- checking again must be a no-op.
        streak_again = await should_trigger(session)
        assert streak_again is not None
        second = await repository.enqueue_task(
            session,
            kind=TaskKind.STRATEGIC_REVIEW,
            engine=EngineName.SUPERVISOR,
            idempotency_key=f"strategic-review:{streak_again[0].id}",
            payload={},
        )
        assert second is None

        # A genuinely new qualifying decision must get a fresh review, not be
        # silently dropped by a key that never changes.
        await _release_decision(session, choice="abandon", product_id=new_id())
        await session.commit()
        newer_streak = await should_trigger(session)
        assert newer_streak is not None
        third = await repository.enqueue_task(
            session,
            kind=TaskKind.STRATEGIC_REVIEW,
            engine=EngineName.SUPERVISOR,
            idempotency_key=f"strategic-review:{newer_streak[0].id}",
            payload={},
        )
        assert third is not None
