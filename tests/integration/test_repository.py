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
from factory.state.orm import Base, Issue, Task
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


async def test_used_idea_source_urls_returns_every_stored_url(session: AsyncSession) -> None:
    from factory.state.schemas import ScoredIdea

    def _idea(suffix: str, url: str) -> ScoredIdea:
        base = _scored_idea()
        return base.model_copy(
            update={"target_niche": f"{base.target_niche} ({suffix})", "source_url": url}
        )

    assert await repository.upsert_idea(session, _idea("a", "https://a.test/1")) is not None
    assert await repository.upsert_idea(session, _idea("b", "https://a.test/2")) is not None

    assert await repository.used_idea_source_urls(session) == {
        "https://a.test/1",
        "https://a.test/2",
    }


async def test_used_idea_source_urls_strips_trailing_slashes(session: AsyncSession) -> None:
    """Matches `discovery/graph.py::_exclude_already_mined`'s own normalisation --
    a raw signal URL never has pydantic's `HttpUrl` trailing-slash addition."""
    idea = _scored_idea().model_copy(update={"source_url": "https://a.test/1/"})
    assert await repository.upsert_idea(session, idea) is not None

    assert await repository.used_idea_source_urls(session) == {"https://a.test/1"}


async def test_used_idea_source_urls_is_empty_with_no_ideas(session: AsyncSession) -> None:
    assert await repository.used_idea_source_urls(session) == set()


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


async def test_self_serve_price_usd_round_trips_through_validated_idea(
    session: AsyncSession,
) -> None:
    """Phase 5: the price the discriminator targeted used to be discarded after
    the PLG gate -- this is the read side of carrying it through so codegen can
    actually charge it. Exercised via `next_validated_idea` specifically because
    that's one of the three `ValidatedIdea` construction sites this phase had to
    update (`extra="forbid"` makes a partial miss a hard failure, not a silent
    default)."""
    from factory.state.schemas import ScoredIdea

    idea = ScoredIdea(
        problem_statement="Bookkeepers reconcile invoices by hand every single week.",
        target_niche="Bookkeepers at small Dutch accounting firms (price case)",
        source_url="https://example.test/thread",
        monetization_hypothesis="EUR 49/month per seat.",
        pain_severity=0.8,
        willingness_to_pay=0.7,
        build_feasibility=0.6,
        adoption_ease=0.8,
        self_serve_price_usd=49.0,
    )
    idea_id = await repository.upsert_idea(session, idea)
    assert idea_id is not None
    await repository.set_idea_status(session, idea_id, "approved")

    validated = await repository.next_validated_idea(session)

    assert validated is not None
    assert validated.self_serve_price_usd == 49.0


async def test_self_serve_price_usd_defaults_to_zero(session: AsyncSession) -> None:
    idea_id = await repository.upsert_idea(session, _scored_idea())
    assert idea_id is not None
    await repository.set_idea_status(session, idea_id, "approved")

    validated = await repository.next_validated_idea(session)

    assert validated is not None
    assert validated.self_serve_price_usd == 0.0


async def test_get_idea_returns_the_row_by_id(session: AsyncSession) -> None:
    idea_id = await repository.upsert_idea(session, _scored_idea())
    assert idea_id is not None
    await session.flush()

    row = await repository.get_idea(session, idea_id)

    assert row is not None
    assert row.id == idea_id


async def test_get_idea_returns_none_for_an_unknown_id(session: AsyncSession) -> None:
    assert await repository.get_idea(session, "not-a-real-id") is None


