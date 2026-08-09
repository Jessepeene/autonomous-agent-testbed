"""Cross-session memory.

Postgres already gave the factory *state* — what tasks exist, what products are live,
what the balance is. That is not the same as memory. State says "product X is live";
memory says "the last three products in this niche all failed compliance for the same
reason, stop proposing them."

Without this the factory relearns every lesson every run: it re-proposes ideas that
were rejected, re-attempts deploys that failed the same way, re-derives a diagnosis it
already reached last night. The Master System Prompt asks it to improve its own
processes continuously, and nothing can improve if nothing is retained.

**Retrieval is lexical, not embedded.** Same reasoning as the discovery clustering:
recall runs on every agent spawn, an embedding call per query would cost real money to
answer a question that token overlap answers well enough, and lexical scoring is
deterministic so tests can assert on what gets recalled.

**Memories are scoped, and scope is the safety property.** A lesson learned about one
product must not silently steer another. Recall only ever reads the scopes it is
handed.

Never write personal data here. Memories are replayed verbatim into future prompts,
which would turn one leaked address into a permanent one.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from factory.engines.discovery.cluster import jaccard, tokenize
from factory.logging import get_logger
from factory.settings import EngineName
from factory.state.orm import Memory

log = get_logger(__name__)

MAX_MEMORY_CHARS = 2_000
GLOBAL_SCOPE = "global"

#: How many memories a recall block may inject. Enough to carry real lessons, small
#: enough that memory never crowds out the actual task in the prompt.
RECALL_LIMIT = 6

#: Below this overlap a memory is not about the current situation, and injecting it
#: would be noise that costs tokens and misleads.
MIN_RELEVANCE = 0.06


class MemoryKind(StrEnum):
    LESSON = "lesson"  # something learned that should change future behaviour
    FACT = "fact"  # a durable truth about the environment
    FAILURE = "failure"  # a specific thing that did not work, and why
    PREFERENCE = "preference"  # an operator decision to honour going forward


@dataclass(slots=True)
class Recalled:
    id: str
    content: str
    kind: str
    scope: str
    relevance: float


def scope_for(*, engine: EngineName | None = None, product_id: str | None = None) -> str:
    """The most specific scope a writer should file a memory under."""
    if product_id:
        return f"product:{product_id}"
    if engine:
        return f"engine:{engine.value}"
    return GLOBAL_SCOPE


def scopes_for(*, engine: EngineName | None = None, product_id: str | None = None) -> list[str]:
    """Scopes a reader may draw on, narrowest first.

    Global lessons always apply; a product's lessons apply only to that product.
    """
    scopes = [GLOBAL_SCOPE]
    if engine:
        scopes.append(f"engine:{engine.value}")
    if product_id:
        scopes.append(f"product:{product_id}")
    return scopes


async def remember(
    session: AsyncSession,
    *,
    content: str,
    kind: MemoryKind = MemoryKind.LESSON,
    scope: str = GLOBAL_SCOPE,
    source_task_id: str | None = None,
    origin: str = "unknown",
    confidence: float = 0.6,
) -> str | None:
    """Record a lesson. Near-duplicates reinforce the existing memory instead.

    Reinforcing rather than appending is what stops the store filling with fifty
    phrasings of the same lesson, which would then crowd out everything else at
    recall time.
    """
    text = content.strip()[:MAX_MEMORY_CHARS]
    if len(text) < 20:
        return None

    tokens = tokenize(text)
    existing = await _in_scopes(session, [scope])
    for row in existing:
        if jaccard(tokens, tokenize(row.content)) >= 0.6:
            await session.execute(
                update(Memory)
                .where(Memory.id == row.id)
                .values(
                    hits=row.hits + 1,
                    confidence=min(1.0, row.confidence + 0.1),
                    last_used_at=datetime.now(UTC),
                )
            )
            log.debug("memory.reinforced", memory_id=row.id, scope=scope)
            return str(row.id)

    memory = Memory(
        scope=scope,
        kind=kind.value,
        content=text,
        origin=origin[:64],
        source_task_id=source_task_id,
        confidence=confidence,
    )
    session.add(memory)
    await session.flush()
    log.info("memory.stored", memory_id=memory.id, scope=scope, kind=kind.value)
    return memory.id


async def recall(
    session: AsyncSession,
    *,
    query: str,
    scopes: list[str] | None = None,
    limit: int = RECALL_LIMIT,
) -> list[Recalled]:
    """Most relevant memories for a situation, scored on token overlap."""
    rows = await _in_scopes(session, scopes or [GLOBAL_SCOPE])
    if not rows:
        return []

    query_tokens = tokenize(query)
    scored: list[Recalled] = []
    for row in rows:
        relevance = jaccard(query_tokens, tokenize(row.content))
        # Confidence breaks ties between similarly-relevant memories, so a lesson
        # confirmed five times outranks one recorded once.
        weighted = relevance * (0.5 + row.confidence / 2)
        if weighted >= MIN_RELEVANCE:
            scored.append(
                Recalled(
                    id=row.id,
                    content=row.content,
                    kind=row.kind,
                    scope=row.scope,
                    relevance=round(weighted, 4),
                )
            )

    scored.sort(key=lambda r: r.relevance, reverse=True)
    top = scored[:limit]
    if top:
        await session.execute(
            update(Memory)
            .where(Memory.id.in_([r.id for r in top]))
            .values(hits=Memory.hits + 1, last_used_at=datetime.now(UTC))
        )
    return top


async def recall_block(
    session: AsyncSession,
    *,
    query: str,
    scopes: list[str] | None = None,
    limit: int = RECALL_LIMIT,
) -> str:
    """Recall rendered for prompt injection. Empty string when nothing is relevant.

    Labelled as prior conclusions rather than facts: a memory is something a previous
    run believed, and the current agent should be willing to contradict it.
    """
    memories = await recall(session, query=query, scopes=scopes, limit=limit)
    if not memories:
        return ""
    lines = "\n".join(f"- [{m.kind}] {m.content}" for m in memories)
    return (
        "What earlier runs concluded (treat as prior belief, not fact — contradict it "
        f"if what you observe now disagrees):\n{lines}"
    )


async def forget(session: AsyncSession, memory_id: str, reason: str) -> None:
    """Retire a memory that turned out to be wrong.

    Superseded rather than deleted, so an audit can still answer "why did the factory
    believe that for two weeks".
    """
    await session.execute(
        update(Memory)
        .where(Memory.id == memory_id)
        .values(superseded_at=datetime.now(UTC), superseded_reason=reason[:500])
    )
    log.info("memory.forgotten", memory_id=memory_id, reason=reason[:200])


async def count(session: AsyncSession) -> int:
    stmt = select(func.count()).select_from(Memory).where(Memory.superseded_at.is_(None))
    return int((await session.execute(stmt)).scalar_one())


# --- pruning --------------------------------------------------------------------
#
# `recall()` and `remember()`'s near-duplicate check both scan every live memory in
# the scopes they're given — cheap today, but O(n) in a number that only ever grows,
# on every single agent spawn. A scope's live-row count, not the table's total, is
# the thing that has to stay bounded for that cost to stay bounded. Pruning is
# deliberately a value *score*, not an age cutoff: a memory that is rarely recalled
# but was written with high confidence (a rare-but-important lesson) should not be
# evicted just for being infrequent — see MEMORY.md-adjacent discussion in this
# codebase's own design notes on why pure recency would be the wrong metric here.

#: Live memories per scope, beyond which the lowest-value entries are pruned.
MAX_MEMORIES_PER_SCOPE = 200

#: Value-score weights. None of the three terms alone should decide an eviction —
#: confidence and hit-count are each a partial signal, not a gate.
_CONFIDENCE_WEIGHT = 0.40
_HIT_WEIGHT = 0.35
_RECENCY_WEIGHT = 0.25
#: Hits beyond this stop adding value — a handful of confirmations is already good
#: evidence; a memory recalled 500 times should not out-rank one recalled 20 times
#: purely on volume.
_HIT_SATURATION = 10
#: Recency decays smoothly rather than as a hard cutoff — halves every 30 days.
_RECENCY_HALF_LIFE_DAYS = 30.0


@dataclass(slots=True)
class PruneReport:
    scopes_checked: int = 0
    pruned: int = 0


def _net_value_score(memory: Memory, *, now: datetime) -> float:
    """Net value per byte — deliberately a simplified cousin of the "value minus
    harm, divided by length" formulation this pruning design is based on: value here
    is confidence + recall frequency + recency, with no harm term, because nothing
    in this codebase yet attributes a downstream failure back to a specific memory
    (doing that properly would need the same kind of outcome-tracking the
    `factory.learning` statistical loop has for prompts, and memory is not wired
    into that). Dividing by content length still gets the main practical benefit: a
    concise, frequently-useful memory outranks a verbose, rarely-used one of similar
    confidence, so the budget is spent on signal rather than length.
    """
    reference = memory.last_used_at or memory.created_at
    if reference.tzinfo is None:
        # SQLite (used only for testing this store) does not round-trip tzinfo the
        # way Postgres does; a naive value read back is still UTC, never local time.
        reference = reference.replace(tzinfo=UTC)
    age_days = max((now - reference).total_seconds() / 86400, 0.0)
    recency = 0.5 ** (age_days / _RECENCY_HALF_LIFE_DAYS)
    hit_score = min(memory.hits, _HIT_SATURATION) / _HIT_SATURATION
    value = (
        _CONFIDENCE_WEIGHT * memory.confidence + _HIT_WEIGHT * hit_score + _RECENCY_WEIGHT * recency
    )
    return float(value) / max(len(memory.content), 1)


async def prune(
    session: AsyncSession, *, max_per_scope: int = MAX_MEMORIES_PER_SCOPE
) -> PruneReport:
    """Evict the lowest-value memories once a scope exceeds its budget.

    Operator ``PREFERENCE`` memories are exempt — they are an explicit human
    decision to honour, not a learned experience, and a value score has no
    business overriding that. Eviction supersedes exactly like `forget()`, never a
    hard delete, so the audit trail this store already keeps survives pruning too.
    """
    report = PruneReport()
    now = datetime.now(UTC)

    scope_stmt = select(Memory.scope).where(Memory.superseded_at.is_(None)).distinct()
    scopes = list((await session.execute(scope_stmt)).scalars().all())

    for scope in scopes:
        report.scopes_checked += 1
        rows = await _in_scopes(session, [scope])
        overflow = len(rows) - max_per_scope
        if overflow <= 0:
            continue

        prunable = [row for row in rows if row.kind != MemoryKind.PREFERENCE.value]
        prunable.sort(key=lambda row: _net_value_score(row, now=now))
        for row in prunable[: min(overflow, len(prunable))]:
            await forget(session, row.id, "pruned: low value/byte score under the scope budget")
            report.pruned += 1

    if report.pruned:
        log.info("memory.pruned", scopes_checked=report.scopes_checked, evicted=report.pruned)
    return report


async def _in_scopes(session: AsyncSession, scopes: list[str]) -> list[Memory]:
    stmt = select(Memory).where(
        Memory.scope.in_(scopes),
        Memory.superseded_at.is_(None),
    )
    return list((await session.execute(stmt)).scalars().all())
