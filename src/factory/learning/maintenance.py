"""The proposal step: an ephemeral ANALYST reviews recent outcomes for one allowlisted
key and recommends a concrete revision, or no change.

Two properties matter here:

* **The agent never writes a `tunable_parameters` row.** It produces prose
  (`AgentResult.answer`); a second, separate structured-extraction call turns that
  prose into a validated `PromptCandidate`/`WeightsCandidate` — this codebase's own
  rule that a decision is read from a validated field, never regex'd from text.
* **The proposal step never promotes.** `store.insert_candidate` always writes
  `status='candidate'`. Promotion to `trial` is a separate, deterministic function
  (`trials.promote_and_evaluate`) gated on a cooldown and the one-trial-at-a-time
  rule — never something the proposing agent controls.

`RoleName.ANALYST` is reused rather than a new role added to the fixed catalogue: this
job needs exactly ANALYST's read-only envelope, and the agent is never trusted with a
mutating capability here regardless.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import Field
from sqlalchemy.ext.asyncio import AsyncSession

from factory.agents.roles import RoleName
from factory.agents.spawner import SpawnRequest, spawn
from factory.learning import store
from factory.learning.allowlist import MUTABLE_KEYS, default_metric_for
from factory.llm.providers.base import LLMError
from factory.logging import get_logger
from factory.settings import EngineName, TaskType
from factory.state.orm import Task
from factory.state.schemas import Contract
from factory.supervisor.context import FactoryContext

log = get_logger(__name__)

#: How long a key must go without a fresh proposal before another is considered —
#: independent of the trial-promotion cooldown, this just stops the proposal step
#: itself from re-litigating a key every 48 hours forever.
PROPOSAL_COOLDOWN_HOURS = 336  # 14 days


class PromptCandidate(Contract):
    no_change_recommended: bool
    proposed_text: str = Field(min_length=20, max_length=4000)
    rationale: str = Field(min_length=20, max_length=1000)


class WeightsCandidate(Contract):
    no_change_recommended: bool
    pain_severity: Annotated[float, Field(ge=0.0, le=1.0)]
    willingness_to_pay: Annotated[float, Field(ge=0.0, le=1.0)]
    build_feasibility: Annotated[float, Field(ge=0.0, le=1.0)]
    adoption_ease: Annotated[float, Field(ge=0.0, le=1.0)]
    rationale: str = Field(min_length=20, max_length=1000)


_EXTRACT_SYSTEM = """\
You extract a structured mutation proposal from an analyst's free-text review of a \
prompt or a set of scoring weights.

If the analyst recommended no change, set no_change_recommended=true and leave the \
other fields at safe defaults. Never invent a recommendation the analyst did not \
actually make.\
"""


async def _episode_summary(session: AsyncSession, key: str) -> str:
    """Aggregate counts plus a handful of concrete recent failures — bounded and
    cheap, not the raw episode table."""
    from factory.learning.store import get_active

    active = await get_active(session, key)
    if active is None:
        return f"No data yet for {key} — it has never been read."

    successes, total = await store.episode_counts(session, active.id, limit=200)
    rate = round(successes / total, 3) if total else 0.0
    return (
        f"Key: {key}\n"
        f"Current version {active.version} (active since {active.promoted_at}).\n"
        f"Current text/value: {active.value}\n"
        f"Recent outcomes (last 200): {successes}/{total} succeeded ({rate:.1%}).\n"
        f"Target metric: {active.target_metric}."
    )


async def propose_mutations(ctx: FactoryContext, session: AsyncSession, task: Task) -> str:
    proposed = 0
    for key in sorted(MUTABLE_KEYS):
        if await store.has_open_work(session, key):
            continue
        if await store.recently_resolved(session, key, cooldown_hours=PROPOSAL_COOLDOWN_HOURS):
            continue

        context = await _episode_summary(session, key)
        result = await spawn(
            ctx,
            session,
            SpawnRequest(
                role=RoleName.ANALYST,
                objective=(
                    f"Review recent outcomes for {key} and recommend a concrete revised "
                    "prompt or scoring weights, or state plainly that no change is warranted."
                ),
                context=context,
                engine=EngineName.SUPERVISOR,
                task_id=task.id,
            ),
        )
        if not result.succeeded or not result.answer.strip():
            continue

        is_weights = key.endswith(":SCORING_WEIGHTS")
        rationale: str
        value: dict[str, float | str]
        no_change: bool
        try:
            if is_weights:
                weights_candidate = await ctx.llm.parse(
                    WeightsCandidate,
                    task_type=TaskType.ANALYSIS,
                    system=_EXTRACT_SYSTEM,
                    prompt=result.answer,
                    engine=EngineName.SUPERVISOR,
                    task_id=task.id,
                )
                no_change = weights_candidate.no_change_recommended
                rationale = weights_candidate.rationale
                value = {
                    "pain_severity": weights_candidate.pain_severity,
                    "willingness_to_pay": weights_candidate.willingness_to_pay,
                    "build_feasibility": weights_candidate.build_feasibility,
                    "adoption_ease": weights_candidate.adoption_ease,
                }
            else:
                prompt_candidate = await ctx.llm.parse(
                    PromptCandidate,
                    task_type=TaskType.ANALYSIS,
                    system=_EXTRACT_SYSTEM,
                    prompt=result.answer,
                    engine=EngineName.SUPERVISOR,
                    task_id=task.id,
                )
                no_change = prompt_candidate.no_change_recommended
                rationale = prompt_candidate.rationale
                value = {"text": prompt_candidate.proposed_text}
        except LLMError as exc:
            log.warning("learning.extraction_failed", key=key, detail=str(exc)[:200])
            continue

        if no_change:
            continue

        active = await store.get_active(session, key)
        await store.insert_candidate(
            session,
            key=key,
            kind="SCORING_WEIGHTS" if is_weights else "PROMPT",
            value=value,
            rationale=rationale,
            created_by="agent:analyst",
            parent_id=active.id if active else None,
            target_metric=default_metric_for(key),
        )
        proposed += 1

    return f"learning: {proposed} candidate(s) proposed"