async def test_upsert_idea_persists_the_per_dimension_score_breakdown(
    session: AsyncSession,
) -> None:
    """Phase 4: `ideas` gained per-dimension columns behind the blended `score`
    specifically so a future weights change can be backtested against real
    historical ideas -- this is the write side of that, and the read-back
    confirms `upsert_idea` doesn't silently drop the new fields the way it would
    have if the new columns existed but nothing wrote to them."""
    from factory.state.orm import Idea
    from factory.state.schemas import PlatformDependency, ReplacementMode, ScoredIdea

    idea = ScoredIdea(
        problem_statement="Bookkeepers reconcile invoices by hand every single week.",
        target_niche="Bookkeepers at small Dutch accounting firms (defensibility case)",
        source_url="https://example.test/thread",
        monetization_hypothesis="EUR 49/month per seat, bought after the first close.",
        pain_severity=0.8,
        willingness_to_pay=0.7,
        build_feasibility=0.6,
        adoption_ease=0.8,
        defensibility_reasoning="Accumulates reconciliation history per client over time.",
        defensibility=0.65,
        platform_dependency=PlatformDependency.OPTIONAL,
        replacement_mode=ReplacementMode.REPLACES,
    )

    idea_id = await repository.upsert_idea(session, idea, score=idea.score)
    assert idea_id is not None
    await session.flush()

    row = await session.get(Idea, idea_id)

    assert row is not None
    assert row.pain_severity == 0.8
    assert row.willingness_to_pay == 0.7
    assert row.build_feasibility == 0.6
    assert row.adoption_ease == 0.8
    assert row.defensibility == 0.65
    assert row.platform_dependency == "optional"
    assert row.replacement_mode == "replaces"


async def test_upsert_idea_leaves_per_dimension_columns_null_for_a_defaulted_idea(
    session: AsyncSession,
) -> None:
    """A `ScoredIdea` built without ever hearing about defensibility (every
    pre-Phase-4 test and caller) must still upsert cleanly -- the columns just
    hold whatever the defaulted field values are, not garbage or a crash."""
    from factory.state.orm import Idea

    idea_id = await repository.upsert_idea(session, _scored_idea())
    assert idea_id is not None
    await session.flush()

    row = await session.get(Idea, idea_id)

    assert row is not None
    assert row.defensibility == 0.0
    assert row.platform_dependency == "unknown"
    assert row.replacement_mode == "unknown"


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


async def test_design_sees_a_product_while_it_is_still_being_built(
    session: AsyncSession,
) -> None:
    """The scoping bug, and the reason it was invisible.

    `design_domain` looked only at VALIDATING and LIVE, which excludes every state
    a product is in *while it is being built* -- PLANNED, BUILDING, REVIEWING,
    PATCHING. That made the department structurally unable to do the thing its own
    storage describes: design guidelines are "a standard written before the work,
    that the builder works to", and a department that only sees shipped products can
    only ever write them afterwards.

    It surfaced as silence, not as an error. The manager ran, saw an empty domain,
    and reported nothing worth doing -- indistinguishable from a department that
    genuinely had nothing to do.
    """
    from factory.state.schemas import ScoredIdea

    def _idea(suffix: str) -> ScoredIdea:
        base = _scored_idea()
        return base.model_copy(update={"target_niche": f"{base.target_niche} ({suffix})"})

    ideas = [await repository.upsert_idea(session, _idea(s)) for s in ("b", "l", "r")]
    assert all(i is not None for i in ideas)
    building = await repository.create_product(session, name="Still Building", idea_id=ideas[0])
    live = await repository.create_product(session, name="Shipped", idea_id=ideas[1])
    dead = await repository.create_product(session, name="Retired", idea_id=ideas[2])
    await repository.set_product_status(session, building, ProductStatus.BUILDING)
    await repository.set_product_status(session, live, ProductStatus.LIVE)
    await repository.set_product_status(session, dead, ProductStatus.RETIRED)

    domain = await repository.design_domain(session)
    seen = {s.id for s in domain.without_guidelines}

    assert building in seen, "a product being built is exactly when guidelines matter"
    assert live in seen
    # The one exclusion that needs no judgement: nobody will look at it again.
    assert dead not in seen


