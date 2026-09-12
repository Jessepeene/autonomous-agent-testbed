"""Tier 2 finding #6, system-wide verification plan: `IDEA_SYSTEM_PROMPT`
(compliance/assessor.py) already names health/medical/payments/employee-data
categories as reasons to reject or flag an idea -- but nothing deterministic
backstopped that instruction. An `APPROVED` verdict on an idea that mentions
one of these commits real engineering spend before any human ever looks at
it. `compliance/graph.py::run` now forces `FLAG_FOR_REVIEW` regardless of the
model's own verdict when a risk-category keyword is present.

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
from factory.state.orm import Base, Interrupt, Product, Task, new_id
from factory.state.schemas import (
    ComplianceAssessment,
    ComplianceStatus,
    InterruptReason,
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
        worker_id="idea-risk-backstop-test",
    )


async def _make_idea(session: AsyncSession, niche: str, *, problem_statement: str) -> str:
    idea = ScoredIdea(
        problem_statement=problem_statement,
        target_niche=niche,
        source_url="https://example.test/thread",
        monetization_hypothesis="Buyers pay EUR 49/month per seat.",
        pain_severity=0.8,
        willingness_to_pay=0.7,
        build_feasibility=0.6,
        adoption_ease=0.8,
    )
    idea_id = await repository.upsert_idea(session, idea)
    assert idea_id is not None
    return idea_id


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


async def test_an_approved_idea_mentioning_a_risk_category_is_flagged_not_approved(
    session: AsyncSession, tmp_path: Path
) -> None:
    idea_id = await _make_idea(
        session,
        "risk-category-override",
        problem_statement="Clinics struggle to track patient medical records across systems.",
    )
    provider = FakeProvider(name=Provider.GEMINI)
    provider.register(
        ComplianceAssessment,
        ComplianceAssessment(
            status=ComplianceStatus.APPROVED,
            justification="Lawful, no special data.",
            confidence=0.9,
        ),
    )
    ctx = _ctx(provider=provider, workspace_root=tmp_path)

    result = await compliance_graph.run(ctx, session, _compliance_task(idea_id))

    assert "flagged" in result
    products = (
        (await session.execute(select(Product).where(Product.idea_id == idea_id))).scalars().all()
    )
    assert products == []
    stmt = select(Interrupt).where(Interrupt.reason == InterruptReason.LEGAL_UNCERTAINTY.value)
    interrupts = (await session.execute(stmt)).scalars().all()
    assert len(interrupts) == 1


async def test_an_approved_idea_with_no_risk_category_proceeds_normally(
    session: AsyncSession, tmp_path: Path
) -> None:
    idea_id = await _make_idea(
        session,
        "no-risk-category",
        problem_statement="Bookkeepers reconcile invoices by hand every week.",
    )
    provider = FakeProvider(name=Provider.GEMINI)
    provider.register(
        ComplianceAssessment,
        ComplianceAssessment(
            status=ComplianceStatus.APPROVED,
            justification="Lawful, no special data.",
            confidence=0.9,
        ),
    )
    ctx = _ctx(provider=provider, workspace_root=tmp_path)

    await compliance_graph.run(ctx, session, _compliance_task(idea_id))

    products = (
        (await session.execute(select(Product).where(Product.idea_id == idea_id))).scalars().all()
    )
    assert len(products) == 1


async def test_a_rejected_idea_mentioning_a_risk_category_stays_rejected(
    session: AsyncSession, tmp_path: Path
) -> None:
    """The backstop only touches APPROVED -- REJECTED is already the safe,
    conservative outcome and must not be overridden into a human review."""
    idea_id = await _make_idea(
        session,
        "risk-category-already-rejected",
        problem_statement="Clinics struggle to track patient medical records across systems.",
    )
    provider = FakeProvider(name=Provider.GEMINI)
    provider.register(
        ComplianceAssessment,
        ComplianceAssessment(
            status=ComplianceStatus.REJECTED,
            justification="Processes special-category health data.",
            confidence=0.9,
        ),
    )
    ctx = _ctx(provider=provider, workspace_root=tmp_path)

    result = await compliance_graph.run(ctx, session, _compliance_task(idea_id))

    assert "rejected" in result
    interrupts = (await session.execute(select(Interrupt))).scalars().all()
    assert interrupts == []
