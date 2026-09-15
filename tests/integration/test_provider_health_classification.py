"""What counts as evidence that a provider is *down*.

The breaker answers exactly one question -- is this vendor wholly, persistently
gone -- and it used to answer it with "how many calls had outcome != ok", which
quietly folded in a completely different kind of failure.

Observed live within an hour of the DeepSeek POC starting: `deepseek-flash`
returned HTTP 200 to every request and missed the required schema on most
orchestration and analysis calls. The breaker read "8 call(s) in the last 30m,
none succeeded", declared a sustained outage, and paused DeepSeek -- which under
POC routing is nearly every task type. The factory stopped, with a provider that
was answering and billing normally.

That is a deadlock, not a pause: waiting cannot fix a model that does not hold a
schema, so the breaker would never clear on its own.

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

from factory.llm.health import tripped_providers
from factory.llm.providers.base import Provider
from factory.state.orm import Base, LLMCall

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


async def _calls(session: AsyncSession, outcome: str, count: int) -> None:
    for _ in range(count):
        session.add(
            LLMCall(
                provider=Provider.DEEPSEEK.value,
                model="deepseek-flash",
                task_type="analysis",
                outcome=outcome,
                input_tokens=10,
                output_tokens=10,
                cost_usd=0.0,
                latency_ms=100,
            )
        )
    await session.flush()


async def test_a_provider_that_cannot_be_reached_trips_the_breaker(
    session: AsyncSession,
) -> None:
    """The case the breaker is for, and it must keep working."""
    await _calls(session, "error", 10)
    await session.commit()

    tripped = await tripped_providers(session)

    assert Provider.DEEPSEEK in tripped


async def test_schema_violations_never_trip_the_breaker(session: AsyncSession) -> None:
    """The live deadlock. Every one of these calls was answered and billed -- the
    provider is demonstrably up. Pausing it changes nothing, because the model will
    miss the same schema whenever it resumes."""
    await _calls(session, "schema_violation", 10)
    await _calls(session, "schema_violation_repaired", 6)
    await session.commit()

    tripped = await tripped_providers(session)

    assert Provider.DEEPSEEK not in tripped


async def test_a_real_outage_is_still_caught_underneath_schema_noise(
    session: AsyncSession,
) -> None:
    """Excluding schema failures must not blind the breaker to a genuine outage
    happening at the same time -- the transport evidence still has to be judged on
    its own."""
    await _calls(session, "schema_violation", 20)
    await _calls(session, "error", 9)
    await session.commit()

    tripped = await tripped_providers(session)

    assert Provider.DEEPSEEK in tripped


async def test_schema_failures_do_not_dilute_the_evidence_threshold(
    session: AsyncSession,
) -> None:
    """MIN_CALLS exists so one bad call on a quiet provider proves nothing. Schema
    violations must not pad that population either -- two transport errors buried in
    thirty schema failures is still a quiet provider, not an outage."""
    await _calls(session, "schema_violation", 30)
    await _calls(session, "error", 2)
    await session.commit()

    tripped = await tripped_providers(session)

    assert Provider.DEEPSEEK not in tripped


async def test_going_quiet_is_not_recovering(session: AsyncSession) -> None:
    """The bug that told the operator "gemini is answering again" seven times.

    `tripped_providers` correctly returns nothing for a provider with too few calls
    to judge -- a handful of failures on a quiet provider proves nothing. The caller
    used to compute recovery as `previous - tripped`, and plain subtraction cannot
    tell "answering again" from "too quiet to judge".

    Gemini ran that way for three days: credits depleted, **zero successful calls
    ever**, and few enough calls per hour that it kept dropping out of the 30-minute
    window. Eight outage escalations were raised and seven auto-closed, each closure
    wiping the evidence -- so a total outage read as a series of brief blips that all
    fixed themselves.
    """
    from factory.llm.health import recovered_providers

    # Dead, and now quiet: not enough evidence to stay tripped, and no successes.
    await _calls(session, "error", 2)
    await session.commit()

    assert await tripped_providers(session) == {}, "too quiet to judge, correctly"
    assert await recovered_providers(session, {Provider.DEEPSEEK}) == set(), (
        "silence is not recovery"
    )


async def test_recovery_needs_real_successes(session: AsyncSession) -> None:
    """One success is not enough to clear, by the same asymmetry that trips it.

    A provider genuinely back up produces successes easily; a flapping one does not.
    """
    from factory.llm.health import MIN_RECOVERY_SUCCESSES, recovered_providers

    await _calls(session, "ok", MIN_RECOVERY_SUCCESSES - 1)
    await session.commit()
    assert await recovered_providers(session, {Provider.DEEPSEEK}) == set()

    await _calls(session, "ok", 1)
    await session.commit()
    assert await recovered_providers(session, {Provider.DEEPSEEK}) == {Provider.DEEPSEEK}


async def test_nothing_was_tripped_so_nothing_can_recover(session: AsyncSession) -> None:
    """A provider that was never down must not be announced as recovered."""
    from factory.llm.health import recovered_providers

    await _calls(session, "ok", 20)
    await session.commit()
    assert await recovered_providers(session, set()) == set()
