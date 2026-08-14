"""The idea validation gate: landing page + outreach before a full build.

Covers the parts most likely to fail silently: compliance now creates a VALIDATING
product instead of building immediately, capacity is enforced without wasting a
repeat LLM call, leads/signals are correctly scoped per validation, and the Learning
Loop integration never touches the pre-existing outreach/planner episode pools it
was deliberately kept separate from.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator
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
from factory.state.orm import Base, Interrupt, Lead, OutreachLog, Product, Task, new_id
from factory.state.schemas import (
    ComplianceAssessment,
    ComplianceStatus,
    InterruptReason,
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


@pytest.fixture(autouse=True)
def _reset_settings_cache() -> Iterator[None]:
    """`get_settings()` is `lru_cache`d, so the *first* test in this file to hit
    an HTTP endpoint bakes in whatever `Settings()` that call built -- later
    `monkeypatch.setenv` calls (FACTORY_DRY_RUN, ABUSE_IP_HASH_SECRET, the rate
    limits) would silently have no effect on what the endpoint actually reads.
    Cleared before and after every test so each one starts from a genuinely
    fresh read of its own env."""
    from factory.settings import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


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


async def test_leads_for_outreach_excludes_already_sent_leads(session: AsyncSession) -> None:
    """Regression test: `leads_for_outreach`'s own docstring claims "not yet
    contacted" but the query never enforced it, so the same lead was re-selected
    (and re-emailed) on every subsequent call indefinitely."""
    idea_id = await _make_idea(session, "already-contacted")
    product_id = await repository.create_product(session, name="AC", idea_id=idea_id)

    def _lead(lead_id: str, email: str) -> Lead:
        return Lead(
            id=lead_id,
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

    contacted = _lead("contacted", "contacted@example.test")
    blocked = _lead("blocked", "blocked@example.test")
    fresh = _lead("fresh", "fresh@example.test")
    session.add_all([contacted, blocked, fresh])
    await session.flush()

    await repository.record_outreach(
        session,
        lead_id=contacted.id,
        subject="s",
        status="sent",
        product_id=product_id,
        unsubscribe_token="tok-contacted",
    )
    # A blocked send never reached the recipient -- must remain eligible for retry.
    await repository.record_outreach(
        session,
        lead_id=blocked.id,
        subject="s",
        status="blocked",
        product_id=product_id,
        unsubscribe_token="tok-blocked",
    )
    await session.flush()

    result = await repository.leads_for_outreach(session, product_id=product_id)
    ids = {lead.id for lead in result}
    assert ids == {"blocked", "fresh"}
    assert "contacted" not in ids


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


# --- signal verification: double opt-in --------------------------------------
#
# Regression coverage for a real exploit found live during a risk audit: six
# unconfirmed email addresses submitted directly to /interest cleared
# validation_min_response_rate and carried a product all the way through
# EVALUATE_VALIDATION into a real build. A signal now only counts once its
# confirmation link is clicked.


def _settings_env(monkeypatch: pytest.MonkeyPatch, **overrides: str) -> None:
    monkeypatch.setenv("DATABASE_URL", DATABASE_URL or "")
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    for key, value in overrides.items():
        monkeypatch.setenv(key, value)


async def test_an_unconfirmed_signal_does_not_count_toward_the_gate(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _settings_env(monkeypatch, FACTORY_DRY_RUN="0")
    _, product_id = await _make_validating_product(session, niche="unconfirmed-signal")
    await session.commit()

    async with await _interest_client() as client:
        response = await client.post(
            f"/interest/{product_id}", json={"email": "unconfirmed@example.test"}
        )

    assert response.status_code == 200
    # The row exists (dedup must still see it on a repeat submission) but is not
    # confirmed, so the gate's own tally -- confirmed_only=True by default --
    # must not count it.
    assert await repository.tally_validation_signals(session, product_id, confirmed_only=False) == 1
    assert await repository.tally_validation_signals(session, product_id) == 0


async def test_confirming_the_link_makes_the_signal_count(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _settings_env(monkeypatch, FACTORY_DRY_RUN="0", UNSUBSCRIBE_BASE_URL="https://example.test")
    _, product_id = await _make_validating_product(session, niche="confirm-flow")
    await session.commit()

    async with await _interest_client() as client:
        await client.post(f"/interest/{product_id}", json={"email": "confirmable@example.test"})

    from sqlalchemy import select

    from factory.state.orm import ValidationSignal

    token = (
        await session.execute(
            select(ValidationSignal.confirmation_token).where(
                ValidationSignal.product_id == product_id
            )
        )
    ).scalar_one()
    assert token is not None

    async with await _interest_client() as client:
        confirm_response = await client.get(f"/confirm/{token}")

    assert confirm_response.status_code == 200
    assert await repository.tally_validation_signals(session, product_id) == 1


async def test_confirming_twice_is_a_harmless_no_op(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _settings_env(monkeypatch, FACTORY_DRY_RUN="0", UNSUBSCRIBE_BASE_URL="https://example.test")
    _, product_id = await _make_validating_product(session, niche="confirm-twice")
    await session.commit()

    async with await _interest_client() as client:
        await client.post(f"/interest/{product_id}", json={"email": "twice@example.test"})

    from sqlalchemy import select

    from factory.state.orm import ValidationSignal

    token = (
        await session.execute(
            select(ValidationSignal.confirmation_token).where(
                ValidationSignal.product_id == product_id
            )
        )
    ).scalar_one()

    async with await _interest_client() as client:
        first = await client.get(f"/confirm/{token}")
        second = await client.get(f"/confirm/{token}")

    assert first.status_code == 200
    assert second.status_code == 200
    assert await repository.tally_validation_signals(session, product_id) == 1


async def test_an_unknown_confirmation_token_404s(monkeypatch: pytest.MonkeyPatch) -> None:
    _settings_env(monkeypatch, FACTORY_DRY_RUN="0")

    async with await _interest_client() as client:
        response = await client.get("/confirm/not-a-real-token")

    assert response.status_code == 404


async def test_dry_run_auto_confirms_at_insert_time(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The explicit, documented version of what every other test in this file
    relies on implicitly (FACTORY_DRY_RUN defaults to True when unset): dry-run
    must never require a confirmation click that has no real inbox to come
    from, and the tally must still be a single, honest `confirmed_at IS NOT
    NULL` query -- no separate dry-run branch on the read side."""
    _settings_env(monkeypatch, FACTORY_DRY_RUN="1")
    _, product_id = await _make_validating_product(session, niche="dry-run-auto-confirm")
    await session.commit()

    async with await _interest_client() as client:
        response = await client.post(
            f"/interest/{product_id}", json={"email": "dryrun@example.test"}
        )

    assert response.status_code == 200
    assert await repository.tally_validation_signals(session, product_id) == 1

    from sqlalchemy import select

    from factory.state.orm import ValidationSignal

    method = (
        await session.execute(
            select(ValidationSignal.confirmation_method).where(
                ValidationSignal.product_id == product_id
            )
        )
    ).scalar_one()
    assert method == "dry_run_auto"


