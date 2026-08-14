"""The tunable-parameters lifecycle: bootstrap, candidate, trial, confirm/rollback.

Needs Postgres for JSONB and the partial-unique-active index:

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

from factory.learning import store
from factory.learning.allowlist import ProtectedKey
from factory.learning.stats import fisher_exact_less
from factory.learning.trials import _MIN_ABSOLUTE_N, MAX_TRIAL_DAYS, _evaluate_one
from factory.state.orm import Base, TunableParameter

DATABASE_URL = os.environ.get("DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not DATABASE_URL, reason="DATABASE_URL is not set; integration tests need Postgres"
)

_KEY = "factory.engines.marketing.outreach:SYSTEM_PROMPT"


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


async def test_first_read_bootstraps_an_active_version_1(session: AsyncSession) -> None:
    from factory.learning.prompts import get_prompt

    text, version_id = await get_prompt(session, key=_KEY, default="the default text")
    await session.commit()

    assert text == "the default text"
    row = await session.get(TunableParameter, version_id)
    assert row is not None
    assert row.version == 1
    assert row.status == "active"
    assert row.created_by == "bootstrap"


async def test_a_second_read_returns_the_stored_value_not_the_default(
    session: AsyncSession,
) -> None:
    from factory.learning.prompts import get_prompt

    await store.bootstrap_active(
        session,
        kind="PROMPT",
        key=_KEY,
        value={"text": "original"},
        created_by="bootstrap",
        target_metric="outreach_content_pass_rate",
    )
    await session.commit()

    text, _ = await get_prompt(session, key=_KEY, default="a different default")
    assert text == "original"


# --- get_scoring_weights: dimension-mismatch guardrail (Phase 4) -------------------
#
# Added when ScoredIdea gained a 5th weighted dimension (defensibility):
# `learning/prompts.py::get_prompt`/`get_scoring_weights` serve whatever's in
# tunable_parameters with no re-sync path, so a database that still has an old
# 4-key SCORING_WEIGHTS row (from before 0023_idea_defensibility rewrote it, or a
# database that migration was somehow never run against) must not silently serve
# those stale weights forever -- `weighted_score`'s `weights.get(dim, 0.0)` would
# zero out defensibility for every idea without anyone deciding that.

_WEIGHTS_KEY = "factory.engines.discovery.score:SCORING_WEIGHTS"


async def test_get_scoring_weights_serves_a_matching_row_unchanged(
    session: AsyncSession,
) -> None:
    from factory.learning.prompts import get_scoring_weights
    from factory.state.schemas import ScoredIdea

    default = ScoredIdea.default_weights()
    await store.bootstrap_active(
        session,
        kind="SCORING_WEIGHTS",
        key=_WEIGHTS_KEY,
        value=default,
        created_by="bootstrap",
        target_metric="idea_survival_rate",
    )
    await session.commit()

    weights, _ = await get_scoring_weights(session, key=_WEIGHTS_KEY, default=default)

    assert weights == default


async def test_get_scoring_weights_falls_back_on_a_dimension_mismatch(
    session: AsyncSession,
) -> None:
    """A row bootstrapped before defensibility existed -- exactly what a database
    would still have if 0023_idea_defensibility's rewrite were somehow skipped."""
    from factory.learning.prompts import get_scoring_weights
    from factory.state.schemas import ScoredIdea

    old_default = {
        "pain_severity": 0.3,
        "willingness_to_pay": 0.3,
        "adoption_ease": 0.25,
        "build_feasibility": 0.15,
    }
    await store.bootstrap_active(
        session,
        kind="SCORING_WEIGHTS",
        key=_WEIGHTS_KEY,
        value=old_default,
        created_by="bootstrap",
        target_metric="idea_survival_rate",
    )
    await session.commit()

    new_default = ScoredIdea.default_weights()
    weights, tunable_parameter_id = await get_scoring_weights(
        session, key=_WEIGHTS_KEY, default=new_default
    )

    assert weights == new_default  # the code default, not the stale served row
    assert tunable_parameter_id is not None  # still identifies the drifted row


