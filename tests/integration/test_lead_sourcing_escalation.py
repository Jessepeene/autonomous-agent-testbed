"""A diagnosed lead-provider credential/request problem escalates immediately,
not after a day of waiting -- "either the code or something with the key,"
and only the key case is something a human needs to act on, the same basket
as asking for a genuinely missing credential.

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

from factory.engines.marketing import graph as mkt_graph
from factory.engines.marketing import leads as leads_module
from factory.engines.marketing.graph import _escalate_lead_sourcing_problem
from factory.hitl.telegram import NullNotifier
from factory.llm.client import LLMClient
from factory.llm.providers.base import Provider
from factory.llm.providers.fake import FakeProvider
from factory.llm.router import ProviderRegistry
from factory.sandbox.disabled import DisabledSandbox
from factory.settings import EngineName, Settings
from factory.state import db as db_module
from factory.state import repository
from factory.state.orm import Base, Interrupt, Product, Task, new_id
from factory.state.schemas import InterruptReason, ProductStatus, ScoredIdea, TaskKind, TaskStatus
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
    await db_module.dispose_engine()


def _ctx(*, workspace_root: Path) -> FactoryContext:
    settings = Settings(
        database_url=DATABASE_URL, gemini_api_key="test-key", workspace_root=workspace_root
    )
    llm = LLMClient(ProviderRegistry({Provider.GEMINI: FakeProvider()}), settings=settings)
    return FactoryContext(
        settings=settings,
        llm=llm,
        notifier=NullNotifier(),
        sandbox=DisabledSandbox(),
        enabled_engines={EngineName.MARKETING},
        worker_id="lead-sourcing-escalation-test",
    )


async def _make_product(session: AsyncSession, name: str) -> str:
    idea = ScoredIdea(
        problem_statement="Bookkeepers reconcile invoices by hand every single week.",
        target_niche=name,
        source_url="https://example.test/thread",
        monetization_hypothesis="EUR 49/month, bought after the first month-end close.",
        pain_severity=0.8,
        willingness_to_pay=0.7,
        build_feasibility=0.6,
        adoption_ease=0.8,
    )
    idea_id = await repository.upsert_idea(session, idea)
    assert idea_id is not None
    product_id = await repository.create_product(session, name=name, idea_id=idea_id)
    await repository.set_product_status(session, product_id, ProductStatus.VALIDATING)
    return product_id


def _harvest_task(product_id: str) -> Task:
    return Task(
        id=new_id(),
        kind=TaskKind.HARVEST_LEADS.value,
        engine=EngineName.MARKETING.value,
        status=TaskStatus.CLAIMED.value,
        priority=120,
        payload={"product_id": product_id},
        product_id=product_id,
        attempt_count=1,
    )


# --- _escalate_lead_sourcing_problem, called directly ---------------------------------


async def test_credential_problem_names_it_as_an_account_issue(session: AsyncSession) -> None:
    product_id = await _make_product(session, "escalation-credential-product")
    report = leads_module.IngestReport(
        credential_problem="apollo.io rejected the request (HTTP 403): Invalid API Key"
    )
    await session.flush()

    await _escalate_lead_sourcing_problem(session, product_id, report)

    row = (
        (await session.execute(select(Interrupt).where(Interrupt.product_id == product_id)))
        .scalars()
        .one()
    )
    assert row.reason == InterruptReason.LEAD_SOURCING_STALLED.value
    assert "escalation-credential-product" in row.subject
    assert "account problem" in row.detail
    assert "dashboard" in row.detail
    assert "Invalid API Key" in row.detail


async def test_request_problem_names_it_as_a_code_issue_not_an_account_issue(
    session: AsyncSession,
) -> None:
    product_id = await _make_product(session, "escalation-request-product")
    report = leads_module.IngestReport(
        request_problem="apollo.io rejected the request (HTTP 400): Unprocessable parameters"
    )
    await session.flush()

    await _escalate_lead_sourcing_problem(session, product_id, report)

    row = (
        (await session.execute(select(Interrupt).where(Interrupt.product_id == product_id)))
        .scalars()
        .one()
    )
    assert "bug" in row.detail
    assert "not something fixable in its dashboard" in row.detail
    assert "account problem" not in row.detail


async def test_does_not_raise_a_second_escalation_if_one_is_already_open(
    session: AsyncSession,
) -> None:
    product_id = await _make_product(session, "escalation-no-duplicate-product")
    await repository.open_interrupt(
        session,
        reason=InterruptReason.LEAD_SOURCING_STALLED,
        subject="already asked",
        detail="...",
        headline="Test escalation",
        reply_hint="Reply anything to retry sourcing leads, or REJECT to retire this product.",
        product_id=product_id,
    )
    await session.flush()

    report = leads_module.IngestReport(credential_problem="apollo.io rejected: HTTP 403")
    await _escalate_lead_sourcing_problem(session, product_id, report)

    rows = (
        (await session.execute(select(Interrupt).where(Interrupt.product_id == product_id)))
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].subject == "already asked"  # the pre-existing one, untouched


# --- _harvest wiring: does it actually call the escalation? ----------------------------


async def test_harvest_escalates_immediately_on_a_credential_problem(
    session: AsyncSession, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    product_id = await _make_product(session, "harvest-wiring-credential-product")
    await session.commit()

    async def fake_ingest(*args: object, **kwargs: object) -> leads_module.IngestReport:
        return leads_module.IngestReport(
            credential_problem="apollo.io rejected the request (HTTP 403): Invalid API Key"
        )

    monkeypatch.setattr(leads_module, "ingest", fake_ingest)

    ctx = _ctx(workspace_root=tmp_path)
    await mkt_graph._harvest(ctx, session, _harvest_task(product_id))
    await session.commit()

    row = (
        (await session.execute(select(Interrupt).where(Interrupt.product_id == product_id)))
        .scalars()
        .one()
    )
    assert row.reason == InterruptReason.LEAD_SOURCING_STALLED.value


async def test_harvest_does_not_escalate_on_an_ordinary_zero_result_search(
    session: AsyncSession, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The common, unremarkable case -- a search that genuinely found nobody,
    with no diagnosable provider failure -- must not trigger an escalation.
    That case is what the slower stalled-validation sweep is for."""
    product_id = await _make_product(session, "harvest-wiring-empty-product")
    await session.commit()

    async def fake_ingest(*args: object, **kwargs: object) -> leads_module.IngestReport:
        return leads_module.IngestReport()  # no leads, no diagnosed problem either

    monkeypatch.setattr(leads_module, "ingest", fake_ingest)

    ctx = _ctx(workspace_root=tmp_path)
    await mkt_graph._harvest(ctx, session, _harvest_task(product_id))
    await session.commit()

    assert await repository.count_open_interrupts(session) == 0


async def test_harvest_persists_the_report_summary_for_a_later_channel_scout(
    session: AsyncSession, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`IngestReport` itself is thrown away once `_harvest` returns -- this is
    the one place its summary text survives, so a later CHANNEL_SCOUT_REVIEW
    (see `_channel_scout_review`) can be told why the last harvest came up
    empty instead of reasoning blind about it."""
    product_id = await _make_product(session, "harvest-wiring-summary-product")
    await session.commit()

    report = leads_module.IngestReport(planned=30, returned=0)

    async def fake_ingest(*args: object, **kwargs: object) -> leads_module.IngestReport:
        return report

    monkeypatch.setattr(leads_module, "ingest", fake_ingest)

    ctx = _ctx(workspace_root=tmp_path)
    await mkt_graph._harvest(ctx, session, _harvest_task(product_id))
    await session.commit()

    product = await session.get(Product, product_id)
    assert product is not None
    assert product.last_lead_sourcing_summary == report.summary()