async def test_a_blacklisted_address_still_gets_recorded_without_crashing(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch, blacklist_file: Path
) -> None:
    """The endpoint always calls the real `send_email`, which owns its own
    fail-closed blacklist gate (see `test_email_blacklist.py`) -- this only
    proves the wiring doesn't special-case or crash on a suppressed address,
    not that the gate itself works."""
    blacklist_file.write_text("blocked@example.test\n")  # noqa: ASYNC240 - instant local write
    _settings_env(
        monkeypatch,
        FACTORY_DRY_RUN="0",
        UNSUBSCRIBE_BASE_URL="https://example.test",
        BLACKLIST_PATH=str(blacklist_file),
    )
    _, product_id = await _make_validating_product(session, niche="blacklisted-submitter")
    await session.commit()

    async with await _interest_client() as client:
        response = await client.post(
            f"/interest/{product_id}", json={"email": "blocked@example.test"}
        )

    assert response.status_code == 200
    assert await repository.tally_validation_signals(session, product_id, confirmed_only=False) == 1


# --- signal verification: IP concentration ------------------------------------


async def test_concentrated_signals_from_one_ip_refuse_to_pass_and_retire(
    session: AsyncSession, tmp_path: Path
) -> None:
    """The check that actually kills the exploit this whole phase exists for: a
    response rate that clears the normal bar is refused anyway once one
    submitter IP accounts for more than validation_max_ip_concentration of
    confirmed signals."""
    _, product_id = await _make_validating_product(session, niche="concentrated-signals")
    await _record_sent(session, product_id=product_id, count=10)
    # 6 confirmed signals, all from the same hashed IP -- 6/6 = 100% concentration.
    for i in range(6):
        await repository.insert_validation_signal(
            session,
            product_id=product_id,
            email=f"concentrated{i}@example.test",
            require_confirmation=False,
            submitter_ip_hash="same-ip-hash",
        )

    ctx = _ctx(
        provider=FakeProvider(name=Provider.GEMINI),
        workspace_root=tmp_path,
        validation_min_sample_size=10,
        validation_min_response_rate=0.05,
        validation_max_ip_concentration=0.5,
    )
    result = await dev_graph.run(ctx, session, _eval_task(product_id))

    assert "failed" in result
    product = await session.get(Product, product_id)
    assert product is not None
    assert product.status == ProductStatus.RETIRED.value

    interrupts = (
        (
            await session.execute(
                select(Interrupt).where(Interrupt.reason == InterruptReason.DATA_INTEGRITY.value)
            )
        )
        .scalars()
        .all()
    )
    assert len(interrupts) == 1
    assert interrupts[0].needs_reply is False


