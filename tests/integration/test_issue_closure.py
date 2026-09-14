"""Issues close when the check that filed them stops finding them.

Needs Postgres::

    docker compose up -d postgres
    alembic upgrade head
    pytest tests/integration/test_issue_closure.py -q

This is the integration test for a production failure that cost four heal cycles
on one product and would have cost them on every product forever.

An issue is filed `open`, and the only thing that ever closed one was
`verify_unseen_issues`, which acts on issues already marked `fixed`. Nothing marks
a gap issue fixed -- a repair agent rewrites a file, the defect goes away, and the
row stays `open` for good. `product_progress.blocking_now` counts open blocking
issues, so it could rise and never fall, `converging` read `False` on every
attempt after the first, and `_heal` hit its wall and escalated every single time.

Observed exactly that way: product 06829d03 carried one `high` issue from
`contract_check` with `times_seen = 1`, while the contract check itself reported
zero violations on every subsequent run. One stale row, permanently blocking, and
two escalations of a product that had already met its requirements.

The fix is that a check which examines everything every run closes what it no
longer finds. These tests pin the behaviour that makes convergence measurable:
`blocking_now` must be able to reach zero.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from factory import issues
from factory.state import repository
from factory.state.orm import Base

DATABASE_URL = os.environ.get("DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not DATABASE_URL, reason="DATABASE_URL is not set; integration tests need Postgres"
)

PRODUCT = "prod-closure-test"


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


def _fingerprint(title: str) -> str:
    return issues.fingerprint_for(
        product_id=PRODUCT, kind="bug", location=f"contract: {title[:120]}", title=title
    )


async def _file(session: AsyncSession, title: str, severity: str = "high") -> None:
    await repository.record_issue(
        session,
        product_id=PRODUCT,
        kind="bug",
        severity=severity,
        title=title,
        location=f"contract: {title[:120]}",
        found_by="contract_check",
    )


async def test_a_check_that_stops_finding_a_defect_closes_it(session: AsyncSession) -> None:
    """The whole bug in one test: a clean run must clear the previous run's issue."""
    await _file(session, "no reference to the interest-capture URL")
    assert (await repository.product_progress(session, PRODUCT)).blocking_now == 1

    # The next run finds nothing -- the repair worked.
    closed = await repository.close_resolved_issues(
        session,
        product_id=PRODUCT,
        found_by="contract_check",
        still_present=set(),
        verified_by="contract_check",
    )

    assert closed == 1
    progress = await repository.product_progress(session, PRODUCT)
    assert progress.blocking_now == 0, (
        "blocking_now must be able to fall, or `converging` is False forever and "
        "every heal attempt after the first escalates into a wall"
    )


async def test_a_defect_still_present_is_not_closed(session: AsyncSession) -> None:
    """Closure is driven by what the run reported, not by the run having happened.

    The dangerous failure mode of the fix is the opposite of the bug: a pass that
    quietly clears real, still-broken findings and lets a product ship. What is
    still in the report stays open.
    """
    await _file(session, "still broken")
    await _file(session, "fixed since last time")

    closed = await repository.close_resolved_issues(
        session,
        product_id=PRODUCT,
        found_by="contract_check",
        still_present={_fingerprint("still broken")},
        verified_by="contract_check",
    )

    assert closed == 1
    open_titles = {i.title for i in await repository.open_issues_for(session, PRODUCT)}
    assert open_titles == {"still broken"}


async def test_one_source_does_not_close_another_sources_findings(
    session: AsyncSession,
) -> None:
    """A check may only close what it filed.

    The SEO check knowing nothing about a security finding is the point: silence
    from a check that never looks at something is not evidence about it. This is
    also why Bug Council is deliberately excluded from this mechanism entirely --
    it judges six categories by model, and its not mentioning something is not the
    same as its having looked and found nothing.
    """
    await _file(session, "a contract finding")
    await repository.record_issue(
        session,
        product_id=PRODUCT,
        kind="security",
        severity="high",
        title="a council finding",
        location="app/main.py",
        found_by="bug_council:security",
    )

    closed = await repository.close_resolved_issues(
        session,
        product_id=PRODUCT,
        found_by="contract_check",
        still_present=set(),
        verified_by="contract_check",
    )

    assert closed == 1
    open_titles = {i.title for i in await repository.open_issues_for(session, PRODUCT)}
    assert open_titles == {"a council finding"}


async def test_a_recurring_cosmetic_finding_is_not_a_wall(session: AsyncSession) -> None:
    """`stuck` means "we keep failing at the same blocker", not "this keeps coming up".

    `_heal` treats any `stuck` entry as a wall and escalates. Before the severity
    filter, a medium cosmetic finding seen three times was enough to do that on its
    own -- observed live as a hardcoded copyright year, seen four times, which the
    release judgement had twice weighed and deliberately accepted as a trade worth
    shipping with. Reading that as a wall inverts the decision that produced it: an
    issue nobody intends to fix recurring is evidence the judgement is being
    honoured, not evidence the approach is wrong.
    """
    for _ in range(4):
        await repository.record_issue(
            session,
            product_id=PRODUCT,
            kind="ui",
            severity="medium",
            title='Copyright notice hardcodes the literal year "(c) 2026"',
            location="index.html footer",
            found_by="bug_council:quality",
        )

    progress = await repository.product_progress(session, PRODUCT)
    assert progress.stuck == [], (
        "an accepted cosmetic trade must never read as a wall, or the product "
        "escalates forever on something nobody intends to fix"
    )
    assert progress.blocking_now == 0


async def test_a_recurring_blocker_is_still_a_wall(session: AsyncSession) -> None:
    """The signal the filter must not destroy: the same blocker, three passes running."""
    for _ in range(3):
        await _file(session, "checkout returns 422", severity="high")

    progress = await repository.product_progress(session, PRODUCT)
    assert progress.stuck == ["checkout returns 422"]
    assert progress.blocking_now == 1


async def test_zero_blockers_twice_is_done_not_a_wall(session: AsyncSession) -> None:
    """`0 < 0` is False, and reading that as "not converging" escalated a shipped build.

    Live sequence: gaps=0, `release.decided decision=ship`, and twelve seconds later
    `heal_stopped reason=not_converging blocking_now=0 blocking_before=0 stuck=0`.
    Nothing was wrong with the product. Holding at zero is the definition of done.

    This also covers the path with no issues at all: `_heal` handles deploy failures,
    which never produce issue rows, so `blocking_now` is structurally zero there. Left
    as False, every deploy failure walls on its first attempt and is never retried.
    """
    progress = await repository.product_progress(session, PRODUCT)
    assert progress.blocking_now == 0
    assert progress.converging is not False, (
        "zero blockers must never read as a wall -- it is the state a finished "
        "build is in, and the state every deploy failure is in"
    )


async def test_holding_steady_at_a_real_blocker_is_still_a_wall(
    session: AsyncSession,
) -> None:
    """The signal the zero-case must not destroy: same count, still broken."""
    await _file(session, "checkout returns 422", severity="high")
    progress = await repository.product_progress(session, PRODUCT)

    assert progress.blocking_now == 1
    # Simulate a previous attempt that also had one blocker.
    held = repository.ProductProgress(
        attempts=2,
        blocking_now=1,
        blocking_at_last_attempt=1,
        stuck=[],
        last_decision="fix_first",
    )
    assert held.converging is False, "one blocker before and one now is a wall"

    shrinking = repository.ProductProgress(
        attempts=2,
        blocking_now=1,
        blocking_at_last_attempt=3,
        stuck=[],
        last_decision="fix_first",
    )
    assert shrinking.converging is True
