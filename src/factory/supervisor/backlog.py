"""The never-idle catalogue.

When no product work is ready the factory does not sleep — it picks up maintenance
that compounds: failing tests, missing coverage, cost regressions, stale docs,
technical debt. These are real enqueued tasks with real outputs, not a busy-wait.

The catalogue is ordered by leverage, and each entry is rate-limited by its own
idempotency key so the queue cannot fill with fifty copies of "improve prompts".
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from factory.logging import get_logger
from factory.settings import EngineName
from factory.state import repository
from factory.state.schemas import TaskKind

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class MaintenanceItem:
    slug: str
    objective: str
    engine: EngineName
    cadence_hours: int


CATALOGUE: tuple[MaintenanceItem, ...] = (
    MaintenanceItem(
        slug="triage-escalated-tasks",
        objective=(
            "Review tasks in the escalated state, diagnose the root cause of the three "
            "failed attempts, and either repair the underlying issue or write a precise "
            "human-readable summary of what is blocked and why."
        ),
        engine=EngineName.SUPERVISOR,
        cadence_hours=6,
    ),
    MaintenanceItem(
        slug="reduce-llm-cost",
        objective=(
            "Analyse recent llm_calls rows for the largest cost drivers. Identify prompts "
            "that could use a cheaper model, a lower effort level, or better prompt caching, "
            "and record concrete recommendations."
        ),
        engine=EngineName.SUPERVISOR,
        cadence_hours=24,
    ),
    MaintenanceItem(
        slug="improve-test-coverage",
        objective=(
            "Find guardrails in the codebase that lack a direct regression test and write "
            "one. Prioritise the compliance, sandbox, budget and blacklist paths."
        ),
        engine=EngineName.DEVELOPMENT,
        cadence_hours=24,
    ),
    MaintenanceItem(
        slug="refine-agent-prompts",
        objective=(
            "Review recent schema_violation and refusal outcomes in llm_calls and tighten "
            "the prompts that produced them."
        ),
        engine=EngineName.SUPERVISOR,
        cadence_hours=24,
    ),
    MaintenanceItem(
        slug="audit-live-products",
        objective=(
            "Check every live product's health endpoint and recent error rate; open a patch "
            "task for anything degraded."
        ),
        engine=EngineName.DEVELOPMENT,
        cadence_hours=4,
    ),
    MaintenanceItem(
        slug="refresh-market-signals",
        objective=(
            "Re-score pending ideas against the newest market signals so the build queue "
            "stays ordered by current evidence rather than arrival time."
        ),
        engine=EngineName.DISCOVERY,
        cadence_hours=12,
    ),
    # --- the learning loop -----------------------------------------------------
    # All three are handled by dedicated branches in `engines/maintenance.py::run()`
    # before the generic agent-spawn path — the first is agent-driven (a proposal),
    # the other two are fully deterministic, no LLM call.
    MaintenanceItem(
        slug="propose-prompt-mutation",
        objective=(
            "Review recent outcomes for each allowlisted prompt/weight key and recommend "
            "a concrete revision, or no change."
        ),
        engine=EngineName.SUPERVISOR,
        cadence_hours=48,
    ),
    MaintenanceItem(
        slug="evaluate-tunable-trials",
        objective="Promote eligible candidates to trial and judge trials that have run.",
        engine=EngineName.SUPERVISOR,
        cadence_hours=6,
    ),
    MaintenanceItem(
        slug="reconcile-outreach-conversions",
        objective="Attribute recent revenue to the outreach that plausibly caused it.",
        engine=EngineName.MARKETING,
        cadence_hours=24,
    ),
    # --- memory hygiene ----------------------------------------------------------
    # Unrelated to the learning loop above — this bounds `factory.memory`, the
    # lexical lesson store every ephemeral agent spawn reads and writes, which has
    # no cap of its own otherwise. Also handled by a dedicated, deterministic branch
    # in `engines/maintenance.py::run()`.
    MaintenanceItem(
        slug="prune-memory",
        objective=(
            "Evict the lowest-value memories in any scope that has exceeded its budget, "
            "so recall and reinforcement stay cheap as the memory store grows."
        ),
        engine=EngineName.SUPERVISOR,
        cadence_hours=24,
    ),
    # --- HITL follow-through -----------------------------------------------------
    # `deliver_pending` guarantees a stale escalation was *sent*, not that a human
    # *saw* it. Deterministic, no LLM call -- handled by a dedicated branch in
    # `engines/maintenance.py::run()`.
    MaintenanceItem(
        slug="remind-stale-interrupts",
        objective=(
            "Send one consolidated reminder for every open interrupt that has been "
            "waiting for a human reply past the staleness threshold."
        ),
        engine=EngineName.SUPERVISOR,
        cadence_hours=6,
    ),
)


def _bucket(cadence_hours: int, now: datetime | None = None) -> str:
    """Collapse time into cadence-sized buckets so one task exists per window."""
    now = now or datetime.now(UTC)
    epoch_hours = int(now.timestamp() // 3600)
    return str(epoch_hours // max(cadence_hours, 1))


async def enqueue_maintenance(
    session: AsyncSession,
    *,
    enabled_engines: set[EngineName],
    now: datetime | None = None,
) -> list[str]:
    """Queue any maintenance item whose cadence window has no task yet.

    ``item.engine`` is a *topic* tag, used only to decide whether an item is
    relevant given which engines are currently enabled (e.g. don't bother auditing
    live products if Development/Docker is unavailable) -- it is deliberately NOT
    the engine the resulting task is dispatched to. Every real `TaskKind.MAINTENANCE`
    handler lives in `factory.engines.maintenance.run()`, registered only for
    `EngineName.SUPERVISOR` (`engines/base.py`); a task previously enqueued with
    `engine=item.engine` (e.g. DEVELOPMENT for `audit-live-products`) was routed
    instead to that engine's own `run()`, which has never heard of
    `TaskKind.MAINTENANCE` and completed it instantly with an "unsupported task
    kind" string -- so `audit-live-products`, `improve-test-coverage`, and
    `reconcile-outreach-conversions` had never actually executed once, and every
    completion looked identical to a real, successful maintenance run.
    """
    created: list[str] = []
    for item in CATALOGUE:
        if item.engine not in enabled_engines:
            continue
        key = f"maintenance:{item.slug}:{_bucket(item.cadence_hours, now)}"
        task_id = await repository.enqueue_task(
            session,
            kind=TaskKind.MAINTENANCE,
            engine=EngineName.SUPERVISOR,
            idempotency_key=key,
            payload={"slug": item.slug, "objective": item.objective},
            priority=500,  # always yields to revenue-bearing work
        )
        if task_id:
            created.append(item.slug)
    if created:
        log.info("backlog.enqueued", items=created)
    return created
