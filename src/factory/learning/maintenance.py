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

from pydantic import Field, model_validator
from sqlalchemy.ext.asyncio import AsyncSession

from factory.agents.roles import RoleName
from factory.agents.spawner import SpawnRequest, spawn
from factory.learning import store
from factory.learning.allowlist import MUTABLE_KEYS, default_metric_for
from factory.llm.providers.base import LLMError
from factory.logging import get_logger
from factory.memory import store as memory_store
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
    #: Field names here must exactly match `ScoredIdea.SCORE_DIMENSIONS` -- see
    #: `tests/unit/test_learning_maintenance.py`'s parity test. This class can't
    #: import `SCORE_DIMENSIONS` and generate its fields dynamically (Pydantic
    #: needs them declared, and dynamic field generation would make every future
    #: dimension change silently untyped instead of a one-line addition here); the
    #: parity test exists precisely so the next dimension addition can't add it to
    #: `ScoredIdea` and forget it here, the way this one very nearly did.
    no_change_recommended: bool
    pain_severity: Annotated[float, Field(ge=0.0, le=1.0)]
    willingness_to_pay: Annotated[float, Field(ge=0.0, le=1.0)]
    build_feasibility: Annotated[float, Field(ge=0.0, le=1.0)]
    adoption_ease: Annotated[float, Field(ge=0.0, le=1.0)]
    defensibility: Annotated[float, Field(ge=0.0, le=1.0)]
    rationale: str = Field(min_length=20, max_length=1000)

    @model_validator(mode="after")
    def _weights_sum_to_one(self) -> WeightsCandidate:
        """`ScoredIdea.weighted_score` is a plain weighted sum with no normalisation
        by the weights' own total, and `SCORE_THRESHOLD` (0.55) is a fixed constant
        calibrated against weights that sum to 1.0 -- bounding each weight to [0,1]
        independently is not enough. A proposal like {0.5, 0.5, 0.5, 0.5, 0.5} (sum
        2.5) would roughly double every idea's score, pushing survival rate toward
        100% without SCORE_THRESHOLD ever changing, and the resulting trial would
        look like a clear, statistically significant win with no way for the loop to
        self-correct once confirmed. Skipped entirely (not renormalised) when it
        recommends no change, since the numeric fields are unused defaults then.
        """
        if self.no_change_recommended:
            return self
        total = (
            self.pain_severity
            + self.willingness_to_pay
            + self.build_feasibility
            + self.adoption_ease
            + self.defensibility
        )
        if abs(total - 1.0) > 0.02:
            raise ValueError(
                f"scoring weights must sum to ~1.0 (calibrated against a fixed "
                f"SCORE_THRESHOLD), got {total:.4f}"
            )
        return self


class SenderIdentityCandidate(Contract):
    #: Two space-separated name-shaped tokens, nothing else -- this is the *only*
    #: gate a candidate sender name passes through before it starts appearing in a
    #: raw email `From` header (see `marketing/outreach.py::send_to_lead`): the
    #: content-safety checks in `compliance/rules.py` only ever inspect the message
    #: body, never the sender name. The pattern excludes `<`, `>`, `@`, digits, and
    #: control/CRLF characters *by construction*, not by omission -- a candidate
    #: like "Alex Reyes <attacker@evil.test>" would otherwise silently override the
    #: real mailbox, and a CRLF would allow header injection.
    no_change_recommended: bool
    sender_name: str = Field(
        min_length=4, max_length=60, pattern=r"^[A-Za-z][A-Za-z.'-]*(?: [A-Za-z][A-Za-z.'-]*)+$"
    )
    rationale: str = Field(min_length=20, max_length=1000)


_EXTRACT_SYSTEM = """\
You extract a structured mutation proposal from an analyst's free-text review of a \
prompt, a set of scoring weights, or a sender display name.

If the analyst recommended no change, set no_change_recommended=true and leave the \
other fields at safe defaults. Never invent a recommendation the analyst did not \
actually make.\
"""


def _engine_for_key(key: str) -> EngineName | None:
    """Best-effort engine name from a ``module.path:ATTRIBUTE`` allowlist key
    (e.g. ``factory.engines.discovery.score:GENERATOR_SYSTEM`` → ``discovery``),
    for scoping the recall below. ``None`` for a key this can't parse -- the
    recall then just falls back to global-scope memories only, never an error."""
    module_path = key.split(":", 1)[0]
    parts = module_path.split(".")
    if len(parts) >= 3 and parts[0] == "factory" and parts[1] == "engines":
        try:
            return EngineName(parts[2])
        except ValueError:
            return None
    return None


async def _episode_summary(session: AsyncSession, key: str) -> str:
    """Aggregate counts plus a handful of concrete recent failures — bounded and
    cheap, not the raw episode table. Also carries whatever accumulated memory
    this key's own engine has, so the proposing agent sees more than a bare
    success rate -- a pure autonomy increase, no new gate: the analyst still
    only ever produces prose, and `insert_candidate` still never promotes it."""
    from factory.learning.store import get_active

    active = await get_active(session, key)
    if active is None:
        return f"No data yet for {key} — it has never been read."

    successes, total = await store.episode_counts(session, active.id, limit=200)
    rate = round(successes / total, 3) if total else 0.0
    # The key itself (a bare module path) has almost no lexical overlap with the
    # natural-language content memories are written in -- the active value's own
    # text does, since it's the same vocabulary a real prompt or a set of
    # weight names would share with a lesson about how well it performed.
    value_text = active.value.get("text", "") if isinstance(active.value, dict) else ""
    query = f"{active.target_metric} {value_text}"[:1000]
    memory_block = await memory_store.recall_block(
        session, query=query, scopes=memory_store.scopes_for(engine=_engine_for_key(key))
    )
    return (
        f"Key: {key}\n"
        f"Current version {active.version} (active since {active.promoted_at}).\n"
        f"Current text/value: {active.value}\n"
        f"Recent outcomes (last 200): {successes}/{total} succeeded ({rate:.1%}).\n"
        f"Target metric: {active.target_metric}." + (f"\n\n{memory_block}" if memory_block else "")
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
        is_identity = key.endswith(":SENDER_IDENTITY")
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
                    "defensibility": weights_candidate.defensibility,
                }
            elif is_identity:
                identity_candidate = await ctx.llm.parse(
                    SenderIdentityCandidate,
                    task_type=TaskType.ANALYSIS,
                    system=_EXTRACT_SYSTEM,
                    prompt=result.answer,
                    engine=EngineName.SUPERVISOR,
                    task_id=task.id,
                )
                no_change = identity_candidate.no_change_recommended
                rationale = identity_candidate.rationale
                value = {"text": identity_candidate.sender_name}
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
