"""One idea, driven through the entire funnel under the staging harness.

This is the automated half of the pre-launch validation plan's final phase (the
manual half -- real domain, real bounded Apollo/Hunter, a real Vercel project --
lives in DEPLOYMENT_CHECKLIST.md's Phase 7 runbook, since none of those can be
exercised by an automated, offline test).

What "under the staging harness" means here concretely: the Anthropic slot is
the REAL `StagingCodeProvider` (`llm/providers/staging.py`) -- the same
deterministic, functionally-real fixture provider `settings.
staging_mock_expensive_calls` swaps in for a real staging run, so this test
exercises the actual generated-content shape (SEO markers, analytics beacon,
Stripe wiring, auth) and the real Bug Council scanners against it, not a
placeholder. The Gemini slot is `FakeProvider`, scripted per call -- Discovery/
Compliance decisions are structured-output calls cheap enough to script
directly, and a real network call here would make this test flaky and
non-free to run, defeating the point of an automated regression test.

What is deliberately faked instead of real, and why:
- The Docker sandbox: a scripted always-passing fake. The real sandbox install/
  test cycle is covered by the (manually invoked) integration suite and the
  live deployment verification workflow in DEPLOYMENT_CHECKLIST.md; requiring a
  Docker daemon inside this test would make it unable to run in most CI
  environments, and the staging fixtures are already known-passing content
  (see staging.py's own docstring) -- the sandbox here is not what's under test.
- Vercel deploy: `factory_dry_run=True` makes this a stub URL, matching the
  existing FACTORY_DRY_RUN behaviour exactly. A real deploy needs a real Vercel
  project -- Phase 7's manual runbook.
- Lead sourcing / outreach: `validation_signals` and `sent` outreach counts are
  seeded directly rather than run through Apollo/Hunter or SMTP. Real (bounded)
  Apollo/Hunter and a real outreach send are also Phase 7's manual runbook --
  see the original plan's explicit decision to keep outreach stubbed even in a
  live staging run.

Needs Postgres:

    docker compose up -d postgres
    alembic upgrade head
    pytest tests/e2e -q
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from factory.engines.compliance import graph as compliance_graph
from factory.engines.development import graph as dev_graph
from factory.hitl.interrupts import collect_replies
from factory.llm.client import LLMClient
from factory.llm.providers.base import Provider
from factory.llm.providers.fake import FakeProvider
from factory.llm.providers.staging import StagingCodeProvider
from factory.llm.router import ProviderRegistry
from factory.sandbox.runner import SandboxResult
from factory.settings import EngineName, Settings
from factory.state import repository
from factory.state.orm import (
    ABTest,
    Base,
    ChannelExperiment,
    Interrupt,
    Lead,
    OutreachLog,
    Product,
    Task,
    new_id,
)
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
    not DATABASE_URL, reason="DATABASE_URL is not set; e2e tests need Postgres"
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


class _AlwaysPassingSandbox:
    """The staging fixtures are known-passing content by construction (see
    staging.py) -- what this test exercises is the pipeline's orchestration
    logic around a real generation, not Docker itself. Real sandboxed
    execution is covered elsewhere (tests/integration's manually-invoked
    suite, and the live deployment verification workflow)."""

    available = True

    async def execute(self, command: str, *, workspace: str, **kwargs: object) -> SandboxResult:
        if "pip install" in command:
            return SandboxResult(exit_code=0, stdout="installed", stderr="")
        return SandboxResult(exit_code=0, stdout="5 passed", stderr="")


class _ScriptedNotifier:
    def __init__(self) -> None:
        self._replies: list[str] = []
        self.sent: list[str] = []

    def queue(self, reply: str) -> None:
        self._replies.append(reply)

    @property
    def available(self) -> bool:
        return True

    async def send(self, text: str) -> bool:
        self.sent.append(text)
        return True

    async def poll_replies(self) -> list[str]:
        replies, self._replies = self._replies, []
        return replies


def _ctx(
    *, workspace_root: Path, gemini: FakeProvider, notifier: _ScriptedNotifier, **overrides: object
) -> FactoryContext:
    settings = Settings(
        database_url=DATABASE_URL,
        gemini_api_key="test-key",
        workspace_root=workspace_root,
        factory_dry_run=True,
        staging_mock_expensive_calls=True,
        # Single-idea concurrency, matching the pre-launch plan's staging-run
        # configuration -- this test drives one idea explicitly rather than
        # exercising the admission-control gates themselves (covered by
        # test_validation_gate.py's own capacity tests).
        max_concurrent_builds=1,
        max_concurrent_validations=1,
        validation_min_sample_size=10,
        validation_min_response_rate=0.05,
        ab_test_min_views_per_arm=30,
        **overrides,
    )
    llm = LLMClient(
        ProviderRegistry({Provider.GEMINI: gemini, Provider.ANTHROPIC: StagingCodeProvider()}),
        settings=settings,
        backoff_base=0.0,
    )
    return FactoryContext(
        settings=settings,
        llm=llm,
        notifier=notifier,
        sandbox=_AlwaysPassingSandbox(),  # type: ignore[arg-type]
        enabled_engines={EngineName.COMPLIANCE, EngineName.DEVELOPMENT, EngineName.MARKETING},
        docker_available=True,
        worker_id="e2e-full-funnel",
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


def _task(kind: TaskKind, *, product_id: str, payload: dict[str, object] | None = None) -> Task:
    return Task(
        id=new_id(),
        kind=kind.value,
        engine=EngineName.DEVELOPMENT.value,
        status=TaskStatus.CLAIMED.value,
        priority=90,
        payload={"product_id": product_id, **(payload or {})},
        product_id=product_id,
        attempt_count=1,
    )


async def _record_sent_outreach(session: AsyncSession, *, product_id: str, count: int) -> None:
    """Stands in for real (bounded) Apollo/Hunter sourcing + a real outreach
    send -- both explicitly out of scope for an automated, offline test. Same
    shape as tests/integration/test_validation_gate.py's `_record_sent`."""
    lead_id = new_id()
    session.add(
        Lead(
            id=lead_id,
            first_name="Test",
            last_name="Lead",
            job_title="Head of Finance",
            company="Acme BV",
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


async def test_one_idea_through_the_full_funnel_under_staging(
    session: AsyncSession, tmp_path: Path
) -> None:
    gemini = FakeProvider(name=Provider.GEMINI)
    notifier = _ScriptedNotifier()
    ctx = _ctx(workspace_root=tmp_path, gemini=gemini, notifier=notifier)

    # --- One idea clears Compliance -------------------------------------------
    # Seeded directly rather than run through Discovery's own scraping/LLM
    # idea-generation call, which is a separate concern already covered by
    # Discovery's own test suite -- this test starts from "one approved idea."
    idea = ScoredIdea(
        problem_statement="Bookkeepers reconcile invoices by hand every week.",
        target_niche="e2e-full-funnel",
        source_url="https://example.test/thread",
        monetization_hypothesis="Bookkeepers pay EUR 49/month per seat.",
        pain_severity=0.8,
        willingness_to_pay=0.7,
        build_feasibility=0.6,
        adoption_ease=0.8,
    )
    idea_id = await repository.upsert_idea(session, idea)
    assert idea_id is not None

    gemini.register(
        ComplianceAssessment,
        ComplianceAssessment(
            status=ComplianceStatus.APPROVED,
            justification="Lawful, no special-category data involved in this niche.",
            confidence=0.9,
        ),
    )
    await compliance_graph.run(ctx, session, _compliance_task(idea_id))

    products = (
        (await session.execute(select(Product).where(Product.idea_id == idea_id))).scalars().all()
    )
    assert len(products) == 1
    product_id = products[0].id
    assert products[0].status == ProductStatus.VALIDATING.value

    landing_tasks = (
        (
            await session.execute(
                select(Task).where(
                    Task.product_id == product_id,
                    Task.kind == TaskKind.BUILD_LANDING_PAGE.value,
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(landing_tasks) == 1

    # --- Validation: a real staging-fixture landing page, deployed -----------
    result = await dev_graph.run(ctx, session, landing_tasks[0])
    assert "deployed" in result
    product = await session.get(Product, product_id)
    assert product is not None
    assert product.status == ProductStatus.VALIDATING.value
    assert product.deploy_url is not None

    # --- Real interest, real response-rate math -> BUILD_MVP -----------------
    await _record_sent_outreach(session, product_id=product_id, count=20)
    for i in range(5):  # 5/20 = 25%, comfortably clears the default 5% bar
        await repository.insert_validation_signal(
            session, product_id=product_id, email=f"interested{i}@example.test"
        )
    eval_validation = _task(TaskKind.EVALUATE_VALIDATION, product_id=product_id)
    result = await dev_graph.run(ctx, session, eval_validation)
    assert "passed" in result

    build_tasks = (
        (
            await session.execute(
                select(Task).where(
                    Task.product_id == product_id, Task.kind == TaskKind.BUILD_MVP.value
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(build_tasks) == 1

    # --- The real MVP fixture: auth, Stripe wiring, SEO, Bug Council, deploy -
    result = await dev_graph.run(ctx, session, build_tasks[0])
    assert "approved and deployed" in result
    product = await session.get(Product, product_id)
    assert product is not None
    assert product.status == ProductStatus.LIVE.value
    assert product.domain_status == "pending"  # asked for a domain, once

    # --- Domain round-trip: interrupt raised, human replies, attach queued ---
    interrupts = (
        (
            await session.execute(
                select(Interrupt).where(Interrupt.reason == InterruptReason.DOMAIN_REQUIRED.value)
            )
        )
        .scalars()
        .all()
    )
    assert len(interrupts) == 1
    notifier.queue(f"#{interrupts[0].id[:8]} e2e-full-funnel.example.com")
    resolved = await collect_replies(session, notifier)
    assert resolved == 1
    product = await session.get(Product, product_id)
    assert product is not None
    assert product.custom_domain == "e2e-full-funnel.example.com"

    attach_tasks = (
        (
            await session.execute(
                select(Task).where(
                    Task.product_id == product_id, Task.kind == TaskKind.ATTACH_DOMAIN.value
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(attach_tasks) == 1  # the real Vercel call itself is Phase 7's manual runbook

    # --- A live AB test: start it, seed a clear result, confirm the verdict --
    ab_test_id = await dev_graph.start_ab_test(
        session,
        product_id=product_id,
        idea_id=idea_id,
        variant_a_brief="Emphasize how many hours per week this saves.",
        variant_b_brief="Emphasize the fixed monthly price versus a bookkeeper's hourly rate.",
    )
    assert ab_test_id is not None
    ab_test = await session.get(ABTest, ab_test_id)
    assert ab_test is not None
    for _ in range(200):
        await repository.record_page_view(session, product_id=product_id, path="/", variant="a")
    for i in range(8):
        await repository.insert_validation_signal(
            session, product_id=product_id, email=f"ab-a-{i}@example.test", variant="a"
        )
    for _ in range(200):
        await repository.record_page_view(session, product_id=product_id, path="/", variant="b")
    for i in range(80):
        await repository.insert_validation_signal(
            session, product_id=product_id, email=f"ab-b-{i}@example.test", variant="b"
        )
    eval_ab = Task(
        id=new_id(),
        kind=TaskKind.EVALUATE_AB_TEST.value,
        engine=EngineName.DEVELOPMENT.value,
        status=TaskStatus.CLAIMED.value,
        priority=110,
        payload={"ab_test_id": ab_test_id},
        product_id=product_id,
        attempt_count=1,
    )
    result = await dev_graph.run(ctx, session, eval_ab)
    assert "concluded" in result
    assert "winner=b" in result

    # --- GROWTH_REVIEW's own bookkeeping still runs under dry-run ------------
    # GROWTH_ANALYST itself is a mutating role and is correctly refused here --
    # FACTORY_DRY_RUN is set for this whole test (it is what stubs the Vercel
    # deploy above) -- see test_agent_spawner.py's own dry-run gating tests for
    # the agent-loop behaviour itself. What this confirms is that
    # _growth_review still marks the product reviewed even when the spawn is
    # refused, so the periodic backstop sweep does not re-queue it forever.
    growth_task = _task(TaskKind.GROWTH_REVIEW, product_id=product_id)
    result = await dev_graph.run(ctx, session, growth_task)
    assert "growth_review" in result
    product = await session.get(Product, product_id)
    assert product is not None
    assert product.last_growth_review_at is not None

    # Nothing here ever escalated to a human outside the one expected domain
    # request, and CHANNEL_SCOUT was never invoked in this run -- both quiet.
    open_interrupts = (
        (await session.execute(select(Interrupt).where(Interrupt.status == "open"))).scalars().all()
    )
    assert open_interrupts == []
    channel_experiments = (await session.execute(select(ChannelExperiment))).scalars().all()
    assert channel_experiments == []
