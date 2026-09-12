"""Tier 3 finding #13, system-wide verification plan: the LEGAL_UNCERTAINTY
interrupt a human reviews for a flagged idea must show the real structured
facts the justification is reasoning about (niche, problem, monetization,
source), not just the justification's own prose -- so a human can spot an
argument that doesn't actually match the idea it's supposedly about, the
same cross-check `lia.py`'s per-lead interrupt already gave (role/company),
now given to the idea-level interrupt too.

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
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from factory.engines.compliance import graph as compliance_graph
from factory.hitl.telegram import NullNotifier
from factory.llm.client import LLMClient
from factory.llm.providers.base import Provider
from factory.llm.providers.fake import FakeProvider
from factory.llm.router import ProviderRegistry
from factory.sandbox.disabled import DisabledSandbox
from factory.settings import EngineName, Settings
from factory.state import repository
from factory.state.orm import Base, Interrupt, Task, new_id
from factory.state.schemas import (
    ComplianceAssessment,
    ComplianceStatus,
    ScoredIdea,
    TaskKind,
    TaskStatus,
)
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


@pytest_asyncio.fixture(autouse=True)
async def _reset_global_engine() -> AsyncIterator[None]:
    yield
    from factory.state import db as db_module

    await db_module.dispose_engine()


def _ctx(*, provider: FakeProvider, workspace_root: Path) -> FactoryContext:
    settings = Settings(
        database_url=DATABASE_URL, gemini_api_key="test-key", workspace_root=workspace_root
    )
    llm = LLMClient(ProviderRegistry({Provider.GEMINI: provider}), settings=settings)
    return FactoryContext(
        settings=settings,
        llm=llm,
        notifier=NullNotifier(),
        sandbox=DisabledSandbox(),
        enabled_engines={EngineName.COMPLIANCE},
        worker_id="idea-interrupt-transparency-test",
    )


def _compliance_task(idea_id: str) -> Task:
    return Task(
        id=new_id(),
        kind=TaskKind.ASSESS_COMPLIANCE.value,
        engine=EngineName.COMPLIANCE.value,
        status=TaskStatus.CLAIMED.value,
        priority=80,
        payload={"idea_id": idea_id},
        idea_id=idea_id,
        attempt_count=1,
    )


async def test_a_flagged_ideas_interrupt_shows_the_real_structured_facts(
    session: AsyncSession, tmp_path: Path
) -> None:
    idea = ScoredIdea(
        problem_statement="Bookkeepers reconcile invoices by hand every single week.",
        target_niche="Bookkeepers at small Dutch accounting firms",
        source_url="https://example.test/thread/42",
        monetization_hypothesis="EUR 49/month per seat, billed to the firm directly.",
        pain_severity=0.8,
        willingness_to_pay=0.7,
        build_feasibility=0.6,
        adoption_ease=0.8,
    )
    idea_id = await repository.upsert_idea(session, idea)
    assert idea_id is not None

    provider = FakeProvider(name=Provider.GEMINI)
    provider.register(
        ComplianceAssessment,
        ComplianceAssessment(
            status=ComplianceStatus.FLAG_FOR_REVIEW,
            justification="Arguable liability, a human should decide knowingly.",
            confidence=0.5,
        ),
    )
    ctx = _ctx(provider=provider, workspace_root=tmp_path)
    task = _compliance_task(idea_id)

    await compliance_graph.run(ctx, session, task)

    row = (
        (await session.execute(select(Interrupt).where(Interrupt.task_id == task.id)))
        .scalars()
        .one()
    )
    assert "Bookkeepers at small Dutch accounting firms" in row.detail
    assert "Bookkeepers reconcile invoices by hand every single week." in row.detail
    assert "EUR 49/month per seat, billed to the firm directly." in row.detail
    assert "https://example.test/thread/42" in row.detail
    assert "Arguable liability, a human should decide knowingly." in row.detail
