"""The issue lifecycle: detected, assigned, fixed, and closed only when earned.

The whole table exists because "done" used to mean *the patch ran*. These tests
pin the distinction that replaces it — `fixed` is a claim, `closed` is a
conclusion, and only a later review failing to find the defect may promote one to
the other.

Needs Postgres:

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
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from factory.state import repository
from factory.state.orm import Base
from factory.state.schemas import ScoredIdea

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
    idea = ScoredIdea(
        problem_statement="iOS UI test suites exceed CI time limits and builds fail.",
        target_niche="issue-test",
        source_url="https://example.test/thread",
        monetization_hypothesis="USD 199/month per team.",
        pain_severity=0.8,
        willingness_to_pay=0.7,
        build_feasibility=0.6,
        adoption_ease=0.8,
    )
    idea_id = await repository.upsert_idea(session, idea)
    assert idea_id is not None
    return await repository.create_product(session, name="issue-test", idea_id=idea_id)


async def test_the_same_defect_reported_twice_is_one_issue(session: AsyncSession) -> None:
    """Production filed this exact bug four times over 24 days, twice inside one
    minute. One row, with a count."""
    product_id = await _product(session)

    first, is_new = await repository.record_issue(
        session,
        product_id=product_id,
        kind="bug",
        severity="critical",
        title="Subscribe button returns 422 and renders [object Object]",
        location="POST /api/billing/checkout",
        found_by="auditor",
    )
    assert is_new

    second, is_new_again = await repository.record_issue(
        session,
        product_id=product_id,
        kind="bug",
        severity="critical",
        title="The $199/mo checkout flow is broken on the billing endpoint",
        location="/api/billing/checkout",
        found_by="auditor",
    )
    await session.commit()

    assert not is_new_again
    assert second.id == first.id
    assert second.times_seen == 2


async def test_a_patch_shipping_marks_fixed_but_never_closed(session: AsyncSession) -> None:
    """The distinction the table exists for. A deploy is a claim about work, not
    evidence about the defect."""
    product_id = await _product(session)
    issue, _ = await repository.record_issue(
        session,
        product_id=product_id,
        kind="bug",
        severity="high",
        title="Checkout fails",
        location="/api/billing/checkout",
    )
    await repository.assign_issue_fix(
        session, issue.id, task_id="task-1", assigned_to="repair_engineer"
    )
    assert issue.status == "fixing"

    await repository.mark_issue_fixed(session, task_id="task-1", resolution="deployed")
    await session.commit()
    await session.refresh(issue)

    assert issue.status == "fixed"
    assert issue.fixed_at is not None
    assert issue.closed_at is None
    assert issue.verified_at is None


async def test_a_review_that_does_not_find_it_closes_it(session: AsyncSession) -> None:
    product_id = await _product(session)
    issue, _ = await repository.record_issue(
        session,
        product_id=product_id,
        kind="bug",
        severity="high",
        title="Checkout fails",
        location="/api/billing/checkout",
    )
    await repository.assign_issue_fix(
        session, issue.id, task_id="task-1", assigned_to="repair_engineer"
    )
    await repository.mark_issue_fixed(session, task_id="task-1", resolution="deployed")
    await session.commit()

    closed = await repository.verify_unseen_issues(
        session,
        product_id,
        review_started_at=datetime.now(UTC) + timedelta(seconds=1),
        reviewed_by="auditor:task-2",
    )
    await session.commit()
    await session.refresh(issue)

    assert closed == 1
    assert issue.status == "closed"
    assert issue.verified_at is not None


async def test_a_defect_still_there_is_never_closed(session: AsyncSession) -> None:
    """The failure this must not have. A patch shipped, the next review found the
    same defect anyway — closing it here would be the optimistic lie that let one
    broken checkout survive four repairs."""
    product_id = await _product(session)
    issue, _ = await repository.record_issue(
        session,
        product_id=product_id,
        kind="bug",
        severity="critical",
        title="Checkout fails",
        location="/api/billing/checkout",
    )
    await repository.assign_issue_fix(
        session, issue.id, task_id="task-1", assigned_to="repair_engineer"
    )
    await repository.mark_issue_fixed(session, task_id="task-1", resolution="deployed")
    await session.commit()

    review_started_at = datetime.now(UTC)

    # The next review reports it again: the repair did not work.
    again, is_new = await repository.record_issue(
        session,
        product_id=product_id,
        kind="bug",
        severity="critical",
        title="Subscribe still returns 422",
        location="POST /api/billing/checkout",
    )
    await session.commit()

    assert not is_new
    assert again.id == issue.id
    # Reopened, because a defect reported after a claimed repair means the repair
    # failed -- and leaving it `fixed` would let verification close it as gone.
    assert again.status == "open"

    closed = await repository.verify_unseen_issues(
        session, product_id, review_started_at=review_started_at, reviewed_by="auditor:task-2"
    )
    await session.commit()
    await session.refresh(issue)

    assert closed == 0
    assert issue.status == "open"
    assert issue.closed_at is None


async def test_a_closed_defect_coming_back_reopens_rather_than_duplicating(
    session: AsyncSession,
) -> None:
    """A regression is one issue with a history, not a second issue with none."""
    product_id = await _product(session)
    issue, _ = await repository.record_issue(
        session,
        product_id=product_id,
        kind="bug",
        severity="high",
        title="Checkout fails",
        location="/api/billing/checkout",
    )
    await repository.assign_issue_fix(
        session, issue.id, task_id="task-1", assigned_to="repair_engineer"
    )
    await repository.mark_issue_fixed(session, task_id="task-1", resolution="deployed")
    await session.commit()
    await repository.verify_unseen_issues(
        session,
        product_id,
        review_started_at=datetime.now(UTC) + timedelta(seconds=1),
        reviewed_by="auditor:task-2",
    )
    await session.commit()
    await session.refresh(issue)
    assert issue.status == "closed"

    returned, is_new = await repository.record_issue(
        session,
        product_id=product_id,
        kind="bug",
        severity="high",
        title="Checkout fails again after the refactor",
        location="/api/billing/checkout",
    )
    await session.commit()

    assert not is_new
    assert returned.id == issue.id
    assert returned.status == "open"
    assert returned.times_seen == 2
    assert returned.closed_at is None


async def test_the_backlog_is_ordered_worst_first(session: AsyncSession) -> None:
    """This is what a manager reads. Severity has to lead, or the ordering is just
    an accident of when things were found."""
    product_id = await _product(session)
    for severity, location in (
        ("low", "/about"),
        ("critical", "/api/billing/checkout"),
        ("medium", "/pricing"),
        ("high", "/auth/register"),
    ):
        await repository.record_issue(
            session,
            product_id=product_id,
            kind="bug",
            severity=severity,
            title=f"defect at {location}",
            location=location,
        )
    await session.commit()

    backlog = await repository.open_issues_for(session, product_id)
    assert [i.severity for i in backlog] == ["critical", "high", "medium", "low"]
