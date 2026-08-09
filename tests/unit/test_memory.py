"""Cross-session memory.

Answers "does the system have memory?" concretely: a lesson written in one call is
retrievable by a later, unrelated call, scoped correctly, and stops being retrieved
once it is superseded. Runs against real SQL (SQLite hosting only the ``memories``
table — see ``memory_session`` in conftest) rather than a mock of the store.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from factory.memory import store as memory
from factory.settings import EngineName
from factory.state.orm import Memory


async def test_remembered_content_is_recallable(memory_session: AsyncSession) -> None:
    await memory.remember(
        memory_session,
        content="Ideas targeting solo freelancers consistently fail the LIA stage.",
        origin="test",
    )
    await memory_session.commit()

    hits = await memory.recall(memory_session, query="freelancer ideas LIA compliance")
    assert len(hits) == 1
    assert "freelancers" in hits[0].content


async def test_recall_is_empty_for_an_unrelated_query(memory_session: AsyncSession) -> None:
    await memory.remember(
        memory_session,
        content="The Stripe webhook occasionally double-fires for invoice.paid events.",
        origin="test",
    )
    await memory_session.commit()

    hits = await memory.recall(memory_session, query="what color should the logo be")
    assert hits == []


async def test_near_duplicate_reinforces_instead_of_duplicating(
    memory_session: AsyncSession,
) -> None:
    first = await memory.remember(
        memory_session,
        content="Discovery keeps proposing invoice-reconciliation tools that already exist.",
        origin="test",
    )
    second = await memory.remember(
        memory_session,
        content="Discovery keeps proposing invoice reconciliation tools that already exist.",
        origin="test",
    )
    await memory_session.commit()

    assert first == second
    rows = (await memory_session.execute(select(Memory))).scalars().all()
    assert len(rows) == 1
    assert rows[0].hits == 1


async def test_confidence_grows_with_reinforcement(memory_session: AsyncSession) -> None:
    text = "Bug Council secret scanner false-positives on base64 test fixtures."
    memory_id = await memory.remember(memory_session, content=text, origin="test")
    await memory.remember(memory_session, content=text, origin="test")
    await memory_session.commit()

    row = await memory_session.get(Memory, memory_id)
    assert row is not None
    assert row.confidence > 0.6


async def test_content_below_the_minimum_length_is_not_stored(
    memory_session: AsyncSession,
) -> None:
    result = await memory.remember(memory_session, content="too short", origin="test")
    assert result is None
    assert await memory.count(memory_session) == 0


async def test_content_is_truncated_to_the_maximum(memory_session: AsyncSession) -> None:
    huge = "x" * (memory.MAX_MEMORY_CHARS * 2)
    memory_id = await memory.remember(memory_session, content=huge, origin="test")
    await memory_session.commit()
    row = await memory_session.get(Memory, memory_id)
    assert row is not None
    assert len(row.content) == memory.MAX_MEMORY_CHARS


# --- scoping: the safety property --------------------------------------------


async def test_product_scoped_memory_does_not_leak_to_another_product(
    memory_session: AsyncSession,
) -> None:
    await memory.remember(
        memory_session,
        content="This product's checkout flow breaks on Safari due to a cookie bug.",
        scope=memory.scope_for(product_id="prod-a"),
        origin="test",
    )
    await memory_session.commit()

    same_product = await memory.recall(
        memory_session,
        query="checkout flow Safari bug",
        scopes=memory.scopes_for(product_id="prod-a"),
    )
    other_product = await memory.recall(
        memory_session,
        query="checkout flow Safari bug",
        scopes=memory.scopes_for(product_id="prod-b"),
    )
    assert len(same_product) == 1
    assert other_product == []


async def test_global_memory_is_visible_from_every_scope(memory_session: AsyncSession) -> None:
    await memory.remember(
        memory_session,
        content="Never propose ideas that require processing special category data.",
        scope=memory.GLOBAL_SCOPE,
        origin="test",
    )
    await memory_session.commit()

    for scopes in (
        memory.scopes_for(),
        memory.scopes_for(engine=EngineName.DISCOVERY),
        memory.scopes_for(product_id="prod-x"),
    ):
        hits = await memory.recall(
            memory_session, query="special category data processing", scopes=scopes
        )
        assert len(hits) == 1


def test_scope_for_prefers_product_over_engine() -> None:
    assert memory.scope_for(engine=EngineName.MARKETING, product_id="p1") == "product:p1"
    assert memory.scope_for(engine=EngineName.MARKETING) == "engine:marketing"
    assert memory.scope_for() == memory.GLOBAL_SCOPE


def test_scopes_for_always_includes_global() -> None:
    assert memory.GLOBAL_SCOPE in memory.scopes_for(product_id="p1")
    assert memory.GLOBAL_SCOPE in memory.scopes_for()


# --- forgetting ---------------------------------------------------------------


async def test_forgotten_memory_is_no_longer_recalled(memory_session: AsyncSession) -> None:
    memory_id = await memory.remember(
        memory_session,
        content="Assumed the sandbox image had curl installed; it does not.",
        origin="test",
    )
    await memory_session.commit()
    assert memory_id is not None

    await memory.forget(memory_session, memory_id, reason="curl was added to the base image")
    await memory_session.commit()

    hits = await memory.recall(memory_session, query="sandbox image curl installed")
    assert hits == []

    row = await memory_session.get(Memory, memory_id)
    assert row is not None
    assert row.superseded_at is not None
    assert row.superseded_reason == "curl was added to the base image"


# --- prompt-injection rendering ------------------------------------------------


async def test_recall_block_is_empty_when_nothing_is_relevant(
    memory_session: AsyncSession,
) -> None:
    assert await memory.recall_block(memory_session, query="anything at all") == ""


async def test_recall_block_labels_memory_as_prior_belief_not_fact(
    memory_session: AsyncSession,
) -> None:
    await memory.remember(
        memory_session,
        content="Apollo results for junior titles have a high bounce rate.",
        origin="test",
    )
    await memory_session.commit()

    block = await memory.recall_block(memory_session, query="Apollo junior titles bounce rate")
    assert "prior belief" in block
    assert "contradict it" in block
    assert "Apollo results for junior titles" in block


async def test_recall_respects_the_limit(memory_session: AsyncSession) -> None:
    for i in range(memory.RECALL_LIMIT + 4):
        await memory.remember(
            memory_session,
            content=f"distinct lesson number {i} about pricing experiments and churn",
            origin="test",
        )
    await memory_session.commit()

    hits = await memory.recall(memory_session, query="pricing experiments and churn lessons")
    assert len(hits) <= memory.RECALL_LIMIT


# --- pruning --------------------------------------------------------------------


def _memory(
    *,
    content: str,
    scope: str = memory.GLOBAL_SCOPE,
    kind: memory.MemoryKind = memory.MemoryKind.LESSON,
    confidence: float = 0.6,
    hits: int = 0,
    age_days: float = 0.0,
) -> Memory:
    """A directly-constructed row, bypassing `remember()`, so a test can pin exactly
    the confidence/hits/age combination its assertion depends on."""
    when = datetime.now(UTC) - timedelta(days=age_days)
    return Memory(
        scope=scope,
        kind=kind.value,
        content=content,
        origin="test",
        confidence=confidence,
        hits=hits,
        created_at=when,
        last_used_at=when,
    )


async def test_a_scope_under_budget_is_left_untouched(memory_session: AsyncSession) -> None:
    for i in range(5):
        memory_session.add(_memory(content=f"lesson {i} about the checkout flow"))
    await memory_session.commit()

    report = await memory.prune(memory_session, max_per_scope=10)
    await memory_session.commit()

    assert report.pruned == 0
    assert await memory.count(memory_session) == 5


async def test_a_scope_over_budget_evicts_down_to_the_cap(memory_session: AsyncSession) -> None:
    for i in range(10):
        memory_session.add(_memory(content=f"lesson {i} about the checkout flow"))
    await memory_session.commit()

    report = await memory.prune(memory_session, max_per_scope=6)
    await memory_session.commit()

    assert report.pruned == 4
    assert await memory.count(memory_session) == 6


async def test_the_lowest_value_entries_are_the_ones_evicted(memory_session: AsyncSession) -> None:
    strong = _memory(
        content="high confidence, frequently recalled, recent lesson about deploys",
        confidence=0.95,
        hits=9,
        age_days=1,
    )
    weak = _memory(
        content="low confidence, never recalled, stale lesson about deploys",
        confidence=0.2,
        hits=0,
        age_days=200,
    )
    memory_session.add(strong)
    memory_session.add(weak)
    await memory_session.commit()

    report = await memory.prune(memory_session, max_per_scope=1)
    await memory_session.commit()

    assert report.pruned == 1
    survivor = (
        await memory_session.execute(select(Memory).where(Memory.superseded_at.is_(None)))
    ).scalar_one()
    assert survivor.id == strong.id

    evicted = await memory_session.get(Memory, weak.id)
    assert evicted is not None
    assert evicted.superseded_at is not None
    assert "pruned" in (evicted.superseded_reason or "")


async def test_preference_memories_are_never_evicted(memory_session: AsyncSession) -> None:
    preference = _memory(
        content="Operator preference: never contact leads at government domains",
        kind=memory.MemoryKind.PREFERENCE,
        confidence=0.1,  # deliberately the worst possible score
        hits=0,
        age_days=365,
    )
    lesson = _memory(
        content="High-value, frequently recalled, recent lesson worth keeping",
        confidence=0.99,
        hits=10,
        age_days=0,
    )
    memory_session.add(preference)
    memory_session.add(lesson)
    await memory_session.commit()

    # The preference scores worst of the two, yet must survive: with a budget of 1
    # the *lesson* is evicted instead to bring the scope under budget, even though
    # it scores far better on its own.
    report = await memory.prune(memory_session, max_per_scope=1)
    await memory_session.commit()

    assert report.pruned == 1
    still_there = await memory_session.get(Memory, preference.id)
    assert still_there is not None
    assert still_there.superseded_at is None

    evicted_lesson = await memory_session.get(Memory, lesson.id)
    assert evicted_lesson is not None
    assert evicted_lesson.superseded_at is not None


async def test_pruning_is_independent_per_scope(memory_session: AsyncSession) -> None:
    for i in range(5):
        memory_session.add(_memory(content=f"global lesson {i}", scope=memory.GLOBAL_SCOPE))
    for i in range(3):
        memory_session.add(
            _memory(content=f"product lesson {i}", scope=memory.scope_for(product_id="prod-1"))
        )
    await memory_session.commit()

    report = await memory.prune(memory_session, max_per_scope=4)
    await memory_session.commit()

    assert report.scopes_checked == 2
    assert report.pruned == 1  # only the 5-row global scope exceeded the cap of 4


async def test_pruning_never_hard_deletes(memory_session: AsyncSession) -> None:
    for i in range(5):
        memory_session.add(_memory(content=f"lesson {i} about the checkout flow"))
    await memory_session.commit()

    await memory.prune(memory_session, max_per_scope=2)
    await memory_session.commit()

    total_rows = (await memory_session.execute(select(Memory))).scalars().all()
    assert len(total_rows) == 5  # nothing physically removed, only superseded
