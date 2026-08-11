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

from datetime import UTC, datetime, timedelta

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
    # One episode per validated product (development/graph.py::_evaluate_validation),
    # gated behind a human campaign-approval wait and max_concurrent_validations --
    # a realistic ceiling within MAX_TRIAL_DAYS is roughly 6-8, not the 20 this was
    # originally set to (which made this metric's trials always resolve via the
    # age timeout below, never via genuine volume). Lowered so a higher-volume
    # period has a real chance to resolve on evidence; _MIN_ABSOLUTE_N is what
    # actually protects against judging on too little data either way.
    "landing_page_interest_rate": 10,
}
_DEFAULT_MIN_N = 30

#: Below this, even a timed-out trial is not judged on its numbers -- it is rolled
#: back as "insufficient data," never confirmed. Without this floor, a trial that
#: simply never accumulated meaningful volume (0-3 episodes is common for a
#: low-traffic metric like landing_page_interest_rate) would still reach the
#: MAX_TRIAL_DAYS branch below and get judged by Fisher's exact anyway --
#: `fisher_exact_less(0, 0, base_s, base_f)` is 1.0 for every baseline, so a trial
#: with literally zero episodes auto-*confirms* by default, silently making an
#: unproven mutation permanent. Applies to both the trial's own total and (in the
#: has-baseline path) the baseline's total: a comparison against a baseline with
#: too little data of its own is equally meaningless.
_MIN_ABSOLUTE_N = 5

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


async def promote_and_evaluate(session: AsyncSession) -> str:
    """The one entry point the maintenance job calls: promote at most one eligible
    candidate to trial, then judge every trial currently running (in practice, at
    most one — the one-trial-at-a-time serialization rule)."""
    # trial_days=MAX_TRIAL_DAYS: a separate, shorter TRIAL_DAYS constant used to
    # live here (14, vs. this module's real cutoff of 30) and was written to
    # `trial.trial_until` but never actually read by _evaluate_one below, which
    # computed its own age independently from `trial_started_at` -- the column
    # and the docstring both described a 14-day cutoff that didn't exist. Now the
    # single source of truth: this call sets trial_until, and _evaluate_one reads
    # it back, so there is exactly one place the trial length is defined.
    promoted = await store.promote_eligible_candidates(
        session, cooldown_hours=PROMOTION_COOLDOWN_HOURS, trial_days=MAX_TRIAL_DAYS
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

    # trial_until is the single source of truth for when volume-based patience runs
    # out (set by promote_eligible_candidates as trial_started_at + MAX_TRIAL_DAYS);
    # falling back to recomputing from trial_started_at only covers a row written
    # before this field existed.
    now = datetime.now(UTC)
    deadline = trial.trial_until or (
        (trial.trial_started_at or now) + timedelta(days=MAX_TRIAL_DAYS)
    )
    if total < min_n and now < deadline:
        return False  # still collecting

    baseline = await session.get(TunableParameter, trial.parent_id) if trial.parent_id else None
    if baseline is None:
        # First-ever version for this key — nothing to compare against, so there is
        # no "insufficient data" rollback target either; confirming is the only
        # outcome that keeps the key functional. (Unlike the has-baseline path
        # below, this is not the case the near-zero-episode auto-confirm bug
        # applies to -- there is no comparison being made here at all.)
        await store.confirm_trial(session, trial.id, numerator=successes, denominator=total)
        log.info("learning.trial_confirmed_no_baseline", key=trial.key)
        return True

    base_successes, base_total = await store.episode_counts(session, baseline.id, limit=200)
    if total < _MIN_ABSOLUTE_N or base_total < _MIN_ABSOLUTE_N:
        # Timed out with too little data on either side to draw any real
        # conclusion -- the safe default is to revert to the known-good baseline,
        # not to let an unproven mutation quietly become permanent.
        await store.rollback_trial(
            session,
            trial.id,
            numerator=successes,
            denominator=total,
            reason=(
                f"insufficient data after {MAX_TRIAL_DAYS}d: {successes}/{total} vs. "
                f"baseline {base_successes}/{base_total} on {trial.target_metric} "
                f"(minimum {_MIN_ABSOLUTE_N} required on both sides)"
            ),
        )
        log.info("learning.trial_rolled_back_insufficient_data", key=trial.key, total=total)
        return True

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