async def test_concurrent_bootstrap_creates_only_one_row(session: AsyncSession) -> None:
    """Simulates the race directly at the store level: two bootstrap attempts for the
    same key, the second must observe the first's row rather than erroring or
    creating a duplicate."""
    first_id = await store.bootstrap_active(
        session,
        kind="PROMPT",
        key=_KEY,
        value={"text": "a"},
        created_by="bootstrap",
        target_metric="outreach_content_pass_rate",
    )
    second_id = await store.bootstrap_active(
        session,
        kind="PROMPT",
        key=_KEY,
        value={"text": "b"},
        created_by="bootstrap",
        target_metric="outreach_content_pass_rate",
    )
    await session.commit()

    assert first_id == second_id
    from sqlalchemy import func, select

    count = (
        await session.execute(
            select(func.count()).select_from(TunableParameter).where(TunableParameter.key == _KEY)
        )
    ).scalar_one()
    assert count == 1


async def test_a_candidate_never_serves_until_promoted_to_trial(session: AsyncSession) -> None:
    from factory.learning.prompts import get_prompt

    active_id = await store.bootstrap_active(
        session,
        kind="PROMPT",
        key=_KEY,
        value={"text": "active text"},
        created_by="bootstrap",
        target_metric="outreach_content_pass_rate",
    )
    await store.insert_candidate(
        session,
        key=_KEY,
        kind="PROMPT",
        value={"text": "candidate text"},
        rationale="x" * 30,
        created_by="agent:analyst",
        parent_id=active_id,
        target_metric="outreach_content_pass_rate",
    )
    await session.commit()

    text, version_id = await get_prompt(session, key=_KEY, default="unused")
    assert text == "active text"
    assert version_id == active_id


async def test_a_trial_serves_over_the_active_version(session: AsyncSession) -> None:
    from factory.learning.prompts import get_prompt

    active_id = await store.bootstrap_active(
        session,
        kind="PROMPT",
        key=_KEY,
        value={"text": "active text"},
        created_by="bootstrap",
        target_metric="outreach_content_pass_rate",
    )
    candidate_id = await store.insert_candidate(
        session,
        key=_KEY,
        kind="PROMPT",
        value={"text": "trial text"},
        rationale="x" * 30,
        created_by="agent:analyst",
        parent_id=active_id,
        target_metric="outreach_content_pass_rate",
    )
    promoted = await store.promote_eligible_candidates(session, cooldown_hours=0, trial_days=14)
    await session.commit()

    assert promoted == [candidate_id]
    text, version_id = await get_prompt(session, key=_KEY, default="unused")
    assert text == "trial text"
    assert version_id == candidate_id


async def test_only_one_trial_may_run_at_a_time_across_the_whole_allowlist(
    session: AsyncSession,
) -> None:
    other_key = "factory.engines.discovery.score:GENERATOR_SYSTEM"
    for key in (_KEY, other_key):
        active_id = await store.bootstrap_active(
            session,
            kind="PROMPT",
            key=key,
            value={"text": "active"},
            created_by="bootstrap",
            target_metric="outreach_content_pass_rate",
        )
        await store.insert_candidate(
            session,
            key=key,
            kind="PROMPT",
            value={"text": "candidate"},
            rationale="x" * 30,
            created_by="agent:analyst",
            parent_id=active_id,
            target_metric="outreach_content_pass_rate",
        )
    await session.commit()

    first = await store.promote_eligible_candidates(session, cooldown_hours=0, trial_days=14)
    second = await store.promote_eligible_candidates(session, cooldown_hours=0, trial_days=14)

    assert len(first) == 1
    assert second == []


async def test_confirm_retires_the_parent_and_activates_the_trial(session: AsyncSession) -> None:
    active_id = await store.bootstrap_active(
        session,
        kind="PROMPT",
        key=_KEY,
        value={"text": "v1"},
        created_by="bootstrap",
        target_metric="outreach_content_pass_rate",
    )
    trial_id = await store.insert_candidate(
        session,
        key=_KEY,
        kind="PROMPT",
        value={"text": "v2"},
        rationale="x" * 30,
        created_by="agent:analyst",
        parent_id=active_id,
        target_metric="outreach_content_pass_rate",
    )
    await store.confirm_trial(session, trial_id, numerator=40, denominator=100)
    await session.commit()

    parent = await session.get(TunableParameter, active_id)
    trial = await session.get(TunableParameter, trial_id)
    assert parent is not None and parent.status == "retired"
    assert trial is not None and trial.status == "active"
    assert trial.trial_numerator == 40
    assert trial.trial_denominator == 100


