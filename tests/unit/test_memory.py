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


async def test_facts_differing_only_in_a_number_are_not_merged(
    memory_session: AsyncSession,
) -> None:
    """Regression test: the tokenizer only matched alphabetic tokens, so two facts
    differing solely in a number hashed to identical token sets ({"timeout",
    "seconds"} either way) and were treated as near-duplicates -- the newer,
    corrected number never got stored at all, and the stale one could end up with
    *higher* confidence than a real correction would have. With the number
    included as its own token, the two sets share 2 of 4 tokens (jaccard 0.5),
    correctly below the 0.6 reinforcement threshold."""
    first = await memory.remember(
        memory_session, content="Timeout is 40 seconds.", origin="test"
    )
    second = await memory.remember(
        memory_session, content="Timeout is 60 seconds.", origin="test"
    )
    await memory_session.commit()

    assert first is not None
    assert second is not None
    assert first != second
    rows = (await memory_session.execute(select(Memory))).scalars().all()
    assert len(rows) == 2
    assert all(row.hits == 0 for row in rows)


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
    """PREFERENCE rows are exempt from *both* eviction and the count that decides
    how much eviction is needed -- max_per_scope bounds only the learned
    (non-preference) population. A scope with one preference and one lesson, under
    a budget of 1, needs no eviction at all: exactly one prunable row exists and
    the budget allows one.
    """
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

    report = await memory.prune(memory_session, max_per_scope=1)
    await memory_session.commit()

    assert report.pruned == 0
    still_there = await memory_session.get(Memory, preference.id)
    assert still_there is not None
    assert still_there.superseded_at is None

    lesson_row = await memory_session.get(Memory, lesson.id)
    assert lesson_row is not None
    assert lesson_row.superseded_at is None


async def test_preferences_never_count_toward_the_prunable_budget(
    memory_session: AsyncSession,
) -> None:
    """Regression test: overflow used to be computed against the *total* row count
    (preferences included), so a scope with many preferences and few lessons could
    compute an overflow larger than the number of prunable rows -- every lesson got
    evicted trying to reach a budget that only the exempt preferences were pushing
    it past, and the scope was left permanently over budget with nothing left to
    prune. Preferences must not count toward the budget at all."""
    for i in range(5):
        memory_session.add(
            _memory(
                content=f"Operator preference number {i} about contact rules",
                kind=memory.MemoryKind.PREFERENCE,
            )
        )
    for i in range(3):
        memory_session.add(_memory(content=f"lesson {i} about the checkout flow"))
    await memory_session.commit()

    # 5 preferences + 3 lessons = 8 total rows, well over a budget of 4 if
    # preferences counted -- but only 3 rows are actually prunable, under budget.
    report = await memory.prune(memory_session, max_per_scope=4)
    await memory_session.commit()

    assert report.pruned == 0
    assert await memory.count(memory_session) == 8


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


async def test_a_verbose_high_value_memory_outranks_a_short_low_value_one(
    memory_session: AsyncSession,
) -> None:
    """Regression test: dividing by raw content length let length dominate the
    score completely (an 80x spread from a 20-2000 char range vs. value's own ~4x
    spread), so a barely-informative short note could outscore a highly confident,
    frequently-recalled, detailed lesson purely by being shorter -- the single most
    valuable memory in a scope was the first one evicted."""
    weak_but_short = _memory(
        content="x" * 25,  # minimum-length filler, no real signal
        confidence=0.2,
        hits=0,
        age_days=365,
    )
    strong_but_long = _memory(
        content=(
            "A detailed, hard-won lesson about exactly why the compliance assessor "
            "rejected the last three fintech ideas, written out in full so the next "
            "run does not have to rediscover it from scratch. " + ("detail " * 250)
        )[: memory.MAX_MEMORY_CHARS],
        confidence=1.0,
        hits=50,
        age_days=0,
    )
    memory_session.add(weak_but_short)
    memory_session.add(strong_but_long)
    await memory_session.commit()

    report = await memory.prune(memory_session, max_per_scope=1)
    await memory_session.commit()

    assert report.pruned == 1
    survivor = (
        await memory_session.execute(select(Memory).where(Memory.superseded_at.is_(None)))
    ).scalar_one()
    assert survivor.id == strong_but_long.id


async def test_pruning_never_hard_deletes(memory_session: AsyncSession) -> None:
    for i in range(5):
        memory_session.add(_memory(content=f"lesson {i} about the checkout flow"))
    await memory_session.commit()

    await memory.prune(memory_session, max_per_scope=2)
    await memory_session.commit()

    total_rows = (await memory_session.execute(select(Memory))).scalars().all()
    assert len(total_rows) == 5  # nothing physically removed, only superseded


# --------------------------------------------------------- recall never blocks


class _BrokenSession:
    """A session whose every statement fails, the way a dropped connection or an
    un-run migration behaves."""

    def __init__(self) -> None:
        self.attempts = 0

    async def execute(self, *_a, **_k):
        self.attempts += 1
        raise RuntimeError("connection reset while reading memories")


async def test_recall_returns_nothing_rather_than_raising() -> None:
    """The rule, pinned as behaviour rather than as a comment somebody has to have
    read. Recall is an enhancement at every call site and a dependency at none: an
    agent missing its standards is degraded, one that never ran is a hole in the
    pipeline.

    This exists because the comment version failed. It was worked out in
    `preship.py`, written down there, and five of this codebase's seven recall sites
    stayed unguarded regardless -- including one put on the landing-page build path
    on 2026-09-21, where a failed recall would have failed the build, inside a
    function whose own docstring promises it never blocks one.
    """
    session = _BrokenSession()

    assert await memory.recall(session, query="anything at all", scopes=["global"]) == []
    assert session.attempts == 1, "it must actually have tried"


async def test_recall_block_returns_empty_rather_than_raising() -> None:
    """The rendered form takes the same guarantee -- it is what callers actually use."""
    assert await memory.recall_block(_BrokenSession(), query="anything at all") == ""


async def test_a_failed_hit_count_does_not_cost_the_caller_its_memories(
    memory_session: AsyncSession, monkeypatch
) -> None:
    """Recording that a memory was useful is bookkeeping for `prune`'s value score.
    Losing it must not lose the recall it was bookkeeping for."""
    await memory.remember(
        memory_session,
        content="The repair sandbox has no dependencies installed before the first run.",
        scope="global",
    )
    await memory_session.flush()

    real_execute = memory_session.execute
    calls = {"n": 0}

    async def flaky(stmt, *a, **k):
        calls["n"] += 1
        # The SELECT succeeds; the hit-count UPDATE that follows it does not.
        if calls["n"] > 1:
            raise RuntimeError("write failed")
        return await real_execute(stmt, *a, **k)

    monkeypatch.setattr(memory_session, "execute", flaky)
    found = await memory.recall(memory_session, query="repair sandbox dependencies")

    assert len(found) == 1
    assert "dependencies installed" in found[0].content
