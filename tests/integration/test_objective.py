"""The objective lives in one row, and a change is never silent.

Phase 5a (`ARCHITECTURE_ROADMAP.md`). Today the objective is held by discipline --
everyone in a conversation agreeing not to touch it -- which nothing in the code
would notice drifting. This table is the fix: one live row, superseded rather than
overwritten, exactly like `requirements`.

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


class TestNoObjectiveYet:
    async def test_live_objective_is_none_before_anything_is_set(
        self, session: AsyncSession
    ) -> None:
        """A fresh table (this test's own `Base.metadata.create_all`, not the
        seeded migration) has no live row. A strategic review reading `None` must
        be able to tell "never set" apart from every later state on its own."""
        assert await repository.live_objective(session) is None


class TestRecordAndSupersede:
    async def test_recording_one_makes_it_live(self, session: AsyncSession) -> None:
        await repository.record_objective(
            session, statement="Maximise sustainable long-term profit.", set_by="jesse"
        )
        live = await repository.live_objective(session)
        assert live is not None
        assert live.statement == "Maximise sustainable long-term profit."
        assert live.set_by == "jesse"
        assert live.superseded_at is None

    async def test_a_second_record_supersedes_the_first_rather_than_overwriting_it(
        self, session: AsyncSession
    ) -> None:
        """The property the whole table exists for: a change produces a new row
        and marks the old one superseded, rather than rewriting it in place -- the
        same discipline `record_requirements` already holds for a build standard."""
        first_id = await repository.record_objective(
            session, statement="Version one.", set_by="jesse"
        )
        await repository.record_objective(
            session, statement="Version two.", set_by="jesse", reason="sharpened wording"
        )

        live = await repository.live_objective(session)
        assert live is not None
        assert live.statement == "Version two."
        assert live.reason == "sharpened wording"

        from sqlalchemy import select

        from factory.state.orm import Objective

        first_row = (
            await session.execute(select(Objective).where(Objective.id == first_id))
        ).scalar_one()
        assert first_row.superseded_at is not None
        assert first_row.statement == "Version one."

    async def test_only_one_row_is_ever_live_at_once(self, session: AsyncSession) -> None:
        """A strategic review's query (`superseded_at IS NULL`) must never be able
        to return more than one row -- the failure mode that would make "the"
        objective ambiguous."""
        from sqlalchemy import func, select

        from factory.state.orm import Objective

        await repository.record_objective(session, statement="A", set_by="jesse")
        await repository.record_objective(session, statement="B", set_by="jesse")
        await repository.record_objective(session, statement="C", set_by="jesse")

        live_count = (
            await session.execute(
                select(func.count()).select_from(Objective).where(Objective.superseded_at.is_(None))
            )
        ).scalar_one()
        assert live_count == 1

    async def test_reason_is_optional(self, session: AsyncSession) -> None:
        """Not every change needs a stated reason to be recorded -- the founding
        row itself has one, but the field must not be required to write at all."""
        await repository.record_objective(session, statement="No reason given.", set_by="jesse")
        live = await repository.live_objective(session)
        assert live is not None
        assert live.reason is None