async def test_signals_spread_across_different_ips_are_not_flagged(
    session: AsyncSession, tmp_path: Path
) -> None:
    """The guard must not become a blanket veto: five confirmed signals from
    five different submitters is exactly the broad interest this gate exists
    to let through."""
    _, product_id = await _make_validating_product(session, niche="spread-signals")
    await _record_sent(session, product_id=product_id, count=10)
    for i in range(5):
        await repository.insert_validation_signal(
            session,
            product_id=product_id,
            email=f"spread{i}@example.test",
            require_confirmation=False,
            submitter_ip_hash=f"ip-hash-{i}",
        )

    ctx = _ctx(
        provider=FakeProvider(name=Provider.GEMINI),
        workspace_root=tmp_path,
        validation_min_sample_size=10,
        validation_min_response_rate=0.05,
        validation_max_ip_concentration=0.5,
    )
    result = await dev_graph.run(ctx, session, _eval_task(product_id))

    assert "passed" in result
    product = await session.get(Product, product_id)
    assert product is not None
    assert product.status == ProductStatus.PLANNED.value

    interrupts = (
        (
            await session.execute(
                select(Interrupt).where(Interrupt.reason == InterruptReason.DATA_INTEGRITY.value)
            )
        )
        .scalars()
        .all()
    )
    assert interrupts == []


async def test_unhashed_signals_never_trigger_the_concentration_check(
    session: AsyncSession, tmp_path: Path
) -> None:
    """When ABUSE_IP_HASH_SECRET is unset, every signal has submitter_ip_hash=None
    -- rows with no hash must never count toward concentration in either
    direction, or the check would be either meaningless or a false positive
    depending on how NULLs happened to group."""
    _, product_id = await _make_validating_product(session, niche="unhashed-signals")
    await _record_sent(session, product_id=product_id, count=10)
    for i in range(6):
        await repository.insert_validation_signal(
            session,
            product_id=product_id,
            email=f"unhashed{i}@example.test",
            require_confirmation=False,
        )

    ctx = _ctx(
        provider=FakeProvider(name=Provider.GEMINI),
        workspace_root=tmp_path,
        validation_min_sample_size=10,
        validation_min_response_rate=0.05,
        validation_max_ip_concentration=0.5,
    )
    result = await dev_graph.run(ctx, session, _eval_task(product_id))

    assert "passed" in result


# --- abuse controls: rate limiting ---------------------------------------------


async def test_interest_endpoint_rate_limits_per_ip(monkeypatch: pytest.MonkeyPatch) -> None:
    from pydantic import SecretStr

    from factory.settings import get_settings

    _settings_env(
        monkeypatch,
        FACTORY_DRY_RUN="1",
        ABUSE_IP_HASH_SECRET="test-secret",
        INTEREST_RATE_LIMIT_PER_IP_HOUR="2",
    )
    settings = get_settings()
    assert settings.abuse_ip_hash_secret == SecretStr("test-secret")
    assert settings.interest_rate_limit_per_ip_hour == 2

    headers = {"x-forwarded-for": "198.51.100.42"}
    async with await _interest_client() as client:
        first = await client.post(
            "/interest/does-not-exist", json={"email": "a@example.test"}, headers=headers
        )
        second = await client.post(
            "/interest/does-not-exist", json={"email": "b@example.test"}, headers=headers
        )
        third = await client.post(
            "/interest/does-not-exist", json={"email": "c@example.test"}, headers=headers
        )

    # The endpoint 404s on an unknown product either way -- the rate limit is a
    # dependency that runs *before* the route body, so it must trip regardless
    # of what the request would otherwise have done.
    assert first.status_code == 404
    assert second.status_code == 404
    assert third.status_code == 429


async def test_different_ips_are_independent_buckets(monkeypatch: pytest.MonkeyPatch) -> None:
    _settings_env(
        monkeypatch,
        FACTORY_DRY_RUN="1",
        ABUSE_IP_HASH_SECRET="test-secret",
        INTEREST_RATE_LIMIT_PER_IP_HOUR="1",
    )

    async with await _interest_client() as client:
        first = await client.post(
            "/interest/does-not-exist",
            json={"email": "a@example.test"},
            headers={"x-forwarded-for": "198.51.100.1"},
        )
        second = await client.post(
            "/interest/does-not-exist",
            json={"email": "b@example.test"},
            headers={"x-forwarded-for": "198.51.100.2"},
        )

    assert first.status_code == 404
    assert second.status_code == 404
