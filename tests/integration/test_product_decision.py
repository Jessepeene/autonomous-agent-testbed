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


async def test_the_goal_is_stated_as_the_goal_not_listed_as_a_gap(
    session: AsyncSession,
) -> None:
    """Run 10: "the more urgent blocker is that nobody outside the company has
    encountered this product yet." It read the objective as an obstacle, which is a
    sentence no employee of a real company could say -- nobody is blocked on having
    customers. `reach` belongs in the statement of what the product is for."""
    from factory.supervisor.product_decision import _render_options

    product_id = await _product(session)
    facts = await product_facts(session, product_id)
    assert facts is not None
    assert Unmet.REACH in facts.unmet  # still a true fact, still a good diagnostic

    rendered = _render_options(facts)
    assert "What this product is for" in rendered
    assert "will pay for it" in rendered
    # ...and it is not offered as a thing to go and start.
    startable = rendered.split("What can be started right now:")[-1]
    assert "- reach:" not in startable


async def test_a_need_nobody_owns_never_reaches_the_decider(session: AsyncSession) -> None:
    """It is a staffing fact, not a product one. Shown as something it could not
    pursue, a reasonable decider concludes the product is stuck -- runs 8 and 10."""
    from factory.state.orm import Interrupt
    from factory.supervisor.product_decision import _raise_staffing_gap, _render_options

    product_id = await _product(session)
    facts = await product_facts(session, product_id)
    assert facts is not None

    assert "audience" not in _render_options(facts)

    # It goes to a person instead, once, however many products hit it.
    await _raise_staffing_gap(session, facts)
    await _raise_staffing_gap(session, facts)
    await session.flush()
    raised = list((await session.execute(select(Interrupt))).scalars().all())
    assert [i.subject for i in raised] == ["Nobody owns audience"]


async def test_requirements_read_as_pending_work_not_as_a_wall(session: AsyncSession) -> None:
    """Requirements are written by the builder as its first step.

    Listed among the needs nobody owns, that read as an unclearable blocker: run 8's
    decider concluded "the product needs a requirements specification... nobody is
    currently responsible for" it and commissioned nothing. The menu has to say how
    they actually come to exist, or a decider reasoning "requirements first" -- which
    is a perfectly sensible order -- stalls the product every time it thinks of it.
    """
    from factory.supervisor.product_decision import _render_options

    product_id = await _product(session)
    facts = await product_facts(session, product_id)
    assert facts is not None
    assert Unmet.REQUIREMENTS in facts.unmet

    rendered = _render_options(facts)
    requirements_line = next(
        line for line in rendered.splitlines() if line.strip().startswith("- requirements:")
    )
    assert "commission the build" in requirements_line
    # It is not offered as something to go and start on its own...
    startable = rendered.split("What can be started right now:")[-1].split("Comes with")[0]
    assert "- requirements:" not in startable
    # ...and there is no longer any section telling the decider what it cannot do.
    assert "nobody owns" not in rendered.lower()
    assert "cannot pursue" not in rendered.lower()


def test_the_menu_matches_what_can_actually_be_commissioned() -> None:
    """Every need the decider is offered must be one the code can carry out.

    These were two derivations of one question and they disagreed: the menu came from
    OWNER, which has no entry for DEPLOYMENT because Development has no manager, while
    `_commission` starts a build directly. So a decider was shown "nobody owns
    deployment", correctly concluded it could pursue nothing, and the product stalled
    with a build one function call away.
    """
    from factory.supervisor.product_decision import _pursuable

    # Anything _commission can act on must be offered, and nothing else may be.
    assert _pursuable(Unmet.DEPLOYMENT) is True, "_commission starts builds; the menu must say so"
    assert _pursuable(Unmet.DESIGN_GUIDELINES) is True
    assert _pursuable(Unmet.AUDIENCE) is False, "genuinely unowned; must not be offered"


async def test_a_product_with_only_a_build_left_still_gets_built(
    session: AsyncSession, tmp_path: Path
) -> None:
    """The state run 7 stalled in: guidelines written, build not yet started."""
    from factory.state.schemas import DesignGuidelines

    product_id = await _product(session)
    await repository.record_design_guidelines(
        session,
        product_id=product_id,
        guidelines=DesignGuidelines(
            direction="Borrow the language of a bakery's morning production sheet.",
            palette=["#2b1a12 crust", "#f5ead6 flour", "#c2703d bake"],
            typography="A warm grotesque for headings, tabular figures for counts.",
            must_hold=["The page loads at least one webfont"],
        ),
    )
    await session.flush()

    ctx, _ = _ctx(
        tmp_path,
        ProductNext(
            assessment="The standard exists now, so a builder has something to aim at "
            "and the page is the only way to find out whether anyone wants this.",
            pursue=[Unmet.DEPLOYMENT.value],
        ),
    )
    await decide_next(ctx, session, _task(product_id))
    await session.flush()

    assert len(await _tasks_of(session, TaskKind.BUILD_LANDING_PAGE)) == 1


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
