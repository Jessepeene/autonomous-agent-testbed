"""An outcome is evidence about every decision that led to it.

`learning_episodes` had one non-null foreign key, `tunable_parameter_id`, and that
single column decided for the whole system that **every outcome teaches exactly one
thing**. A validation that failed because the idea was weak was recorded against the
prompt that wrote the landing page; the idea, the acceptance criteria, the design
standard and the decision to ship it all learned nothing.

Two properties matter most here and they pull in opposite directions:

- the links have to be **derived**, because a caller asked to list them is a caller
  that forgets -- which is exactly how `caused_by_task_id` came to be populated in 0
  of 232 rows;
- and the existing trials must keep their **exact** arithmetic, because a trial
  mid-flight whose numerator moves underneath it is worse than one that never ran.

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

from factory import attribution, causality
from factory.settings import EngineName
from factory.state import repository
from factory.state.orm import Base, Outcome, OutcomeEvidence, TaskEvent, new_id
from factory.state.schemas import TaskKind

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


async def _chain(session: AsyncSession, *task_ids: str) -> None:
    """Record `a caused b caused c`, the way `supervisor/graph.py` does on start."""
    for child, parent in zip(task_ids[1:], task_ids, strict=False):
        session.add(
            TaskEvent(
                id=new_id(),
                task_id=child,
                event="engine.start",
                engine=EngineName.DEVELOPMENT.value,
                detail={},
                caused_by_task_id=parent,
            )
        )
    await session.flush()


async def test_the_chain_is_walked_back_to_its_origin(session: AsyncSession) -> None:
    await _chain(session, "t-decide", "t-build", "t-review")
    assert await attribution.ancestors(session, "t-review") == ["t-review", "t-build", "t-decide"]
    # A task nothing caused is its own origin, which is correct rather than missing:
    # the scheduler really is where that work came from.
    assert await attribution.ancestors(session, "t-decide") == ["t-decide"]


async def test_a_cycle_cannot_hang_a_write_path(session: AsyncSession) -> None:
    """`caused_by_task_id` should form a tree. A bug that made it circular must not
    turn recording an outcome into an infinite loop."""
    await _chain(session, "t-a", "t-b")
    session.add(
        TaskEvent(
            id=new_id(),
            task_id="t-a",
            event="engine.start",
            engine=EngineName.DEVELOPMENT.value,
            detail={},
            caused_by_task_id="t-b",
        )
    )
    await session.flush()
    assert set(await attribution.ancestors(session, "t-b")) == {"t-a", "t-b"}


async def test_an_outcome_credits_every_decision_that_led_to_it(session: AsyncSession) -> None:
    """The whole point. Three judgements up the chain, one result at the bottom."""
    await _chain(session, "t-decide", "t-build", "t-evaluate")
    chose = await attribution.record_decision(
        session,
        kind="product_next",
        subject_kind="product",
        subject_id="p-1",
        choice="deployment",
        rationale="Nothing exists to put in front of anyone yet.",
        made_by_engine=EngineName.SUPERVISOR.value,
        made_by_task_id="t-decide",
        alternatives=["design_guidelines", "deployment"],
    )
    shipped = await attribution.record_decision(
        session,
        kind="release",
        subject_kind="product",
        subject_id="p-1",
        choice="ship",
        rationale="One open SHOULD. A trade-off, not a blocker.",
        made_by_engine=EngineName.DEVELOPMENT.value,
        made_by_task_id="t-build",
        alternatives=["ship", "fix_first", "rethink", "abandon"],
    )

    outcome_id = await attribution.record_outcome(
        session,
        kind="validation",
        succeeded=False,
        subject_kind="product",
        subject_id="p-1",
        task_id="t-evaluate",
        parameters=["param-landing-v7"],
        detail={"sent": 40, "signals": 0},
    )

    rows = (
        await session.execute(
            select(OutcomeEvidence.subject_kind, OutcomeEvidence.subject_id).where(
                OutcomeEvidence.outcome_id == outcome_id
            )
        )
    ).all()
    assert set(rows) == {
        ("parameter", "param-landing-v7"),
        ("decision", chose),
        ("decision", shipped),
    }, "a failed validation must not teach only the prompt that wrote the page"


async def test_a_decision_made_after_the_outcome_is_not_credited(session: AsyncSession) -> None:
    """Attribution is derived from the chain, not from the clock, but a decision
    recorded in a task that is not an ancestor has nothing to do with this result."""
    await _chain(session, "t-build", "t-evaluate")
    await attribution.record_decision(
        session,
        kind="release",
        subject_kind="product",
        subject_id="p-2",
        choice="ship",
        rationale="Unrelated product entirely.",
        made_by_engine=EngineName.DEVELOPMENT.value,
        made_by_task_id="t-somewhere-else",
    )
    outcome_id = await attribution.record_outcome(
        session, kind="validation", succeeded=True, task_id="t-evaluate"
    )
    linked = (
        (
            await session.execute(
                select(OutcomeEvidence.subject_id).where(OutcomeEvidence.outcome_id == outcome_id)
            )
        )
        .scalars()
        .all()
    )
    assert linked == []


async def test_trial_arithmetic_is_unchanged_by_the_new_shape(session: AsyncSession) -> None:
    """The migration's whole safety property, asserted as behaviour.

    `episode_counts` fed the trial statistics and read one table with one foreign key.
    It now reads through the join. A trial mid-flight whose numerator moves underneath
    it is worse than one that never ran, so these have to be the same numbers.
    """
    from factory.learning import store

    for succeeded in (True, True, False, True):
        await attribution.record_outcome(
            session,
            kind="trial_episode",
            succeeded=succeeded,
            parameters=["param-under-trial"],
            credit_decisions=False,
        )
    await session.flush()

    assert await store.episode_counts(session, "param-under-trial") == (3, 4)
    # ...and the rolling window a baseline lookup uses still caps correctly.
    _, total = await store.episode_counts(session, "param-under-trial", limit=2)
    assert total == 2


async def test_an_outcome_can_carry_money(session: AsyncSession) -> None:
    """Profit is a magnitude. A system that records only whether something worked
    cannot tell a EUR 12 month from a EUR 1,200 one, and the objective is profit."""
    outcome_id = await attribution.record_outcome(
        session, kind="revenue", value_cents=120_00, subject_kind="product", subject_id="p-1"
    )
    row = (await session.execute(select(Outcome).where(Outcome.id == outcome_id))).scalar_one()
    assert row.value_cents == 120_00
    assert row.succeeded is None, "money and success are different facts"


async def test_work_enqueued_inside_a_task_records_its_parent(session: AsyncSession) -> None:
    """End to end: the ambient link reaches the database, which is what makes the
    chain walkable at all."""
    with causality.running("t-parent"):
        task_id = await repository.enqueue_task(
            session,
            kind=TaskKind.BUILD_LANDING_PAGE,
            engine=EngineName.DEVELOPMENT,
            idempotency_key=f"test:{new_id()}",
            payload={"product_id": "p-1"},
        )
    await session.flush()
    assert task_id is not None
    from factory.state.orm import Task

    task = await session.get(Task, task_id)
    assert task is not None
    assert task.payload[causality.CAUSE_KEY] == "t-parent"


# ------------------------------------------- what a decision cost, against what it returned


async def _spend(session: AsyncSession, task_id: str, usd: float) -> None:
    from factory.state.orm import LLMCall

    session.add(
        LLMCall(
            id=new_id(),
            provider="anthropic",
            model="claude-opus-5",
            task_type="analysis",
            task_id=task_id,
            cost_usd=usd,
        )
    )
    await session.flush()


async def test_a_decision_is_charged_for_everything_it_set_in_motion(
    session: AsyncSession,
) -> None:
    """The first figure here that survives a change of method.

    Every metric the learning loop optimises is a SaaS funnel proxy and stops meaning
    anything the moment the company does something that is not a landing page. Cost
    against value does not: a freelance engagement and a subscription both reduce to
    it, which is what lets a general manager compare them at all.
    """
    await _chain(session, "t-decide", "t-build", "t-review")
    decision = await attribution.record_decision(
        session,
        kind="product_next",
        subject_kind="product",
        subject_id="p-1",
        choice="deployment",
        rationale="Nothing exists to put in front of anyone yet.",
        made_by_engine=EngineName.SUPERVISOR.value,
        made_by_task_id="t-decide",
    )
    # Spend at every depth of the subtree, and some on an unrelated task.
    await _spend(session, "t-decide", 0.40)
    await _spend(session, "t-build", 1.10)
    await _spend(session, "t-review", 0.25)
    await _spend(session, "t-unrelated", 99.00)

    await attribution.record_outcome(
        session,
        kind="revenue",
        value_cents=4900,
        task_id="t-review",
        credit_decisions=True,
    )

    got = await attribution.decision_return(session, decision)
    assert got.cost_cents == 175, "charged for its whole subtree and nothing else"
    assert got.value_cents == 4900
    assert got.net_cents == 4725
    assert got.tasks == 3
    assert got.measured is True


async def test_nothing_yet_is_distinguishable_from_returned_nothing(
    session: AsyncSession,
) -> None:
    """The instrumentation principle applied to this number. A decision made a minute
    ago and one that genuinely earned nothing both read as zero, and treating them
    alike is how a judgement gets made on the absence of evidence."""
    await _chain(session, "t-fresh")
    decision = await attribution.record_decision(
        session,
        kind="release",
        subject_kind="product",
        subject_id="p-1",
        choice="ship",
        rationale="Meets what it must do.",
        made_by_engine=EngineName.DEVELOPMENT.value,
        made_by_task_id="t-fresh",
    )
    fresh = await attribution.decision_return(session, decision)
    assert fresh.net_cents == 0
    assert fresh.measured is False, "one task, no spend, no outcome -- nothing observed"

    await _spend(session, "t-fresh", 0.02)
    spent = await attribution.decision_return(session, decision)
    assert spent.measured is True
    assert spent.net_cents == -2, "cost arrives before value, and the number says so"


async def test_a_decision_with_no_task_returns_an_empty_reading(
    session: AsyncSession,
) -> None:
    """Some decisions are recorded outside any task. That is a real state and must not
    raise on a read path a dashboard will call."""
    decision = await attribution.record_decision(
        session,
        kind="release",
        subject_kind="product",
        subject_id="p-1",
        choice="ship",
        rationale="Recorded without a task.",
        made_by_engine=EngineName.DEVELOPMENT.value,
        made_by_task_id=None,
    )
    got = await attribution.decision_return(session, decision)
    assert (got.cost_cents, got.value_cents, got.measured) == (0, 0, False)


async def test_the_subtree_walk_terminates_on_a_cycle(session: AsyncSession) -> None:
    """Same guard as `ancestors`, in the other direction: a bug that made the chain
    circular must not turn a read into a table scan."""
    await _chain(session, "t-a", "t-b")
    session.add(
        TaskEvent(
            id=new_id(),
            task_id="t-a",
            event="engine.start",
            engine=EngineName.DEVELOPMENT.value,
            detail={},
            caused_by_task_id="t-b",
        )
    )
    await session.flush()
    assert set(await attribution.descendants(session, "t-a")) == {"t-a", "t-b"}