async def test_rollback_leaves_the_previous_active_version_untouched(
    session: AsyncSession,
) -> None:
    """Rollback never 'reactivates' the parent — it never stopped being active."""
    active_id = await store.bootstrap_active(
        session,
        kind="PROMPT",
        key=_KEY,
        value={"text": "v1"},
        created_by="bootstrap",
        target_metric="outreach_content_pass_rate",
    )
    trial_id = await store.insert_candidate(
        session,
        key=_KEY,
        kind="PROMPT",
        value={"text": "v2 (bad)"},
        rationale="x" * 30,
        created_by="agent:analyst",
        parent_id=active_id,
        target_metric="outreach_content_pass_rate",
    )
    await store.rollback_trial(session, trial_id, numerator=6, denominator=40, reason="regressed")
    await session.commit()

    parent = await session.get(TunableParameter, active_id)
    trial = await session.get(TunableParameter, trial_id)
    assert parent is not None and parent.status == "active"
    assert trial is not None and trial.status == "retired"
    assert trial.retired_reason == "regressed"


@pytest.mark.parametrize(
    "key",
    [
        "factory.engines.compliance.assessor:LIA_SYSTEM_PROMPT",
        "factory.engines.development.bug_council:REVIEWER_PROMPTS",
        "factory.supervisor.orchestrator:_SYSTEM_PROMPT_TEMPLATE",
        "factory.engines.development.scaffold.generator:SYSTEM_PROMPT",
    ],
)
async def test_confirm_and_rollback_refuse_a_protected_key_even_if_a_row_exists(
    session: AsyncSession, key: str
) -> None:
    """Defense in depth: even a row that should never exist (bypassing
    insert_candidate, which itself would have refused) is rejected by confirm/
    rollback specifically, proving the guard lives in every write path, not just
    the one intended entry point."""
    row = TunableParameter(
        kind="PROMPT",
        key=key,
        value={"text": "should never have been created"},
        version=1,
        status="trial",
        target_metric="idea_survival_rate",
        rationale="bypassing the guard for this test",
        created_by="test",
    )
    session.add(row)
    await session.flush()

    with pytest.raises(ProtectedKey):
        await store.confirm_trial(session, row.id, numerator=1, denominator=1)
    with pytest.raises(ProtectedKey):
        await store.rollback_trial(session, row.id, numerator=1, denominator=1, reason="x")


# --- the worked rollback example, end to end through _evaluate_one -------------


async def _seed_episodes(
    session: AsyncSession, tunable_parameter_id: str, *, successes: int, failures: int
) -> None:
    for _ in range(successes):
        await store.record_episode(
            session, tunable_parameter_id=tunable_parameter_id, outcome="success"
        )
    for _ in range(failures):
        await store.record_episode(
            session, tunable_parameter_id=tunable_parameter_id, outcome="failure"
        )


async def test_evaluate_one_rolls_back_on_a_clear_regression(session: AsyncSession) -> None:
    """The plan's worked example: trial 6/40 (15.0%) vs. baseline 40/120 (33.3%)."""
    active_id = await store.bootstrap_active(
        session,
        kind="PROMPT",
        key=_KEY,
        value={"text": "baseline"},
        created_by="bootstrap",
        target_metric="idea_survival_rate",
    )
    await _seed_episodes(session, active_id, successes=40, failures=80)

    trial_id = await store.insert_candidate(
        session,
        key=_KEY,
        kind="PROMPT",
        value={"text": "regressive candidate"},
        rationale="x" * 30,
        created_by="agent:analyst",
        parent_id=active_id,
        target_metric="idea_survival_rate",
    )
    await store.promote_eligible_candidates(session, cooldown_hours=0, trial_days=14)
    trial = await session.get(TunableParameter, trial_id)
    assert trial is not None
    await session.execute(
        TunableParameter.__table__.update()
        .where(TunableParameter.id == trial_id)
        .values(trial_started_at=datetime.now(UTC) - timedelta(days=1))
    )
    await session.refresh(trial)
    await _seed_episodes(session, trial_id, successes=6, failures=34)
    await session.commit()

    resolved = await _evaluate_one(session, trial)
    await session.commit()

    assert resolved is True
    updated = await session.get(TunableParameter, trial_id)
    assert updated is not None
    assert updated.status == "retired"
    assert updated.retired_reason is not None
    assert "Fisher exact" in updated.retired_reason

    # Sanity: the p-value actually embedded matches the direct computation.
    expected_p = fisher_exact_less(6, 34, 40, 80)
    assert f"p={expected_p:.4f}" in updated.retired_reason


