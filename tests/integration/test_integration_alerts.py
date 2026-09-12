"""`IntegrationAlert`: usage-limit/quota signals from third-party providers,
surfaced on the dashboard's Integrations page. Deliberately separate from
`Interrupt` -- see `state/orm.py::IntegrationAlert`'s docstring -- but the
same collapse-while-open dedup shape as `uq_interrupts_open_subject`
(0017_interrupt_open_subject_partial.py), for the identical reason: a
provider that keeps hitting the same limit on retry must not spam a fresh
row every time.

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

from factory.observability import integrations as integrations_metrics
from factory.settings import Settings
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


async def test_raise_integration_alert_returns_a_new_id(session: AsyncSession) -> None:
    alert_id = await repository.raise_integration_alert(
        session, provider="hunter.io", message="usage limit reached"
    )

    assert alert_id is not None


async def test_a_repeated_identical_open_alert_collapses_into_one(session: AsyncSession) -> None:
    """The actual point of the dedup: a provider hit on every retry of the same
    harvest cycle must not spam a fresh row each time."""
    first_id = await repository.raise_integration_alert(
        session, provider="hunter.io", message="usage limit reached"
    )
    assert first_id is not None

    second_id = await repository.raise_integration_alert(
        session, provider="hunter.io", message="usage limit reached"
    )

    assert second_id is None
    open_alerts = await repository.list_open_integration_alerts(session)
    assert len([a for a in open_alerts if a.id == first_id]) == 1


async def test_a_resolved_alert_does_not_block_a_later_genuinely_new_one(
    session: AsyncSession,
) -> None:
    first_id = await repository.raise_integration_alert(
        session, provider="hunter.io", message="usage limit reached"
    )
    assert first_id is not None
    resolved = await repository.resolve_integration_alert(session, first_id)
    assert resolved is True

    second_id = await repository.raise_integration_alert(
        session, provider="hunter.io", message="usage limit reached"
    )

    assert second_id is not None
    assert second_id != first_id


async def test_different_providers_never_collapse_into_each_other(session: AsyncSession) -> None:
    hunter_id = await repository.raise_integration_alert(
        session, provider="hunter.io", message="usage limit reached"
    )
    apollo_id = await repository.raise_integration_alert(
        session, provider="apollo.io", message="usage limit reached"
    )

    assert hunter_id is not None
    assert apollo_id is not None
    assert hunter_id != apollo_id


async def test_list_open_integration_alerts_excludes_resolved_rows(session: AsyncSession) -> None:
    open_id = await repository.raise_integration_alert(
        session, provider="hunter.io", message="still open"
    )
    resolved_id = await repository.raise_integration_alert(
        session, provider="apollo.io", message="already handled"
    )
    assert open_id is not None
    assert resolved_id is not None
    await repository.resolve_integration_alert(session, resolved_id)

    open_alerts = await repository.list_open_integration_alerts(session)

    ids = {a.id for a in open_alerts}
    assert open_id in ids
    assert resolved_id not in ids


async def test_count_open_integration_alerts_reflects_only_open_rows(session: AsyncSession) -> None:
    assert await repository.count_open_integration_alerts(session) == 0

    first_id = await repository.raise_integration_alert(
        session, provider="hunter.io", message="first"
    )
    await repository.raise_integration_alert(session, provider="apollo.io", message="second")
    assert await repository.count_open_integration_alerts(session) == 2

    assert first_id is not None
    await repository.resolve_integration_alert(session, first_id)
    assert await repository.count_open_integration_alerts(session) == 1


async def test_resolve_integration_alert_returns_false_for_an_unknown_id(
    session: AsyncSession,
) -> None:
    assert await repository.resolve_integration_alert(session, "not-a-real-id") is False


async def test_resolve_integration_alert_returns_false_for_an_already_resolved_id(
    session: AsyncSession,
) -> None:
    alert_id = await repository.raise_integration_alert(
        session, provider="hunter.io", message="one-shot"
    )
    assert alert_id is not None
    assert await repository.resolve_integration_alert(session, alert_id) is True

    assert await repository.resolve_integration_alert(session, alert_id) is False


# --- observability.integrations.collect: the full assembly -----------------------------


async def test_collect_assembles_credentials_and_open_alerts_together(
    session: AsyncSession,
) -> None:
    await repository.raise_integration_alert(
        session, provider="hunter.io", message="usage limit reached"
    )
    await session.commit()
    settings = Settings(database_url=DATABASE_URL, hunter_api_key="a-real-key")

    snapshot = await integrations_metrics.collect(session, settings=settings)

    hunter = next(c for c in snapshot.credentials if c.key == "hunter_api_key")
    assert hunter.configured is True
    assert len(snapshot.alerts) == 1
    assert snapshot.alerts[0].provider == "hunter.io"
    # Serializes cleanly -- this is exactly what the FastAPI route returns.
    as_dict = snapshot.as_dict()
    assert as_dict["alerts"][0]["provider"] == "hunter.io"
