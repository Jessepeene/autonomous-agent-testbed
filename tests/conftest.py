"""Shared fixtures.

Unit tests run with no network, no Docker, and no database. Anything that would
reach outside the process is a fake, so the guardrail tests assert on behaviour
rather than on mocks of themselves.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from factory.hitl.telegram import NullNotifier
from factory.llm.client import LLMClient
from factory.llm.providers.base import Provider
from factory.llm.providers.fake import FakeProvider
from factory.llm.router import ProviderRegistry
from factory.sandbox.disabled import DisabledSandbox
from factory.settings import EngineName, Settings
from factory.state.orm import Base, Memory
from factory.state.schemas import LeadRecord
from factory.supervisor.context import FactoryContext


@pytest.fixture
def blacklist_file(tmp_path: Path) -> Path:
    path = tmp_path / "blacklist.txt"
    path.write_text(
        "\n".join(
            [
                "# test suppression list",
                "blocked@example.com",
                "@spam-domain.test",
                "*.subs.test",
                "erased@example.com  # ERASURE requested 2026-01-01T00:00:00Z",
            ]
        ),
        encoding="utf-8",
    )
    return path


@pytest.fixture
def settings(tmp_path: Path, blacklist_file: Path) -> Settings:
    return Settings(
        database_url=None,
        gemini_api_key="test-gemini-key",
        anthropic_api_key="test-anthropic-key",
        factory_dry_run=True,
        blacklist_path=blacklist_file,
        workspace_root=tmp_path / "workspaces",
        outbound_from_address="factory@example.com",
        outbound_postal_address="Testlaan 1, 1000 AA Amsterdam, NL",
        unsubscribe_base_url="https://example.com",
        max_concurrent_builds=2,
        compliance_min_confidence=0.85,
    )


@pytest.fixture
def gemini_fake() -> FakeProvider:
    return FakeProvider(Provider.GEMINI)


@pytest.fixture
def anthropic_fake() -> FakeProvider:
    return FakeProvider(Provider.ANTHROPIC)


@pytest.fixture
def llm(settings: Settings, gemini_fake: FakeProvider, anthropic_fake: FakeProvider) -> LLMClient:
    registry = ProviderRegistry({Provider.GEMINI: gemini_fake, Provider.ANTHROPIC: anthropic_fake})
    # backoff_base=0 exercises the real retry path without real sleeps.
    return LLMClient(registry, settings=settings, backoff_base=0.0)


@pytest.fixture
def ctx(settings: Settings, llm: LLMClient) -> FactoryContext:
    return FactoryContext(
        settings=settings,
        llm=llm,
        notifier=NullNotifier(),
        sandbox=DisabledSandbox(),
        enabled_engines={
            EngineName.SUPERVISOR,
            EngineName.DISCOVERY,
            EngineName.COMPLIANCE,
            EngineName.MARKETING,
            EngineName.FINANCE,
        },
        docker_available=False,
        worker_id="test-worker",
        disabled_reasons={EngineName.DEVELOPMENT: "no Docker daemon"},
    )


@pytest_asyncio.fixture
async def memory_session() -> AsyncIterator[AsyncSession]:
    """An in-memory SQLite session hosting only the ``memories`` table.

    ``factory.memory.store`` is plain SQLAlchemy Core with no Postgres-specific
    constructs, so SQLite exercises its real SQL rather than a mock of it. Other
    tables use ``JSONB``/``ON CONFLICT``, which SQLite cannot compile, so only the
    one table this module needs is created.
    """
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all, tables=[Memory.__table__])
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        yield session
    await engine.dispose()


@pytest.fixture
def lead() -> LeadRecord:
    return LeadRecord(
        first_name="Ada",
        last_name="Lovelace",
        job_title="Head of Operations",
        company="Analytical Engines BV",
        work_email="ada@analytical-engines.test",
        source_url="https://analytical-engines.test/team",
        scraped_at=datetime(2026, 8, 1, tzinfo=UTC),
    )