async def test_evaluate_one_confirms_when_the_regression_is_not_significant(
    session: AsyncSession,
) -> None:
    """8/40 (20.0%) vs. baseline 40/120 (33.3%) — real but unprovable at this n."""
    active_id = await store.bootstrap_active(
        session,
        kind="PROMPT",
        key=_KEY,
        value={"text": "baseline"},
        created_by="bootstrap",
        target_metric="idea_survival_rate",
    )
    await _seed_episodes(session, active_id, successes=40, failures=80)

    trial_id = await store.insert_candidate(
        session,
        key=_KEY,
        kind="PROMPT",
        value={"text": "candidate"},
        rationale="x" * 30,
        created_by="agent:analyst",
        parent_id=active_id,
        target_metric="idea_survival_rate",
    )
    await store.promote_eligible_candidates(session, cooldown_hours=0, trial_days=14)
    trial = await session.get(TunableParameter, trial_id)
    assert trial is not None
    await _seed_episodes(session, trial_id, successes=8, failures=32)
    await session.commit()
    await session.refresh(trial)

    resolved = await _evaluate_one(session, trial)
    await session.commit()

    assert resolved is True
    updated = await session.get(TunableParameter, trial_id)
    assert updated is not None
    assert updated.status == "active"

    parent = await session.get(TunableParameter, active_id)
    assert parent is not None
    assert parent.status == "retired"


async def test_evaluate_one_waits_for_more_data_below_the_minimum_sample(
    session: AsyncSession,
) -> None:
    active_id = await store.bootstrap_active(
        session,
        kind="PROMPT",
        key=_KEY,
        value={"text": "baseline"},
        created_by="bootstrap",
        target_metric="idea_survival_rate",
    )
    trial_id = await store.insert_candidate(
        session,
        key=_KEY,
        kind="PROMPT",
        value={"text": "candidate"},
        rationale="x" * 30,
        created_by="agent:analyst",
        parent_id=active_id,
        target_metric="idea_survival_rate",
    )
    await store.promote_eligible_candidates(session, cooldown_hours=0, trial_days=14)
    trial = await session.get(TunableParameter, trial_id)
    assert trial is not None
    await _seed_episodes(session, trial_id, successes=2, failures=1)  # well under min_n=30
    await session.commit()
    await session.refresh(trial)

    resolved = await _evaluate_one(session, trial)

    assert resolved is False
    still_trial = await session.get(TunableParameter, trial_id)
    assert still_trial is not None
    assert still_trial.status == "trial"


# --- 2026-08-11 audit fix: no confirming on near-zero data at timeout ----------


async def _backdate_trial_until(session: AsyncSession, trial_id: str, *, days_ago: int) -> None:
    """Simulates a trial whose MAX_TRIAL_DAYS deadline has already passed."""
    past = datetime.now(UTC) - timedelta(days=days_ago)
    await session.execute(
        TunableParameter.__table__.update()
        .where(TunableParameter.id == trial_id)
        .values(trial_started_at=past, trial_until=past + timedelta(days=MAX_TRIAL_DAYS))
    )


