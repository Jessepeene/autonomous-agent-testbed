"""Database-backed behaviour.

Skipped unless ``DATABASE_URL`` points at a real Postgres — these assert on
``FOR UPDATE SKIP LOCKED`` claiming, ``ON CONFLICT`` idempotency, and CHECK
constraints, none of which any in-memory substitute reproduces faithfully.

    docker compose up -d postgres
    alembic upgrade head
    pytest tests/integration -q
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from factory.engines.compliance.minimize import ProvenanceMissing
from factory.settings import EngineName
from factory.state import repository
from factory.state.orm import Base
from factory.state.schemas import InterruptReason, ProductStatus, TaskKind, TaskStatus

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


async def test_enqueue_is_idempotent(session: AsyncSession) -> None:
    first = await repository.enqueue_task(
        session,
        kind=TaskKind.DISCOVER_OPPORTUNITIES,
        engine=EngineName.DISCOVERY,
        idempotency_key="discovery:cycle:1",
    )
    second = await repository.enqueue_task(
        session,
        kind=TaskKind.DISCOVER_OPPORTUNITIES,
        engine=EngineName.DISCOVERY,
        idempotency_key="discovery:cycle:1",
    )
    assert first is not None
    assert second is None, "the same logical work was queued twice"


async def test_claim_leases_a_task_exactly_once(session: AsyncSession) -> None:
    await repository.enqueue_task(
        session,
        kind=TaskKind.DISCOVER_OPPORTUNITIES,
        engine=EngineName.DISCOVERY,
        idempotency_key="claim-me",
    )
    claimed = await repository.claim_next_task(
        session, worker_id="w1", engines=[EngineName.DISCOVERY]
    )
    assert claimed is not None
    assert claimed.status == TaskStatus.CLAIMED.value
    assert claimed.claimed_by == "w1"

    again = await repository.claim_next_task(
        session, worker_id="w2", engines=[EngineName.DISCOVERY]
    )
    assert again is None, "a claimed task was handed to a second worker"


async def test_expired_lease_is_reclaimed_after_a_crash(session: AsyncSession) -> None:
    await repository.enqueue_task(
        session,
        kind=TaskKind.DISCOVER_OPPORTUNITIES,
        engine=EngineName.DISCOVERY,
        idempotency_key="reclaim-me",
    )
    task = await repository.claim_next_task(
        session, worker_id="dead-worker", engines=[EngineName.DISCOVERY]
    )
    assert task is not None
    task.lease_expires_at = datetime.now(UTC) - timedelta(minutes=1)
    await session.flush()

    assert await repository.reclaim_expired_leases(session) == 1
    recovered = await repository.claim_next_task(
        session, worker_id="live-worker", engines=[EngineName.DISCOVERY]
    )
    assert recovered is not None
    assert recovered.id == task.id


async def test_task_escalates_only_after_the_attempt_budget(session: AsyncSession) -> None:
    await repository.enqueue_task(
        session,
        kind=TaskKind.MAINTENANCE,
        engine=EngineName.SUPERVISOR,
        idempotency_key="failing",
    )
    task_id = None
    for attempt in range(1, 4):
        claimed = await repository.claim_next_task(
            session, worker_id="w", engines=[EngineName.SUPERVISOR]
        )
        if claimed is None:  # backoff window
            break
        task_id = claimed.id
        claimed.not_before = None
        exhausted = await repository.fail_task(session, task_id, f"boom {attempt}")
        assert exhausted is (attempt == 3)
    assert task_id is not None


async def test_ledger_refuses_to_go_negative(session: AsyncSession) -> None:
    await repository.append_ledger(
        session, external_event_id="evt_1", kind="revenue", amount_cents=1_000
    )
    with pytest.raises(ValueError, match="negative"):
        await repository.append_ledger(
            session, external_event_id="evt_2", kind="spend", amount_cents=-5_000
        )


async def test_ledger_ignores_a_replayed_stripe_event(session: AsyncSession) -> None:
    assert await repository.append_ledger(
        session, external_event_id="evt_dup", kind="revenue", amount_cents=500
    )
    assert not await repository.append_ledger(
        session, external_event_id="evt_dup", kind="revenue", amount_cents=500
    )
    balance, _ = await repository.current_balance(session)
    assert balance == 500


async def test_product_lock_pauses_paid_spend(session: AsyncSession) -> None:
    idea = await repository.upsert_idea(session, _scored_idea())
    assert idea is not None
    product_id = await repository.create_product(session, name="Test", idea_id=idea)

    assert await repository.is_paid_spend_paused(session, product_id) is False
    await repository.acquire_product_lock(
        session, product_id=product_id, holder=EngineName.DEVELOPMENT, reason="patching"
    )
    assert await repository.is_paid_spend_paused(session, product_id) is True

    await repository.release_product_lock(session, product_id)
    assert await repository.is_paid_spend_paused(session, product_id) is False


async def test_active_dev_count_drives_the_throttle(session: AsyncSession) -> None:
    idea = await repository.upsert_idea(session, _scored_idea())
    assert idea is not None
    product_id = await repository.create_product(session, name="Test", idea_id=idea)

    assert await repository.count_active_dev_tasks(session) == 0
    await repository.set_product_status(session, product_id, ProductStatus.BUILDING)
    assert await repository.count_active_dev_tasks(session) == 1
    await repository.set_product_status(session, product_id, ProductStatus.LIVE)
    assert await repository.count_active_dev_tasks(session) == 0


async def test_pending_dev_retries_counts_build_mvp_and_patch_for_active_products(
    session: AsyncSession,
) -> None:
    """Regression coverage for the build-capacity deadlock: a pending BUILD_MVP or
    PATCH_PRODUCT task whose product is already BUILDING/REVIEWING/PATCHING must be
    recognised as a retry, not a fresh build -- the count that used to only look at
    PATCH_PRODUCT missed a BUILD_MVP retry entirely (a BUILD_MVP that throws leaves
    its product stuck BUILDING, never reset on failure)."""
    from factory.state.schemas import ScoredIdea

    def _idea(suffix: str) -> ScoredIdea:
        # upsert_idea dedups on fingerprint(problem_statement, target_niche), not
        # source_url -- target_niche must differ for these to be distinct ideas.
        base = _scored_idea()
        return base.model_copy(update={"target_niche": f"{base.target_niche} ({suffix})"})

    idea_a = await repository.upsert_idea(session, _idea("a"))
    idea_b = await repository.upsert_idea(session, _idea("b"))
    assert idea_a is not None
    assert idea_b is not None
    stuck_building = await repository.create_product(session, name="Stuck", idea_id=idea_a)
    stuck_patching = await repository.create_product(session, name="Patching", idea_id=idea_b)
    idea_c = await repository.upsert_idea(session, _idea("c"))
    assert idea_c is not None
    not_yet_started = await repository.create_product(session, name="Fresh", idea_id=idea_c)

    await repository.set_product_status(session, stuck_building, ProductStatus.BUILDING)
    await repository.set_product_status(session, stuck_patching, ProductStatus.PATCHING)

    assert await repository.count_pending_dev_retries(session) == 0

    await repository.enqueue_task(
        session,
        kind=TaskKind.BUILD_MVP,
        engine=EngineName.DEVELOPMENT,
        idempotency_key="retry-build-1",
        idea_id=idea_a,
        product_id=stuck_building,
    )
    assert await repository.count_pending_dev_retries(session) == 1

    await repository.enqueue_task(
        session,
        kind=TaskKind.PATCH_PRODUCT,
        engine=EngineName.DEVELOPMENT,
        idempotency_key="retry-patch-1",
        idea_id=idea_b,
        product_id=stuck_patching,
    )
    assert await repository.count_pending_dev_retries(session) == 2

    # A BUILD_MVP for a product not yet in an active-dev status is a genuinely new
    # build, not a retry -- must not be counted.
    await repository.enqueue_task(
        session,
        kind=TaskKind.BUILD_MVP,
        engine=EngineName.DEVELOPMENT,
        idempotency_key="fresh-build-1",
        idea_id=idea_c,
        product_id=not_yet_started,
    )
    assert await repository.count_pending_dev_retries(session) == 2


async def test_upsert_idea_persists_the_given_evidence_count(session: AsyncSession) -> None:
    idea_id = await repository.upsert_idea(session, _scored_idea(), evidence_count=7)
    assert idea_id is not None
    await repository.set_idea_status(session, idea_id, "approved")

    validated = await repository.next_validated_idea(session)

    assert validated is not None
    assert validated.evidence_count == 7


async def test_upsert_idea_defaults_evidence_count_to_zero(session: AsyncSession) -> None:
    idea_id = await repository.upsert_idea(session, _scored_idea())
    assert idea_id is not None
    await repository.set_idea_status(session, idea_id, "approved")

    validated = await repository.next_validated_idea(session)

    assert validated is not None
    assert validated.evidence_count == 0


async def test_lead_insert_drops_extraneous_fields(session: AsyncSession) -> None:
    record = await repository.insert_lead(
        session,
        {
            "first_name": "Ada",
            "last_name": "Lovelace",
            "job_title": "Head of Ops",
            "company": "Engines BV",
            "work_email": "ada@engines.test",
            "source_url": "https://engines.test/team",
            "scraped_at": datetime.now(UTC),
            "phone": "+31612345678",
            "home_address": "Somestraat 4",
        },
        lia_passed=True,
    )
    assert "phone" not in record.model_dump()

    stored = await repository.leads_for_outreach(session)
    assert len(stored) == 1
    assert stored[0].source_url == "https://engines.test/team"
    assert not hasattr(stored[0], "phone")


async def test_lead_without_provenance_is_rejected(session: AsyncSession) -> None:
    with pytest.raises(ProvenanceMissing):
        await repository.insert_lead(
            session,
            {
                "first_name": "A",
                "last_name": "B",
                "job_title": "C",
                "company": "D",
                "work_email": "a@b.test",
            },
            lia_passed=True,
        )


async def test_open_interrupts_collapse_by_subject(session: AsyncSession) -> None:
    first = await repository.open_interrupt(
        session,
        reason=InterruptReason.MISSING_CREDENTIAL,
        subject="TELEGRAM_BOT_TOKEN is not configured",
        detail="set it",
        headline="Test escalation",
        reply_hint="No reply needed here — set the credential and restart the factory.",
        needs_reply=False,
    )
    second = await repository.open_interrupt(
        session,
        reason=InterruptReason.MISSING_CREDENTIAL,
        subject="TELEGRAM_BOT_TOKEN is not configured",
        detail="set it",
        headline="Test escalation",
        reply_hint="No reply needed here — set the credential and restart the factory.",
        needs_reply=False,
    )
    assert first is not None
    assert second is None
    assert len(await repository.undelivered_interrupts(session)) == 1


async def _product_scoped_interrupt(
    session: AsyncSession, *, subject: str, product_id: str | None
) -> str:
    interrupt_id = await repository.open_interrupt(
        session,
        reason=InterruptReason.MISSING_CREDENTIAL,
        subject=subject,
        detail="...",
        headline="Test escalation",
        reply_hint="Reply with your answer.",
        product_id=product_id,
    )
    assert interrupt_id is not None
    return interrupt_id


async def test_list_open_interrupts_groups_by_product(session: AsyncSession) -> None:
    """Same-product escalations must read together, not interleaved with
    unrelated ones -- interrupts are raised in an order that deliberately does
    not match product grouping, to prove the query itself groups them rather
    than happening to already be in order."""
    idea_a = await repository.upsert_idea(session, _scored_idea())
    # upsert_idea dedupes on fingerprint(problem_statement, target_niche) --
    # a second idea needs a genuinely different niche or it collides with the
    # first and returns None instead of a second row.
    idea_b = await repository.upsert_idea(
        session, _scored_idea().model_copy(update={"target_niche": "A different niche entirely"})
    )
    assert idea_a is not None and idea_b is not None
    product_a = await repository.create_product(session, name="Product A", idea_id=idea_a)
    product_b = await repository.create_product(session, name="Product B", idea_id=idea_b)

    await _product_scoped_interrupt(session, subject="b-first", product_id=product_b)
    await _product_scoped_interrupt(session, subject="a-first", product_id=product_a)
    await _product_scoped_interrupt(session, subject="no-product", product_id=None)
    await _product_scoped_interrupt(session, subject="b-second", product_id=product_b)
    await session.flush()

    rows = await repository.list_open_interrupts(session, limit=10)

    assert len(rows) == 4
    product_ids = [row.product_id for row in rows]
    # Non-product-scoped rows sort last (NULLS LAST).
    assert product_ids[-1] is None
    # Every row for the same product is contiguous -- no other product's (or no
    # product's) row appears between two rows sharing a product_id.
    for pid in (product_a, product_b):
        positions = [i for i, p in enumerate(product_ids) if p == pid]
        assert positions == list(range(positions[0], positions[0] + len(positions)))


async def test_stale_open_interrupts_also_groups_by_product(session: AsyncSession) -> None:
    idea_a = await repository.upsert_idea(session, _scored_idea())
    idea_b = await repository.upsert_idea(
        session, _scored_idea().model_copy(update={"target_niche": "A different niche entirely"})
    )
    assert idea_a is not None and idea_b is not None
    product_a = await repository.create_product(session, name="Product A", idea_id=idea_a)
    product_b = await repository.create_product(session, name="Product B", idea_id=idea_b)

    await _product_scoped_interrupt(session, subject="b-first", product_id=product_b)
    await _product_scoped_interrupt(session, subject="a-first", product_id=product_a)
    await _product_scoped_interrupt(session, subject="b-second", product_id=product_b)
    await session.flush()

    rows = await repository.stale_open_interrupts(session, older_than=timedelta(seconds=0))

    assert len(rows) == 3
    product_ids = [row.product_id for row in rows]
    for pid in (product_a, product_b):
        positions = [i for i, p in enumerate(product_ids) if p == pid]
        assert positions == list(range(positions[0], positions[0] + len(positions)))


def _scored_idea():
    from factory.state.schemas import ScoredIdea

    return ScoredIdea(
        problem_statement="Bookkeepers reconcile invoices by hand every single week.",
        target_niche="Bookkeepers at small Dutch accounting firms",
        source_url="https://example.test/thread",
        monetization_hypothesis="EUR 49/month per seat, bought after the first month-end close.",
        pain_severity=0.8,
        willingness_to_pay=0.7,
        build_feasibility=0.6,
        adoption_ease=0.8,
    )
