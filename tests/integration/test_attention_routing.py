"""A department can be told there is something it might own.

Design had no inbox: every task it ever ran was enqueued by its own 24-hour tick. On
2026-09-15 its manager woke at 00:00:07, saw a product with no guidelines, and
correctly commissioned nothing -- the page had shipped 94 minutes earlier. Its
judgement was right and its position in time was wrong.

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

from factory.facts import OWNER, Unmet
from factory.settings import EngineName
from factory.state import repository
from factory.state.orm import Base, Task
from factory.state.schemas import DesignGuidelines, ScoredIdea, TaskKind
from factory.supervisor.attention import route_attention

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


async def _manager_tasks(session: AsyncSession) -> list[Task]:
    rows = await session.execute(
        select(Task).where(Task.kind == TaskKind.DESIGN_MANAGE.value).order_by(Task.created_at)
    )
    return list(rows.scalars().all())


async def test_an_unmet_need_wakes_the_department_that_owns_it(session: AsyncSession) -> None:
    product_id = await _product(session)

    routed = await route_attention(session, product_id, asked_by="compliance")
    await session.flush()

    assert Unmet.DESIGN_GUIDELINES in routed
    tasks = await _manager_tasks(session)
    assert len(tasks) == 1
    assert tasks[0].engine == EngineName.DESIGN.value
    assert tasks[0].product_id == product_id
    assert tasks[0].payload["asked_by"] == "compliance"
    # The observation, not an instruction. A manager told to write guidelines has had
    # the decision made for it, which is the judgement this department exists for.
    assert "nobody has decided how this product should look" in tasks[0].payload["because"]
    assert "write" not in tasks[0].payload["because"].lower()


async def test_a_need_nobody_owns_is_left_unrouted_rather_than_reassigned(
    session: AsyncSession,
) -> None:
    """`AUDIENCE` has no department today. Handing it to the nearest one that could
    plausibly absorb it would make an unowned question look answered."""
    product_id = await _product(session)

    routed = await route_attention(session, product_id, asked_by="compliance")

    assert Unmet.AUDIENCE not in routed
    assert Unmet.AUDIENCE not in OWNER
    # Exactly one department was woken, not one per unmet need.
    assert len(await _manager_tasks(session)) == 1


async def test_a_met_need_wakes_nobody(session: AsyncSession) -> None:
    product_id = await _product(session)
    await repository.record_design_guidelines(
        session,
        product_id=product_id,
        guidelines=DesignGuidelines(
            direction="Borrow the language of ledgers: ruled columns, tabular figures.",
            palette=["#0b3d2e rule", "#f4f1ea paper", "#b8860b accent"],
            typography="IBM Plex Sans, with a monospace face for figures.",
            must_hold=["The page loads at least one webfont"],
        ),
    )
    await session.flush()

    routed = await route_attention(session, product_id, asked_by="compliance")

    assert Unmet.DESIGN_GUIDELINES not in routed
    assert await _manager_tasks(session) == []


async def test_asking_twice_about_the_same_thing_wakes_once(session: AsyncSession) -> None:
    """A manager that considered a subject and declined has decided. Asking again
    tomorrow is nagging, not routing -- its own periodic tick still sees the domain."""
    product_id = await _product(session)

    first = await route_attention(session, product_id, asked_by="compliance")
    await session.flush()
    second = await route_attention(session, product_id, asked_by="compliance")
    await session.flush()

    assert Unmet.DESIGN_GUIDELINES in first
    assert second == []
    assert len(await _manager_tasks(session)) == 1


async def test_the_inbox_wake_outranks_a_landing_page_build(session: AsyncSession) -> None:
    """Ordering preference, not a dependency: if the manager declines or fails, the
    build still happens. Lower priority runs first."""
    product_id = await _product(session)

    await route_attention(session, product_id, asked_by="compliance")
    await session.flush()

    tasks = await _manager_tasks(session)
    assert tasks[0].priority < 50


async def test_the_manager_is_woken_never_its_workers(session: AsyncSession) -> None:
    """You can ask a department to look at something; you cannot hand it its work.

    A company where any department can enqueue another's tasks is a pipeline whose
    edges happen to be spelled as function calls.
    """
    product_id = await _product(session)

    await route_attention(session, product_id, asked_by="compliance")
    await session.flush()

    rows = await session.execute(select(Task).where(Task.engine == EngineName.DESIGN.value))
    kinds = {t.kind for t in rows.scalars().all()}
    assert kinds == {TaskKind.DESIGN_MANAGE.value}
    assert TaskKind.WRITE_DESIGN_GUIDELINES.value not in kinds


async def test_a_product_that_does_not_exist_routes_nothing(session: AsyncSession) -> None:
    assert await route_attention(session, "0" * 32, asked_by="compliance") == []