async def test_timed_out_trial_with_near_zero_episodes_is_rolled_back_not_confirmed(
    session: AsyncSession,
) -> None:
    """Regression test for the bug where fisher_exact_less(0, 0, base_s, base_f) ==
    1.0 for every baseline, so a trial with essentially no data auto-confirmed by
    default once MAX_TRIAL_DAYS passed -- silently making an unproven mutation
    permanent."""
    active_id = await store.bootstrap_active(
        session,
        kind="PROMPT",
        key=_KEY,
        value={"text": "baseline"},
        created_by="bootstrap",
        target_metric="idea_survival_rate",
    )
    await _seed_episodes(session, active_id, successes=40, failures=80)

    trial_id = await store.insert_candidate(
        session,
        key=_KEY,
        kind="PROMPT",
        value={"text": "candidate"},
        rationale="x" * 30,
        created_by="agent:analyst",
        parent_id=active_id,
        target_metric="idea_survival_rate",
    )
    await store.promote_eligible_candidates(session, cooldown_hours=0, trial_days=MAX_TRIAL_DAYS)
    await _backdate_trial_until(session, trial_id, days_ago=MAX_TRIAL_DAYS + 1)
    # Only 2 episodes -- well under both min_n and _MIN_ABSOLUTE_N.
    await _seed_episodes(session, trial_id, successes=2, failures=0)
    await session.commit()
    trial = await session.get(TunableParameter, trial_id)
    assert trial is not None
    await session.refresh(trial)

    resolved = await _evaluate_one(session, trial)
    await session.commit()

    assert resolved is True
    updated = await session.get(TunableParameter, trial_id)
    assert updated is not None
    assert updated.status == "retired"
    assert updated.retired_reason is not None
    assert "insufficient data" in updated.retired_reason
    # The baseline stays active -- nothing was promoted on 2 episodes of evidence.
    parent = await session.get(TunableParameter, active_id)
    assert parent is not None
    assert parent.status == "active"


async def test_timed_out_trial_is_also_rolled_back_when_the_baseline_lacks_data(
    session: AsyncSession,
) -> None:
    """The floor applies to both sides of the comparison -- a trial with plenty of
    its own episodes still cannot be meaningfully judged against a baseline that
    has almost none."""
    active_id = await store.bootstrap_active(
        session,
        kind="PROMPT",
        key=_KEY,
        value={"text": "baseline"},
        created_by="bootstrap",
        target_metric="idea_survival_rate",
    )
    await _seed_episodes(session, active_id, successes=1, failures=0)  # 1 episode only

    trial_id = await store.insert_candidate(
        session,
        key=_KEY,
        kind="PROMPT",
        value={"text": "candidate"},
        rationale="x" * 30,
        created_by="agent:analyst",
        parent_id=active_id,
        target_metric="idea_survival_rate",
    )
    await store.promote_eligible_candidates(session, cooldown_hours=0, trial_days=MAX_TRIAL_DAYS)
    await _backdate_trial_until(session, trial_id, days_ago=MAX_TRIAL_DAYS + 1)
    await _seed_episodes(session, trial_id, successes=20, failures=10)  # plenty on this side
    await session.commit()
    trial = await session.get(TunableParameter, trial_id)
    assert trial is not None
    await session.refresh(trial)

    resolved = await _evaluate_one(session, trial)
    await session.commit()

    assert resolved is True
    updated = await session.get(TunableParameter, trial_id)
    assert updated is not None
    assert updated.status == "retired"
    assert "insufficient data" in (updated.retired_reason or "")


async def test_timed_out_trial_at_exactly_the_absolute_floor_is_judged_normally(
    session: AsyncSession,
) -> None:
    """At or above _MIN_ABSOLUTE_N on both sides, a timed-out trial is still judged
    by Fisher's exact as before -- the floor only blocks judging on less than this."""
    active_id = await store.bootstrap_active(
        session,
        kind="PROMPT",
        key=_KEY,
        value={"text": "baseline"},
        created_by="bootstrap",
        target_metric="idea_survival_rate",
    )
    await _seed_episodes(session, active_id, successes=_MIN_ABSOLUTE_N, failures=0)

    trial_id = await store.insert_candidate(
        session,
        key=_KEY,
        kind="PROMPT",
        value={"text": "candidate"},
        rationale="x" * 30,
        created_by="agent:analyst",
        parent_id=active_id,
        target_metric="idea_survival_rate",
    )
    await store.promote_eligible_candidates(session, cooldown_hours=0, trial_days=MAX_TRIAL_DAYS)
    await _backdate_trial_until(session, trial_id, days_ago=MAX_TRIAL_DAYS + 1)
    await _seed_episodes(session, trial_id, successes=_MIN_ABSOLUTE_N, failures=0)
    await session.commit()
    trial = await session.get(TunableParameter, trial_id)
    assert trial is not None
    await session.refresh(trial)

    resolved = await _evaluate_one(session, trial)
    await session.commit()

    assert resolved is True
    updated = await session.get(TunableParameter, trial_id)
    assert updated is not None
    # Identical performance on both sides -- not a regression, so confirmed, and
    # via the normal Fisher's-exact path (no "insufficient data" wording).
    assert updated.status == "active"
    assert updated.retired_reason is None
