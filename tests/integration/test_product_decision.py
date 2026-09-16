"""What a product is owed next, decided rather than assumed.

Compliance used to create a product and commission a landing-page build in the same
transaction, so no moment existed at which anyone could judge whether the product was
ready to be built. These assert that the moment now exists, that the decision is what
commissions work, and -- the one that matters -- that a build happens only because
somebody judged it should.

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

from factory.facts import Unmet, product_facts
from factory.hitl.telegram import NullNotifier
from factory.llm.client import LLMClient
from factory.llm.providers.fake import FakeProvider
from factory.llm.router import ProviderRegistry, provider_for
from factory.sandbox.disabled import DisabledSandbox
from factory.settings import EngineName, Settings, TaskType
from factory.state import repository
from factory.state.orm import Base, Task, new_id
from factory.state.schemas import ProductNext, ScoredIdea, TaskKind, TaskStatus
from factory.supervisor.context import FactoryContext
from factory.supervisor.product_decision import decide_next

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


def _ctx(tmp_path: Path, decision: ProductNext | None) -> tuple[FactoryContext, FakeProvider]:
    settings = Settings(
        database_url=DATABASE_URL, gemini_api_key="test-key", workspace_root=tmp_path
    )
    # Registered against whoever the router sends ANALYSIS to, so a routing change
    # moves the fake with it rather than silently un-testing this.
    name = provider_for(TaskType.ANALYSIS, settings)
    provider = FakeProvider(name=name)
    if decision is not None:
        provider.register(ProductNext, decision)
    llm = LLMClient(ProviderRegistry({name: provider}), settings=settings)
    return (
        FactoryContext(
            settings=settings,
            llm=llm,
            notifier=NullNotifier(),
            sandbox=DisabledSandbox(),
            enabled_engines={EngineName.SUPERVISOR, EngineName.DESIGN, EngineName.DEVELOPMENT},
            worker_id="product-decision-test",
        ),
        provider,
    )


async def _product(session: AsyncSession) -> str:
    idea = await repository.upsert_idea(
        session,
        ScoredIdea(
            problem_statement="Bookkeepers reconcile invoices by hand every week.",
            target_niche="Bookkeepers at small Dutch accounting firms",
            source_url="https://example.test/thread",
            monetization_hypothesis="EUR 49/month per seat.",
            pain_severity=0.8,
            willingness_to_pay=0.7,
            build_feasibility=0.6,
            adoption_ease=0.8,
        ),
    )
    assert idea is not None
    return await repository.create_product(session, name="Shardline", idea_id=idea)


def _task(product_id: str) -> Task:
    return Task(
        id=new_id(),
        kind=TaskKind.DECIDE_PRODUCT_NEXT.value,
        engine=EngineName.SUPERVISOR.value,
        status=TaskStatus.CLAIMED.value,
        priority=40,
        payload={"product_id": product_id},
        product_id=product_id,
        attempt_count=1,
    )


async def _tasks_of(session: AsyncSession, kind: TaskKind) -> list[Task]:
    rows = await session.execute(select(Task).where(Task.kind == kind.value))
    return list(rows.scalars().all())


async def test_deciding_to_wait_commissions_nothing(session: AsyncSession, tmp_path: Path) -> None:
    """The half of the decision that used to be impossible to express.

    "Not yet, because the builder would be working blind" was not a thing this system
    could say: the build was already queued before anyone looked.
    """
    product_id = await _product(session)
    ctx, _ = _ctx(
        tmp_path,
        ProductNext(
            assessment="Nobody has decided how this should look, so building now means "
            "inventing a look and then being judged against a standard nobody wrote.",
            pursue=[Unmet.DESIGN_GUIDELINES.value],
            holding="the build waits until there is a standard to build to",
        ),
    )

    await decide_next(ctx, session, _task(product_id))
    await session.flush()

    assert await _tasks_of(session, TaskKind.BUILD_LANDING_PAGE) == []
    assert len(await _tasks_of(session, TaskKind.DESIGN_MANAGE)) == 1


async def test_a_build_happens_only_because_it_was_judged(
    session: AsyncSession, tmp_path: Path
) -> None:
    """The same facts, the opposite decision -- which is the point of it being one."""
    product_id = await _product(session)
    ctx, _ = _ctx(
        tmp_path,
        ProductNext(
            assessment="A cheap validation page for an unproven idea; the house style "
            "is good enough to find out whether anybody wants this at all.",
            pursue=[Unmet.DEPLOYMENT.value],
            holding="design guidelines are not worth commissioning before demand exists",
        ),
    )

    await decide_next(ctx, session, _task(product_id))
    await session.flush()

    builds = await _tasks_of(session, TaskKind.BUILD_LANDING_PAGE)
    assert len(builds) == 1
    assert builds[0].payload["product_id"] == product_id
    assert await _tasks_of(session, TaskKind.DESIGN_MANAGE) == []


async def test_ordering_is_not_written_down_anywhere(session: AsyncSession, tmp_path: Path) -> None:
    """Both orders are reachable from identical facts.

    If design-before-development were a rule, one of these two tests could not pass.
    The two above differ only in what the decider concluded.
    """
    product_id = await _product(session)
    facts = await product_facts(session, product_id)
    assert facts is not None
    assert Unmet.DESIGN_GUIDELINES in facts.unmet
    assert Unmet.DEPLOYMENT in facts.unmet

    ctx, _ = _ctx(
        tmp_path,
        ProductNext(
            assessment="Both are missing and both are worth starting now; the page is "
            "cheap and the standard can land before the first review.",
            pursue=[Unmet.DESIGN_GUIDELINES.value, Unmet.DEPLOYMENT.value],
        ),
    )

    await decide_next(ctx, session, _task(product_id))
    await session.flush()

    assert len(await _tasks_of(session, TaskKind.BUILD_LANDING_PAGE)) == 1
    assert len(await _tasks_of(session, TaskKind.DESIGN_MANAGE)) == 1


async def test_an_unowned_need_cannot_be_pursued(session: AsyncSession, tmp_path: Path) -> None:
    """Naming it is how the company finds out it needs somebody to own it. Acting on
    it would require inventing a department, which is how an unowned question starts
    looking answered."""
    product_id = await _product(session)
    ctx, _ = _ctx(
        tmp_path,
        ProductNext(
            assessment="We do not know who this is for, and that is the thing most "
            "worth fixing before anything else is built.",
            pursue=[Unmet.AUDIENCE.value],
        ),
    )

    result = await decide_next(ctx, session, _task(product_id))
    await session.flush()

    assert "nothing new commissioned" in result
    assert await _tasks_of(session, TaskKind.BUILD_LANDING_PAGE) == []
    assert await _tasks_of(session, TaskKind.DESIGN_MANAGE) == []


async def test_a_decision_naming_something_that_is_not_a_need_shrinks(
    session: AsyncSession, tmp_path: Path
) -> None:
    """A decision that cannot be executed as written should shrink, never be
    reinterpreted into whatever is nearest."""
    product_id = await _product(session)
    ctx, _ = _ctx(
        tmp_path,
        ProductNext(
            assessment="This product needs marketing attention more than anything else "
            "that is currently missing from it.",
            pursue=["launch_campaign", Unmet.DESIGN_GUIDELINES.value],
        ),
    )

    await decide_next(ctx, session, _task(product_id))
    await session.flush()

    assert len(await _tasks_of(session, TaskKind.DESIGN_MANAGE)) == 1
    assert await _tasks_of(session, TaskKind.BUILD_LANDING_PAGE) == []


async def test_a_decider_that_cannot_think_commissions_nothing(
    session: AsyncSession, tmp_path: Path
) -> None:
    """The default that caused all of this was "build it anyway"."""
    product_id = await _product(session)
    ctx, _ = _ctx(tmp_path, None)  # nothing registered: the parse raises

    result = await decide_next(ctx, session, _task(product_id))
    await session.flush()

    assert "could not decide" in result
    assert await _tasks_of(session, TaskKind.BUILD_LANDING_PAGE) == []
    assert await _tasks_of(session, TaskKind.DESIGN_MANAGE) == []


async def test_deciding_twice_does_not_build_twice(session: AsyncSession, tmp_path: Path) -> None:
    product_id = await _product(session)
    decision = ProductNext(
        assessment="A cheap validation page is the fastest way to learn whether anyone "
        "wants this, and nothing else is worth spending on yet.",
        pursue=[Unmet.DEPLOYMENT.value],
    )
    ctx, provider = _ctx(tmp_path, decision)
    provider.register(ProductNext, decision)

    await decide_next(ctx, session, _task(product_id))
    await session.flush()
    await decide_next(ctx, session, _task(product_id))
    await session.flush()

    assert len(await _tasks_of(session, TaskKind.BUILD_LANDING_PAGE)) == 1
