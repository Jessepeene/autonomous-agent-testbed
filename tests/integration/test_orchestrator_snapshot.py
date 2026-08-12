"""`build_snapshot`'s `approved_ideas_waiting` -- a live count, not a lifetime tally.

Regression coverage for a bug found while watching the live factory: an approved
idea always gets a product in the same compliance pass, so a bare count of
`Idea.status == 'approved'` rows only ever grows -- it never drops even once
every one of them has shipped (`live`) or been abandoned (`retired`), and by
itself tells the model nothing about whether Development actually has anything
left to do. Observed live: this stale count alone was enough to make the
Orchestrator's own LLM consultation route to Development on a tick where its
queue was provably empty.

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
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from factory.hitl.telegram import NullNotifier
from factory.llm.client import LLMClient
from factory.llm.providers.base import Provider
from factory.llm.providers.fake import FakeProvider
from factory.llm.router import ProviderRegistry
from factory.sandbox.disabled import DisabledSandbox
from factory.settings import EngineName, Settings
from factory.state import repository
from factory.state.orm import Base
from factory.state.schemas import ProductStatus, ScoredIdea
from factory.supervisor import orchestrator
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


def _ctx(*, workspace_root: Path) -> FactoryContext:
    settings = Settings(
        database_url=DATABASE_URL, gemini_api_key="test-key", workspace_root=workspace_root
    )
    llm = LLMClient(
        ProviderRegistry({Provider.GEMINI: FakeProvider(name=Provider.GEMINI)}), settings=settings
    )
    return FactoryContext(
        settings=settings,
        llm=llm,
        notifier=NullNotifier(),
        sandbox=DisabledSandbox(),
        enabled_engines={EngineName.DEVELOPMENT},
        worker_id="snapshot-test",
    )


async def _approved_idea_with_product(
    session: AsyncSession, niche: str, product_status: ProductStatus
) -> None:
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
    await repository.set_idea_status(session, idea_id, "approved")
    product_id = await repository.create_product(session, name=niche, idea_id=idea_id)
    await repository.set_product_status(session, product_id, product_status)


async def test_live_and_retired_products_do_not_count_as_still_waiting(
    session: AsyncSession, tmp_path: Path
) -> None:
    await _approved_idea_with_product(session, "snapshot-live", ProductStatus.LIVE)
    await _approved_idea_with_product(session, "snapshot-retired", ProductStatus.RETIRED)
    await session.flush()

    ctx = _ctx(workspace_root=tmp_path)
    snapshot = await orchestrator.build_snapshot(session, ctx)

    assert snapshot.approved_ideas_waiting == 0


async def test_a_product_still_in_progress_counts_as_waiting(
    session: AsyncSession, tmp_path: Path
) -> None:
    await _approved_idea_with_product(session, "snapshot-planned", ProductStatus.PLANNED)
    await _approved_idea_with_product(session, "snapshot-validating", ProductStatus.VALIDATING)
    await _approved_idea_with_product(session, "snapshot-building", ProductStatus.BUILDING)
    await session.flush()

    ctx = _ctx(workspace_root=tmp_path)
    snapshot = await orchestrator.build_snapshot(session, ctx)

    assert snapshot.approved_ideas_waiting == 3


async def test_a_mix_counts_only_the_still_in_progress_ones(
    session: AsyncSession, tmp_path: Path
) -> None:
    await _approved_idea_with_product(session, "snapshot-mix-live", ProductStatus.LIVE)
    await _approved_idea_with_product(session, "snapshot-mix-retired", ProductStatus.RETIRED)
    await _approved_idea_with_product(session, "snapshot-mix-planned", ProductStatus.PLANNED)
    await session.flush()

    ctx = _ctx(workspace_root=tmp_path)
    snapshot = await orchestrator.build_snapshot(session, ctx)

    assert snapshot.approved_ideas_waiting == 1