async def test_the_manager_is_told_what_it_can_actually_act_on(
    session: AsyncSession,
) -> None:
    """Status and deployment are carried, not filtered on.

    A product with nothing deployed cannot be *reviewed* -- there is nothing to look
    at -- but its guidelines are worth writing. Deciding which of those applies is
    the manager's judgement, so the domain states the facts rather than handing it a
    shortlist somebody else narrowed.
    """
    idea = await repository.upsert_idea(session, _scored_idea())
    assert idea is not None
    product = await repository.create_product(session, name="No Page Yet", idea_id=idea)
    await repository.set_product_status(session, product, ProductStatus.PLANNED)

    subject = next(
        s for s in (await repository.design_domain(session)).without_guidelines if s.id == product
    )
    assert subject.status == ProductStatus.PLANNED.value
    assert subject.deployed is False

    await repository.set_product_status(
        session, product, ProductStatus.LIVE, deploy_url="https://example.com"
    )
    subject = next(
        s for s in (await repository.design_domain(session)).without_guidelines if s.id == product
    )
    assert subject.deployed is True


async def test_a_brief_says_who_it_is_for_and_whether_anyone_wants_it(
    session: AsyncSession,
) -> None:
    """The two facts three live manager runs blocked on, both of which are rows.

    The Design manager stopped on "who is this for" and "does anyone want it", then
    asserted an answer it had never been given. Both were in the database the whole
    time; nothing assembled them where a decision could see them.
    """
    from factory.handbook import brief_products

    idea = await repository.upsert_idea(session, _scored_idea())
    assert idea is not None
    product = await repository.create_product(session, name="Shardline", idea_id=idea)

    brief = await brief_products(session, [product])

    assert product in brief
    assert "for:" in brief
    # The absence that matters: nobody has been told about it, so zero interest is
    # not evidence against the product. Stated, not left as a bare zero.
    assert "nobody has been told about it yet" in brief


async def test_zero_interest_reads_differently_before_and_after_outreach(
    session: AsyncSession,
) -> None:
    """Opposite findings that are identical as a single number.

    Zero interest after a thousand emails is a real signal; zero interest after none
    is the absence of evidence. Collapsing them is how a product nobody was told
    about gets retired, or one real people declined gets kept.
    """
    from factory.handbook import product_dossier

    idea = await repository.upsert_idea(session, _scored_idea())
    assert idea is not None
    product = await repository.create_product(session, name="Shardline", idea_id=idea)

    untold = await product_dossier(session, product)
    assert "not evidence against it" in untold

    interest, reach = await repository.product_traction(session, product)
    assert (interest, reach) == (0, 0)


async def test_a_brief_carries_no_addresses(session: AsyncSession) -> None:
    """Briefs travel around the company; real emails must not travel with them.

    `ValidationSignal.email` and `Lead.work_email` are real addresses, and the schema
    already minimises deliberately -- `Customer` carries no email and no name. A
    briefing that leaks PII is a data-protection problem wearing a helpful hat.
    """
    from factory.handbook import brief_products

    idea = await repository.upsert_idea(session, _scored_idea())
    assert idea is not None
    product = await repository.create_product(session, name="Shardline", idea_id=idea)
    await repository.insert_validation_signal(
        session, product_id=product, email="someone@example.com", submitter_ip_hash="h"
    )

    brief = await brief_products(session, [product])
    assert "someone@example.com" not in brief
    assert "@" not in brief.split("for:")[-1].split("status:")[0]


async def test_a_task_that_parked_itself_is_not_then_marked_done(
    session: AsyncSession,
) -> None:
    """The bug that would have silently disabled the whole comms channel.

    `_run_engine` calls `complete_task` unconditionally the moment a runner returns.
    A runner that parks its own work -- defers itself to PENDING with a future
    `not_before` because it is waiting on an answer -- also returns normally, so
    without a guard "the runner returned" and "the work is finished" are one fact.

    They are not, and completing a parked task is worse than a no-op: it never
    resumes, `answer_question` then declines to release it (releasing only applies to
    pending or deferred work), and the answer arrives for a task that will never read
    it.

    Every `defer_task` caller used to live in the supervisor's scheduling path,
    outside the runner, so the conflict could not arise. `park_until_answered` is the
    first to defer from inside one.
    """
    from datetime import UTC, datetime, timedelta

    task_id = await repository.enqueue_task(
        session,
        kind=TaskKind.DESIGN_MANAGE,
        engine=EngineName.DESIGN,
        idempotency_key="park-then-complete",
        payload={},
    )
    assert task_id is not None
    claimed = await repository.claim_next_task(
        session, worker_id="w1", engines=[EngineName.DESIGN]
    )
    assert claimed is not None

    # What a runner does when it asks a question and waits for the answer.
    await repository.defer_task(
        session,
        task_id,
        not_before=datetime.now(UTC) + timedelta(hours=6),
        reason="waiting on a question",
    )
    # What `_run_engine` does a line later, whatever the runner did.
    assert await repository.complete_task(session, task_id) is False

    parked = await session.get(Task, task_id)
    assert parked is not None
    assert parked.status == TaskStatus.PENDING.value, "the park must survive"
    assert parked.not_before is not None, "and it must still be waiting"


