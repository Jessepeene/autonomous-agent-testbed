"""Deterministic trial promotion and rollback. No LLM call anywhere in this module —
whether a mutation sticks is a statistical decision over recorded outcomes, never a
judgement the proposing agent (or any agent) makes about its own work.

The tradeoff, stated plainly: a mutated prompt runs live, unattended, for the whole
trial window (up to ``MAX_TRIAL_DAYS``) before this ever runs, and a real
but moderate regression can still auto-confirm on a small sample (see the worked
examples in ``tests/unit/test_learning_stats.py``). That is acceptable only because
every key on the allowlist is commercially-lossy, never safety-critical — the LIA,
Bug Council, orchestrator, and codegen prompts are permanently excluded from this
mechanism entirely.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from factory.learning import store
from factory.learning.stats import fisher_exact_less
from factory.logging import get_logger
from factory.state.orm import TunableParameter

log = get_logger(__name__)

#: Minimum episodes before a trial may be judged at all, per target metric. Outreach
#: metrics need more volume: `outreach_content_pass_rate` is same-day/high-volume so
#: 100 is cheap to reach; `outreach_conversion_rate` is deliberately not the primary
#: gate for exactly this reason (see allowlist.DEFAULT_METRIC and the power analysis
#: in test_learning_stats.py — a realistic baseline is too low to reliably catch a
#: regression at any volume this system will plausibly generate in one trial).
MIN_TRIAL_N: dict[str, int] = {
    "idea_survival_rate": 30,
    "planner_effective_rate": 30,
    "outreach_content_pass_rate": 100,
    "outreach_conversion_rate": 200,
}
_DEFAULT_MIN_N = 30

#: A trial is judged once it has enough episodes, or once it has run this long
#: regardless of volume — a key that simply doesn't get much traffic must not stay on
#: trial forever.
MAX_TRIAL_DAYS = 30

#: One-sided Fisher's exact p-value below which a trial is rolled back.
ROLLBACK_ALPHA = 0.05

#: How long a candidate must exist before it may be promoted to trial — long enough
#: for the human-visible audit trail (Telegram, `llm_calls`/`tunable_parameters`
#: rows) to actually be noticed before anything goes live.
PROMOTION_COOLDOWN_HOURS = 48

#: How long a trial runs before being judged on volume alone (see MAX_TRIAL_DAYS).
TRIAL_DAYS = 14


async def promote_and_evaluate(session: AsyncSession) -> str:
    """The one entry point the maintenance job calls: promote at most one eligible
    candidate to trial, then judge every trial currently running (in practice, at
    most one — the one-trial-at-a-time serialization rule)."""
    promoted = await store.promote_eligible_candidates(
        session, cooldown_hours=PROMOTION_COOLDOWN_HOURS, trial_days=TRIAL_DAYS
    )
    resolved = 0
    for trial in await store.list_trials(session):
        if await _evaluate_one(session, trial):
            resolved += 1
    return f"learning: {len(promoted)} promoted to trial, {resolved} trial(s) resolved"


async def _evaluate_one(session: AsyncSession, trial: TunableParameter) -> bool:
    """Returns whether the trial was resolved (confirmed or rolled back) this call."""
    successes, total = await store.episode_counts(session, trial.id)
    min_n = MIN_TRIAL_N.get(trial.target_metric, _DEFAULT_MIN_N)

    started = trial.trial_started_at or datetime.now(UTC)
    age_days = (datetime.now(UTC) - started).days
    if total < min_n and age_days < MAX_TRIAL_DAYS:
        return False  # still collecting

    baseline = await session.get(TunableParameter, trial.parent_id) if trial.parent_id else None
    if baseline is None:
        # First-ever version for this key — nothing to compare against.
        await store.confirm_trial(session, trial.id, numerator=successes, denominator=total)
        log.info("learning.trial_confirmed_no_baseline", key=trial.key)
        return True

    base_successes, base_total = await store.episode_counts(session, baseline.id, limit=200)
    p_value = fisher_exact_less(
        successes, total - successes, base_successes, base_total - base_successes
    )
    if p_value < ROLLBACK_ALPHA:
        await store.rollback_trial(
            session,
            trial.id,
            numerator=successes,
            denominator=total,
            reason=(
                f"Fisher exact p={p_value:.4f}: {successes}/{total} vs. baseline "
                f"{base_successes}/{base_total} on {trial.target_metric}"
            ),
        )
    else:
        await store.confirm_trial(session, trial.id, numerator=successes, denominator=total)
    return True
