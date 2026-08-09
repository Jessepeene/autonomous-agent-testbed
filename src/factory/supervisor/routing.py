"""Deterministic-first routing.

The Supervisor ticks every ``supervisor_tick_seconds`` (15s by default), and every
tick previously asked a model to pick the next engine — even though ``_fallback``'s
own priority scan in ``orchestrator.py`` already computes the same answer
deterministically from the same snapshot. At steady state this was the single
largest source of LLM calls in the whole system: up to 5,760 calls/day from the tick
loop alone.

The model is consulted only when it can add something the priority scan cannot:

- ``first_tick``      — no judgement has ever been formed for this process.
- ``ambiguous_route``  — the priority scan has no answer (queued work sits on an
  engine it does not rank).
- ``stalled``          — the exact same state has persisted for several ticks
  running, which is precisely the "stubborn failure that needs investigating" case.
- ``cadence``          — enough ticks have passed since the last consultation that
  drift the fingerprint doesn't capture might have accumulated.

Everything here is a pure function over a ``FactorySnapshot`` plus one small mutable
``RoutingMemo`` — no I/O, no model call, fully unit-testable without a database.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from factory.settings import EngineName, Settings
from factory.state.schemas import NextAction, OrchestratorDecision

if TYPE_CHECKING:
    from factory.supervisor.context import FactoryContext

#: Deterministic priority order. Revenue-bearing work outranks discovering more of it.
PRIORITY: tuple[EngineName, ...] = (
    EngineName.FINANCE,
    EngineName.DEVELOPMENT,
    EngineName.COMPLIANCE,
    EngineName.MARKETING,
    EngineName.DISCOVERY,
)

#: Actions safe to replay unchanged when state hasn't moved. SPAWN_AGENT and ESCALATE
#: are side-effecting — replaying either would spawn or escalate again on every
#: identical tick — so they are never cached (see `record_decision`).
REPLAYABLE: frozenset[NextAction] = frozenset(
    {NextAction.ROUTE, NextAction.RETRY, NextAction.IDLE_BACKLOG, NextAction.HOLD_DISCOVERY}
)

#: Why the model is being consulted, rendered into its prompt — telling it "state has
#: been identical for 4 ticks" is materially better context than a bare snapshot.
REASON_TEXT: dict[str, str] = {
    "first_tick": (
        "this is the first tick since the factory booted; there is no prior judgement to reuse."
    ),
    "ambiguous_route": (
        "queued work exists on an engine the deterministic priority order does not rank."
    ),
    "stalled": (
        "factory state has been identical for several consecutive ticks; the deterministic "
        "route may not be resolving the underlying problem."
    ),
    "cadence": "enough ticks have passed since the last review that a periodic check is due.",
}


@dataclass(slots=True)
class FactorySnapshot:
    """Everything the router is allowed to reason about, as plain data."""

    eligible_engines: list[str]
    disabled_engines: dict[str, str]
    pending_tasks_total: int
    pending_by_engine: dict[str, int]
    active_dev_products: int
    max_concurrent_builds: int
    approved_ideas_waiting: int
    pending_ideas: int
    open_interrupts: int
    spend_limit_cents: int
    dry_run: bool

    @property
    def dev_at_capacity(self) -> bool:
        return self.active_dev_products >= self.max_concurrent_builds


@dataclass(slots=True)
class RoutingMemo:
    """Per-process, per-worker routing state. Not durable and never checkpointed — a
    restart simply re-derives judgement on the next ``first_tick``, which is cheap
    (one extra model call) and safer than trying to persist and re-validate this
    across a process boundary."""

    fingerprint: str = ""
    repeat_count: int = 0
    ticks_since_model: int = 0
    has_deliberated: bool = False
    #: Only ever holds a REPLAYABLE decision — see `record_decision`.
    decision: OrchestratorDecision | None = None
    decision_fingerprint: str = ""


def _bucket(n: int) -> int:
    """Collapse a raw count into one of 5 buckets so routine churn (7 -> 8 pending
    tasks) doesn't move the fingerprint, while a real change of order of magnitude
    does: 0 / 1 / 2-5 / 6-20 / 21+."""
    if n <= 0:
        return 0
    if n <= 1:
        return 1
    if n <= 5:
        return 2
    if n <= 20:
        return 3
    return 4


def _log_bucket_cents(cents: int) -> int:
    """Order of magnitude only — a 1000 -> 1001 cent change must not re-ask the model."""
    if cents <= 0:
        return 0
    if cents < 100:
        return 1
    if cents < 1_000:
        return 2
    if cents < 10_000:
        return 3
    return 4


def fingerprint(snapshot: FactorySnapshot) -> str:
    """A stable string over bucketed, decision-relevant fields only.

    Deliberately excludes process-lifetime constants — ``max_concurrent_builds``,
    ``dry_run``, ``disabled_engines``, ``eligible_engines`` are all fixed at boot —
    and ``pending_tasks_total``, which is a pure sum of ``pending_by_engine`` and so
    adds no information while being the churniest field in the snapshot.
    """
    pending = ",".join(
        f"{engine}:{_bucket(count)}" for engine, count in sorted(snapshot.pending_by_engine.items())
    )
    parts = [
        f"pending={pending}",
        f"dev_capacity={int(snapshot.dev_at_capacity)}",
        f"dev_active={min(snapshot.active_dev_products, snapshot.max_concurrent_builds)}",
        f"approved={_bucket(snapshot.approved_ideas_waiting)}",
        f"pending_ideas={_bucket(snapshot.pending_ideas)}",
        f"interrupts={_bucket(snapshot.open_interrupts)}",
        f"spend={_log_bucket_cents(snapshot.spend_limit_cents)}",
    ]
    return "|".join(parts)


def observe_tick(memo: RoutingMemo, fp: str) -> None:
    """Update repeat/age bookkeeping. Called exactly once per tick, before checking
    whether deliberation is needed this tick."""
    if fp == memo.fingerprint:
        memo.repeat_count += 1
    else:
        memo.repeat_count = 0
    memo.fingerprint = fp
    memo.ticks_since_model += 1


def record_decision(memo: RoutingMemo, decision: OrchestratorDecision, fp: str) -> None:
    """Call once, after a model consultation actually succeeds — never on a fallback
    produced by a provider error. Resets the cadence and stall clocks, and caches the
    decision only if replaying it later would be safe."""
    memo.ticks_since_model = 0
    memo.repeat_count = 0
    memo.has_deliberated = True
    memo.decision = decision if decision.next_action in REPLAYABLE else None
    memo.decision_fingerprint = fp


def deterministic_route(snapshot: FactorySnapshot) -> OrchestratorDecision | None:
    """The same fixed priority scan the Supervisor has always fallen back to.
    Returns ``None`` only when queued work exists on an engine this table does not
    rank — today that is unreachable (the table covers every ``EngineName`` but
    SUPERVISOR), but it is the correct signal to escalate to the model rather than
    silently dropping the work if the engine set ever grows."""
    for engine in PRIORITY:
        if snapshot.pending_by_engine.get(engine.value, 0) > 0:
            return OrchestratorDecision(
                next_action=NextAction.ROUTE,
                target_engine=engine,
                tool_call=None,
                rationale=f"Deterministic priority order: {engine.value} has queued work.",
            )
    if snapshot.pending_tasks_total == 0:
        return OrchestratorDecision(
            next_action=NextAction.IDLE_BACKLOG,
            target_engine=EngineName.SUPERVISOR,
            tool_call=None,
            rationale="Deterministic fallback: nothing queued, running maintenance.",
        )
    return None


def deliberation_reason(
    snapshot: FactorySnapshot, memo: RoutingMemo, settings: Settings
) -> str | None:
    """``None`` means the deterministic path is sufficient this tick; anything else
    is the reason the model is being consulted, in priority order — first match
    wins."""
    if not memo.has_deliberated:
        return "first_tick"
    if deterministic_route(snapshot) is None:
        return "ambiguous_route"
    if memo.repeat_count >= settings.supervisor_stall_ticks:
        return "stalled"
    if memo.ticks_since_model >= settings.supervisor_deliberation_ticks:
        return "cadence"
    return None


def replay(memo: RoutingMemo, fp: str, ctx: FactoryContext) -> OrchestratorDecision | None:
    """Reuse the model's last decision if the fingerprint hasn't moved since.

    Only ever returns a REPLAYABLE action: SPAWN_AGENT and ESCALATE are never cached
    in the first place (`record_decision`'s contract), so this cannot re-fire a side
    effect no matter how many identical ticks occur in a row.
    """
    decision = memo.decision
    if decision is None or memo.decision_fingerprint != fp:
        return None
    if decision.next_action not in REPLAYABLE:
        return None  # unreachable given record_decision's contract; belt and braces
    eligible = {e.value for e in ctx.enabled_engines} | {EngineName.SUPERVISOR.value}
    if decision.target_engine.value not in eligible:
        return None
    return decision
