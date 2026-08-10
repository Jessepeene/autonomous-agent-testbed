"""The idea validation gate: landing page + outreach before a full build.

Covers the parts most likely to fail silently: compliance now creates a VALIDATING
product instead of building immediately, capacity is enforced without wasting a
repeat LLM call, leads/signals are correctly scoped per validation, and the Learning
Loop integration never touches the pre-existing outreach/planner episode pools it
was deliberately kept separate from.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from factory.engines.compliance import graph as compliance_graph
from factory.engines.development import graph as dev_graph
from factory.hitl.telegram import NullNotifier
from factory.learning import store as learning_store
from factory.llm.client import LLMClient
from factory.llm.providers.base import Provider
from factory.llm.providers.fake import FakeProvider
from factory.llm.router import ProviderRegistry
from factory.sandbox.disabled import DisabledSandbox
from factory.settings import EngineName, Settings
from factory.state import repository
from factory.state.orm import Base, Lead, OutreachLog, Product, Task, new_id
from factory.state.schemas import (
    ComplianceAssessment,
    ComplianceStatus,
    ProductStatus,
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
    """The /interest endpoint opens sessions through the process-wide cached engine
    (`session_scope` -> `get_sessionmaker`), a different connection than this
    file's `session` fixture. Reset it around each test so no pooled connection
    leaks into a later test -- without this, a stale connection from an earlier
    test's use of the global engine can be torn down after a later test's own
    event loop has already closed, crashing with an asyncpg cross-loop error."""
    yield
    from factory.state import db as db_module

    await db_module.dispose_engine()


def _ctx(*, provider: FakeProvider, workspace_root: Path, **overrides: object) -> FactoryContext:
    settings = Settings(
        database_url=DATABASE_URL,
        gemini_api_key="test-key",
        workspace_root=workspace_root,
        **overrides,  # type: ignore[arg-type]
    )
    llm = LLMClient(ProviderRegistry({Provider.GEMINI: provider}), settings=settings)
    return FactoryContext(
        settings=settings,
        llm=llm,
        notifier=NullNotifier(),
        sandbox=DisabledSandbox(),
        enabled_engines={EngineName.COMPLIANCE, EngineName.DEVELOPMENT, EngineName.MARKETING},
        worker_id="validation-gate-test",
    )


async def _make_idea(session: AsyncSession, niche: str) -> str:
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


# --- compliance: validation redirect ------------------------------------------


async def test_approved_idea_creates_a_validating_product_not_a_build(
    session: AsyncSession, tmp_path: Path
) -> None:
    idea_id = await _make_idea(session, "approved-redirect")
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
    assert products[0].status == ProductStatus.VALIDATING.value

    tasks = (await session.execute(select(Task).where(Task.idea_id == idea_id))).scalars().all()
    kinds = {t.kind for t in tasks}
    assert TaskKind.BUILD_LANDING_PAGE.value in kinds
    assert TaskKind.BUILD_MVP.value not in kinds


async def test_capacity_exceeded_defers_without_creating_a_product(
    session: AsyncSession, tmp_path: Path
) -> None:
    idea_id = await _make_idea(session, "capacity-exceeded")
    # Occupy the one available slot with an unrelated validation already in flight.
    other_idea = await _make_idea(session, "capacity-exceeded-occupant")
    occupant_id = await repository.create_product(session, name="Occupant", idea_id=other_idea)
    await repository.set_product_status(session, occupant_id, ProductStatus.VALIDATING)

    provider = FakeProvider(name=Provider.GEMINI)
    provider.register(
        ComplianceAssessment,
        ComplianceAssessment(
            status=ComplianceStatus.APPROVED,
            justification="Lawful, no special-category data involved.",
            confidence=0.9,
        ),
    )
    ctx = _ctx(provider=provider, workspace_root=tmp_path, max_concurrent_validations=1)

    with pytest.raises(compliance_graph.ValidationCapacityExceeded):
        await compliance_graph.run(ctx, session, _compliance_task(idea_id))

    products = (
        (await session.execute(select(Product).where(Product.idea_id == idea_id))).scalars().all()
    )
    assert products == []
    # The idea itself was still marked approved -- a retry must not re-assess it.
    from factory.state.orm import Idea

    idea = await session.get(Idea, idea_id)
    assert idea is not None
    assert idea.status == "approved"


async def test_retry_after_capacity_deferral_does_not_call_the_assessor_again(
    session: AsyncSession, tmp_path: Path
) -> None:
    idea_id = await _make_idea(session, "capacity-retry")
    other_idea = await _make_idea(session, "capacity-retry-occupant")
    occupant_id = await repository.create_product(session, name="Occupant", idea_id=other_idea)
    await repository.set_product_status(session, occupant_id, ProductStatus.VALIDATING)

    provider = FakeProvider(name=Provider.GEMINI)
    provider.register(
        ComplianceAssessment,
        ComplianceAssessment(
            status=ComplianceStatus.APPROVED,
            justification="Lawful, no special-category data involved.",
            confidence=0.9,
        ),
    )
    blocked_ctx = _ctx(provider=provider, workspace_root=tmp_path, max_concurrent_validations=1)
    with pytest.raises(compliance_graph.ValidationCapacityExceeded):
        await compliance_graph.run(blocked_ctx, session, _compliance_task(idea_id))
    assert len(provider.calls) == 1

    # Free up the slot the same way a real completed/retired validation would.
    await repository.set_product_status(session, occupant_id, ProductStatus.PLANNED)

    # Capacity now available -- retry the same idea.
    open_ctx = _ctx(provider=provider, workspace_root=tmp_path, max_concurrent_validations=2)
    await compliance_graph.run(open_ctx, session, _compliance_task(idea_id))

    # No second assessor call: the retry short-circuited on idea.status=="approved".
    assert len(provider.calls) == 1
    products = (
        (await session.execute(select(Product).where(Product.idea_id == idea_id))).scalars().all()
    )
    assert len(products) == 1


# --- repository: scoping and dedup ---------------------------------------------


async def test_leads_for_outreach_product_scoping(session: AsyncSession) -> None:
    idea_a = await _make_idea(session, "product-a")
    idea_b = await _make_idea(session, "product-b")
    product_a = await repository.create_product(session, name="A", idea_id=idea_a)
    product_b = await repository.create_product(session, name="B", idea_id=idea_b)

    for product_id, email in ((product_a, "a@example.test"), (product_b, "b@example.test")):
        session.add(
            Lead(
                id=f"lead-{email}",
                first_name="Test",
                last_name="Lead",
                job_title="Ops",
                company="Acme",
                work_email=email,
                source_url="https://example.test",
                scraped_at=datetime.now(UTC),
                provenance_kind="direct",
                provider="hunter",
                lia_passed=True,
                product_id=product_id,
            )
        )
    await session.flush()

    scoped = await repository.leads_for_outreach(session, product_id=product_a)
    assert {lead.product_id for lead in scoped} == {product_a}

    unscoped = await repository.leads_for_outreach(session)
    assert {lead.product_id for lead in unscoped} == {product_a, product_b}


async def test_insert_validation_signal_dedups_by_product_and_email(session: AsyncSession) -> None:
    idea_id = await _make_idea(session, "dedup-signal")
    product_id = await repository.create_product(session, name="Dedup", idea_id=idea_id)

    first = await repository.insert_validation_signal(
        session, product_id=product_id, email="Person@Example.test", message="interested"
    )
    second = await repository.insert_validation_signal(
        session, product_id=product_id, email="person@example.test", message="again"
    )

    assert first is not None
    assert second is None
    assert await repository.tally_validation_signals(session, product_id) == 1


async def test_count_active_validations_only_counts_validating_status(
    session: AsyncSession,
) -> None:
    idea_id = await _make_idea(session, "count-active")
    product_id = await repository.create_product(session, name="X", idea_id=idea_id)
    assert await repository.count_active_validations(session) == 0

    await repository.set_product_status(session, product_id, ProductStatus.VALIDATING)
    assert await repository.count_active_validations(session) == 1

    await repository.set_product_status(session, product_id, ProductStatus.PLANNED)
    assert await repository.count_active_validations(session) == 0


# --- EVALUATE_VALIDATION: the build-or-scrap decision --------------------------


async def _make_validating_product(
    session: AsyncSession, *, niche: str, prompt_version: str | None = None
) -> tuple[str, str]:
    idea_id = await _make_idea(session, niche)
    product_id = await repository.create_product(session, name=niche, idea_id=idea_id)
    await repository.set_product_status(
        session,
        product_id,
        ProductStatus.VALIDATING,
        landing_page_prompt_version=prompt_version,
    )
    return idea_id, product_id


async def _record_sent(session: AsyncSession, *, product_id: str, count: int) -> None:
    lead_id = new_id()
    session.add(
        Lead(
            id=lead_id,
            first_name="Test",
            last_name="Lead",
            job_title="Ops",
            company="Acme",
            work_email=f"{lead_id}@example.test",
            source_url="https://example.test",
            scraped_at=datetime.now(UTC),
            provenance_kind="direct",
            provider="hunter",
            lia_passed=True,
            product_id=product_id,
        )
    )
    await session.flush()
    for _ in range(count):
        session.add(
            OutreachLog(
                id=new_id(),
                lead_id=lead_id,
                product_id=product_id,
                subject="s",
                status="sent",
                unsubscribe_token=new_id(),
            )
        )
    await session.flush()


def _eval_task(product_id: str) -> Task:
    return Task(
        id=new_id(),
        kind=TaskKind.EVALUATE_VALIDATION.value,
        engine=EngineName.DEVELOPMENT.value,
        status=TaskStatus.CLAIMED.value,
        priority=100,
        payload={"product_id": product_id},
        product_id=product_id,
        attempt_count=1,
    )


async def test_validation_pass_queues_build_mvp_and_sets_planned(
    session: AsyncSession, tmp_path: Path
) -> None:
    idea_id, product_id = await _make_validating_product(session, niche="pass-case")
    await _record_sent(session, product_id=product_id, count=20)
    for i in range(5):  # 5/20 = 25%, comfortably above the default 5% bar
        await repository.insert_validation_signal(
            session, product_id=product_id, email=f"interested{i}@example.test"
        )

    ctx = _ctx(
        provider=FakeProvider(name=Provider.GEMINI),
        workspace_root=tmp_path,
        validation_min_sample_size=10,
        validation_min_response_rate=0.05,
    )
    result = await dev_graph.run(ctx, session, _eval_task(product_id))

    assert "passed" in result
    product = await session.get(Product, product_id)
    assert product is not None
    assert product.status == ProductStatus.PLANNED.value
    build_tasks = (
        (
            await session.execute(
                select(Task).where(Task.idea_id == idea_id, Task.kind == TaskKind.BUILD_MVP.value)
            )
        )
        .scalars()
        .all()
    )
    assert len(build_tasks) == 1
    assert build_tasks[0].product_id == product_id


async def test_validation_fail_below_sample_size_retires_regardless_of_rate(
    session: AsyncSession, tmp_path: Path
) -> None:
    _, product_id = await _make_validating_product(session, niche="too-few-sent")
    await _record_sent(session, product_id=product_id, count=3)
    await repository.insert_validation_signal(
        session, product_id=product_id, email="one@example.test"
    )  # 1/3 = 33%, would pass on rate alone

    ctx = _ctx(
        provider=FakeProvider(name=Provider.GEMINI),
        workspace_root=tmp_path,
        validation_min_sample_size=10,
        validation_min_response_rate=0.05,
    )
    result = await dev_graph.run(ctx, session, _eval_task(product_id))

    assert "failed" in result
    product = await session.get(Product, product_id)
    assert product is not None
    assert product.status == ProductStatus.RETIRED.value


async def test_validation_fail_below_response_rate_retires(
    session: AsyncSession, tmp_path: Path
) -> None:
    _, product_id = await _make_validating_product(session, niche="low-rate")
    await _record_sent(session, product_id=product_id, count=20)
    # 0 signals -- clearly below any positive threshold.

    ctx = _ctx(
        provider=FakeProvider(name=Provider.GEMINI),
        workspace_root=tmp_path,
        validation_min_sample_size=10,
        validation_min_response_rate=0.05,
    )
    result = await dev_graph.run(ctx, session, _eval_task(product_id))

    assert "failed" in result
    product = await session.get(Product, product_id)
    assert product is not None
    assert product.status == ProductStatus.RETIRED.value


async def test_evaluation_records_a_learning_episode_without_touching_other_keys(
    session: AsyncSession, tmp_path: Path
) -> None:
    outreach_key = "factory.engines.marketing.outreach:SYSTEM_PROMPT"
    baseline_id = await learning_store.bootstrap_active(
        session,
        kind="PROMPT",
        key=outreach_key,
        value={"text": "existing outreach prompt"},
        created_by="bootstrap",
        target_metric="outreach_content_pass_rate",
    )
    await learning_store.record_episode(
        session, tunable_parameter_id=baseline_id, outcome="success"
    )
    before_successes, before_total = await learning_store.episode_counts(session, baseline_id)

    landing_id = await learning_store.bootstrap_active(
        session,
        kind="PROMPT",
        key="factory.engines.development.scaffold.generator:LANDING_PAGE_SYSTEM",
        value={"text": "existing landing page prompt"},
        created_by="bootstrap",
        target_metric="landing_page_interest_rate",
    )
    _, product_id = await _make_validating_product(
        session, niche="learning-isolation", prompt_version=landing_id
    )
    await _record_sent(session, product_id=product_id, count=20)
    await repository.insert_validation_signal(
        session, product_id=product_id, email="interested@example.test"
    )

    ctx = _ctx(
        provider=FakeProvider(name=Provider.GEMINI),
        workspace_root=tmp_path,
        validation_min_sample_size=10,
        validation_min_response_rate=0.01,
    )
    await dev_graph.run(ctx, session, _eval_task(product_id))

    after_successes, after_total = await learning_store.episode_counts(session, baseline_id)
    assert (after_successes, after_total) == (before_successes, before_total)

    landing_successes, landing_total = await learning_store.episode_counts(session, landing_id)
    assert landing_total == 1
    assert landing_successes == 1


# --- /interest endpoint ---------------------------------------------------------
#
# httpx.AsyncClient + ASGITransport, not fastapi.testclient.TestClient: TestClient
# is sync and drives the app from a second, internally-managed event loop (via
# anyio's BlockingPortal), which crashed with an asyncpg cross-loop/closed-loop
# error the moment a test also touched the database on this file's own async
# `session` fixture -- reproduced on both Windows and Linux, so not a platform
# quirk. Staying on one event loop end-to-end avoids it entirely.


async def _interest_client() -> httpx.AsyncClient:
    from factory.api.app import app

    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def test_interest_endpoint_dedups_by_email(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DATABASE_URL", DATABASE_URL or "")
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    _, product_id = await _make_validating_product(session, niche="interest-endpoint")
    await session.commit()
    body = {"email": "lead@example.test", "message": "tell me more"}

    async with await _interest_client() as client:
        first = await client.post(f"/interest/{product_id}", json=body)
        second = await client.post(f"/interest/{product_id}", json=body)

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json() == {"received": True}
    assert second.json() == {"received": True}
    assert await repository.tally_validation_signals(session, product_id) == 1


async def test_interest_endpoint_404s_on_an_unknown_product(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", DATABASE_URL or "")
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")

    async with await _interest_client() as client:
        response = await client.post(
            "/interest/does-not-exist", json={"email": "lead@example.test"}
        )

    assert response.status_code == 404


async def test_interest_endpoint_rejects_malformed_email(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", DATABASE_URL or "")
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")

    async with await _interest_client() as client:
        response = await client.post("/interest/some-product-id", json={"email": "not-an-email"})

    assert response.status_code == 422