async def test_an_ordinary_task_still_completes(session: AsyncSession) -> None:
    """The guard must not break the path every task takes."""
    task_id = await repository.enqueue_task(
        session,
        kind=TaskKind.DESIGN_MANAGE,
        engine=EngineName.DESIGN,
        idempotency_key="ordinary-completion",
        payload={},
    )
    assert task_id is not None
    assert await repository.claim_next_task(
        session, worker_id="w1", engines=[EngineName.DESIGN]
    )

    assert await repository.complete_task(session, task_id) is True
    done = await session.get(Task, task_id)
    assert done is not None and done.status == TaskStatus.DONE.value


async def test_an_accepted_flaw_stops_blocking_and_stays_accepted(
    session: AsyncSession,
) -> None:
    """The defect that stopped a finished product shipping.

    The release judge accepted two findings as "design choice, not functional" and
    the next pass re-filed them as blocking, unchanged, then reported that the
    blocking set was not shrinking. `accepted_issues` was free text written to a log
    and read by nothing, while its own contract called it "what makes 'done'
    reachable".

    Two properties, and the second is the one that makes it durable: an accepted
    flaw stops counting, and re-detecting it does not quietly resurrect it.
    """
    idea = await repository.upsert_idea(session, _scored_idea())
    assert idea is not None
    product = await repository.create_product(session, name="Shardline", idea_id=idea)

    issue, created = await repository.record_issue(
        session,
        product_id=product,
        kind="ui",
        severity="high",
        title="The page loads no webfont",
        location="index.html",
        found_by="requirements_gap_check",
    )
    assert created
    issue_id = issue.id
    assert len(await repository.open_issues_for(session, product)) == 1

    applied = await repository.accept_issues(
        session,
        product_id=product,
        accepted=[(issue_id[:8], "A design choice that does not affect validation.")],
        decided_by_task_id="release-1",
    )
    assert applied == 1
    assert await repository.open_issues_for(session, product) == [], "no longer blocking"

    # The next verification pass finds the same thing, because it is still true.
    await repository.record_issue(
        session,
        product_id=product,
        kind="ui",
        severity="high",
        title="The page loads no webfont",
        location="index.html",
        found_by="requirements_gap_check",
    )
    still = await repository.open_issues_for(session, product)
    assert still == [], "re-detecting an accepted flaw must not resurrect it"

    row = await session.get(Issue, issue_id)
    assert row is not None
    assert row.status == "wont_fix"
    assert row.times_seen == 2, "remembered, not forgotten"
    assert "does not affect validation" in (row.resolution or "")


async def test_an_invented_reference_accepts_nothing(session: AsyncSession) -> None:
    """A judge that invents a reference has accepted nothing.

    Acting on a near-match would silence a defect nobody chose to accept, which is
    worse than the acceptance failing to apply.
    """
    idea = await repository.upsert_idea(session, _scored_idea())
    assert idea is not None
    product = await repository.create_product(session, name="Shardline", idea_id=idea)
    await repository.record_issue(
        session,
        product_id=product,
        kind="ui",
        severity="high",
        title="The page loads no webfont",
        location="index.html",
        found_by="requirements_gap_check",
    )

    applied = await repository.accept_issues(
        session,
        product_id=product,
        accepted=[("deadbeef", "Not a real issue."), ("", "Empty.")],
        decided_by_task_id="release-1",
    )
    assert applied == 0
    assert len(await repository.open_issues_for(session, product)) == 1
