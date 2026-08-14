"""finance/churn.py: turning subscription churn into Discovery-readable memory,
and the review-churn maintenance item.

Needs Postgres:

    docker compose up -d postgres
    alembic upgrade head
    pytest tests/integration -q
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from factory.engines.finance.churn import review_churn, write_churn_lessons
from factory.hitl.telegram import NullNotifier
from factory.llm.client import LLMClient
from factory.llm.providers.base import AgentTurn, Provider, Usage
from factory.llm.providers.fake import FakeProvider
from factory.llm.router import ProviderRegistry
from factory.memory import store as memory_store
from factory.sandbox.disabled import DisabledSandbox
from factory.settings import EngineName, Settings
from factory.state import repository
from factory.state.orm import Base, Memory, Task, new_id
from factory.state.schemas import ScoredIdea, TaskKind, TaskStatus
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


@pytest_asyncio.fixture(autouse=True)
async def _reset_global_engine() -> AsyncIterator[None]:
    yield
    from factory.state import db as db_module

    await db_module.dispose_engine()


def _ctx(*, workspace_root: Path, gemini: FakeProvider) -> FactoryContext:
    settings = Settings(
        database_url=DATABASE_URL,
        gemini_api_key="test-key",
        workspace_root=workspace_root,
        factory_dry_run=False,
    )
    llm = LLMClient(
        ProviderRegistry({Provider.GEMINI: gemini}), settings=settings, backoff_base=0.0
    )
    return FactoryContext(
        settings=settings,
        llm=llm,
        notifier=NullNotifier(),
        sandbox=DisabledSandbox(),
        enabled_engines={EngineName.FINANCE},
        worker_id="churn-review-test",
    )


def _maintenance_task() -> Task:
    return Task(
        id=new_id(),
        kind=TaskKind.MAINTENANCE.value,
        engine=EngineName.FINANCE.value,
        status=TaskStatus.CLAIMED.value,
        priority=500,
        payload={"slug": "review-churn"},
        attempt_count=1,
    )


def _no_action_turn() -> AgentTurn:
    return AgentTurn(
        text="Traffic and conversion both look normal; no action taken.",
        tool_calls=[],
        stop_reason="end_turn",
        provider=Provider.GEMINI,
        model="gemini-2.5-flash",
        usage=Usage(input_tokens=10, output_tokens=10),
    )


async def _make_product(session: AsyncSession, niche: str) -> str:
    idea = ScoredIdea(
        problem_statement="Bookkeepers reconcile invoices by hand every week.",
        target_niche=niche,
        source_url="https://example.test/thread",
        monetization_hypothesis="Bookkeepers pay EUR 49/month per seat.",
        pain_severity=0.8,
        willingness_to_pay=0.7,
        build_feasibility=0.6,
        adoption_ease=0.8,
    )
    idea_id = await repository.upsert_idea(session, idea)
    assert idea_id is not None
    return await repository.create_product(session, name=niche, idea_id=idea_id)


async def _churned_subscription(
    session: AsyncSession,
    *,
    stripe_subscription_id: str,
    product_id: str | None,
    churn_kind: str,
) -> str:
    customer_id = await repository.upsert_customer(
        session, stripe_customer_id=f"cus_{stripe_subscription_id}", product_id=product_id
    )
    subscription_id = await repository.upsert_subscription(
        session,
        stripe_subscription_id=stripe_subscription_id,
        customer_id=customer_id,
        product_id=product_id,
        status="active",
        price_cents=4900,
        cancel_at_period_end=False,
        current_period_end=None,
    )
    await repository.mark_subscription_churned(
        session,
        stripe_subscription_id=stripe_subscription_id,
        status="canceled",
        churn_kind=churn_kind,
    )
    return subscription_id


# --- write_churn_lessons ------------------------------------------------------------


async def test_involuntary_and_voluntary_churn_get_distinctly_worded_lessons(
    session: AsyncSession,
) -> None:
    # Deliberately not "involuntary-churn-niche" / "voluntary-churn-niche" --
    # "involuntary" contains "voluntary" as a literal substring, so an `in`
    # check for the second niche would also match the first row's content.
    involuntary_product = await _make_product(session, "card-declined-niche")
    voluntary_product = await _make_product(session, "canceled-by-choice-niche")
    await _churned_subscription(
        session,
        stripe_subscription_id="sub_involuntary",
        product_id=involuntary_product,
        churn_kind="involuntary",
    )
    await _churned_subscription(
        session,
        stripe_subscription_id="sub_voluntary",
        product_id=voluntary_product,
        churn_kind="voluntary",
    )
    await session.commit()

    written = await write_churn_lessons(session)
    await session.commit()

    assert written == 2
    # Query the Memory table directly rather than through memory_store.recall():
    # recall()'s lexical relevance score is a Jaccard *ratio* over the whole
    # content, so a single matching token (even a distinctive hyphenated one)
    # against ~35 other tokens in a full sentence can land below
    # MIN_RELEVANCE even though the token is genuinely present -- recall()'s
    # threshold is tuned for "is this plausibly relevant to a real query", not
    # "assert this exact content was written", which is what this test needs.
    scope = memory_store.scope_for(engine=EngineName.DISCOVERY)
    rows = (
        (
            await session.execute(
                select(Memory).where(Memory.scope == scope, Memory.superseded_at.is_(None))
            )
        )
        .scalars()
        .all()
    )
    involuntary_lesson = next(m for m in rows if "card-declined-niche" in m.content)
    voluntary_lesson = next(m for m in rows if "canceled-by-choice-niche" in m.content)
    assert "involuntarily" in involuntary_lesson.content
    assert "payment failure" in involuntary_lesson.content
    assert "voluntarily" in voluntary_lesson.content
    # The whole point of Round 3's revision: voluntary churn is framed as
    # stronger evidence, not discarded, and not identically worded to the
    # involuntary case.
    assert involuntary_lesson.content != voluntary_lesson.content


async def test_unattributed_churn_is_skipped_but_still_marked_processed(
    session: AsyncSession,
) -> None:
    """No product_id means no niche to name -- nothing meaningful to tell
    Discovery -- but it must not be rescanned forever either."""
    from factory.state.orm import Subscription

    subscription_id = await _churned_subscription(
        session, stripe_subscription_id="sub_orphan", product_id=None, churn_kind="voluntary"
    )
    await session.commit()

    written = await write_churn_lessons(session)
    await session.commit()

    assert written == 0
    row = await session.get(Subscription, subscription_id)
    assert row is not None
    assert row.memory_written_at is not None


async def test_write_churn_lessons_is_idempotent(session: AsyncSession) -> None:
    product_id = await _make_product(session, "idempotent-churn-niche")
    await _churned_subscription(
        session, stripe_subscription_id="sub_once", product_id=product_id, churn_kind="voluntary"
    )
    await session.commit()

    first = await write_churn_lessons(session)
    await session.commit()
    second = await write_churn_lessons(session)
    await session.commit()

    assert first == 1
    assert second == 0


# --- review_churn: the maintenance item ----------------------------------------------


async def test_review_churn_spawns_growth_analyst_for_a_churning_product(
    session: AsyncSession, tmp_path: Path
) -> None:
    product_id = await _make_product(session, "spawn-churn-niche")
    await _churned_subscription(
        session, stripe_subscription_id="sub_spawn", product_id=product_id, churn_kind="voluntary"
    )
    await session.commit()

    provider = FakeProvider(name=Provider.GEMINI)
    provider.register_turns(_no_action_turn())
    ctx = _ctx(workspace_root=tmp_path, gemini=provider)

    result = await review_churn(ctx, session, _maintenance_task())

    assert "1 lesson(s) written" in result
    assert "reviewed 1/1" in result
    assert len(provider.conversations) == 1


async def test_review_churn_with_nothing_churning_never_spawns_anything(
    session: AsyncSession, tmp_path: Path
) -> None:
    provider = FakeProvider(name=Provider.GEMINI)
    # Deliberately no registered turn -- FakeProvider raises if converse() is
    # ever called, so this is a real assertion, not just an unused fixture.
    ctx = _ctx(workspace_root=tmp_path, gemini=provider)

    result = await review_churn(ctx, session, _maintenance_task())

    assert "no products currently churning" in result
    assert provider.conversations == []
