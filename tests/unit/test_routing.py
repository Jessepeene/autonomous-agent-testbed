"""Deterministic-first routing.

The risk in this change is entirely about *when* the model is (or isn't) consulted
and whether a cached decision can ever be replayed unsafely. Token savings are the
motivation; these tests are about the safety property that makes the savings
acceptable: a model consultation must never be silently skipped when it matters, and
a side-effecting decision (SPAWN_AGENT, ESCALATE) must never fire twice for one
consultation.
"""

from __future__ import annotations

from typing import Any

import pytest

from factory.llm.providers.fake import FakeProvider
from factory.settings import EngineName, Settings
from factory.state.schemas import NextAction, OrchestratorDecision
from factory.supervisor import orchestrator, routing
from factory.supervisor.context import FactoryContext


def _snapshot(**overrides: Any) -> routing.FactorySnapshot:
    base: dict[str, Any] = {
        "eligible_engines": ["development", "discovery", "finance", "marketing", "compliance"],
        "disabled_engines": {},
        "pending_tasks_total": 5,
        "pending_by_engine": {"discovery": 5},
        "active_dev_products": 0,
        "max_concurrent_builds": 2,
        "pending_dev_retries": 0,
        "approved_ideas_waiting": 3,
        "pending_ideas": 10,
        "open_interrupts": 0,
        "spend_limit_cents": 0,
        "dry_run": True,
        "churning_products": [],
    }
    base.update(overrides)
    return routing.FactorySnapshot(**base)


# --- fingerprint --------------------------------------------------------------


def test_routine_churn_does_not_change_the_fingerprint() -> None:
    a = _snapshot(pending_by_engine={"discovery": 7})
    b = _snapshot(pending_by_engine={"discovery": 8})
    assert routing.fingerprint(a) == routing.fingerprint(b)


def test_an_order_of_magnitude_change_does_change_the_fingerprint() -> None:
    a = _snapshot(pending_by_engine={"discovery": 1})
    b = _snapshot(pending_by_engine={"discovery": 30})
    assert routing.fingerprint(a) != routing.fingerprint(b)


def test_process_lifetime_constants_are_excluded_from_the_fingerprint() -> None:
    a = _snapshot(max_concurrent_builds=2, dry_run=True, disabled_engines={})
    b = _snapshot(max_concurrent_builds=99, dry_run=False, disabled_engines={"x": "y"})
    assert routing.fingerprint(a) == routing.fingerprint(b)


def test_dev_capacity_headroom_is_reflected_in_the_fingerprint() -> None:
    a = _snapshot(active_dev_products=0, max_concurrent_builds=2)
    b = _snapshot(active_dev_products=1, max_concurrent_builds=2)
    assert routing.fingerprint(a) != routing.fingerprint(b)


def test_new_churn_is_reflected_in_the_fingerprint() -> None:
    """A product starting to churn is exactly the kind of decision-relevant
    change that should end a repeat streak and prompt fresh deliberation --
    see churning_products' own comment on FactorySnapshot."""
    a = _snapshot(churning_products=[])
    b = _snapshot(churning_products=["prod-1"])
    assert routing.fingerprint(a) != routing.fingerprint(b)


# --- deterministic_route -------------------------------------------------------


def test_deterministic_route_follows_the_priority_order() -> None:
    snapshot = _snapshot(pending_by_engine={"discovery": 3, "finance": 1, "marketing": 2})
    decision = routing.deterministic_route(snapshot)
    assert decision is not None
    assert decision.target_engine is EngineName.FINANCE
    assert decision.next_action is NextAction.ROUTE


def test_deterministic_route_idles_when_nothing_is_queued() -> None:
    decision = routing.deterministic_route(_snapshot(pending_by_engine={}, pending_tasks_total=0))
    assert decision is not None
    assert decision.next_action is NextAction.IDLE_BACKLOG


def test_deterministic_route_is_ambiguous_for_an_unranked_engine() -> None:
    """A key the priority table doesn't recognise, with real queued work and no
    priority-table engine holding any of it, is exactly the case a future engine
    would hit before this table is updated for it."""
    snapshot = _snapshot(pending_by_engine={"some_future_engine": 3}, pending_tasks_total=3)
    assert routing.deterministic_route(snapshot) is None


def test_every_engine_is_ranked() -> None:
    """The rule, not another instance of it.

    An engine missing from PRIORITY is invisible to the deterministic scan: work
    queued only for it falls through, returns None, and turns every tick into an LLM
    call that routes wherever the model happens to look. It has now happened twice --
    SUPERVISOR, fixed with a test covering SUPERVISOR, and then DESIGN, which was
    created as a department eight days later and never added here.

    Writing the regression test for the *value* rather than the *rule* is what let the
    identical bug come back wearing a different name. This asserts the rule.
    """
    assert set(routing.PRIORITY) == set(EngineName), (
        "every EngineName must appear in PRIORITY; missing: "
        f"{sorted(e.value for e in set(EngineName) - set(routing.PRIORITY))}"
    )
    assert len(routing.PRIORITY) == len(set(routing.PRIORITY)), "no engine may be ranked twice"


def test_design_outranks_development_because_development_consumes_its_output() -> None:
    """A queued design task is a standard a builder is about to work without.

    Run 11: the supervisor commissioned guidelines and a build in the same decision,
    Development was serviced immediately and Design eighteen minutes later, so the
    page was built to the generic house style and then failed for not matching the
    standard that arrived afterwards.
    """
    snapshot = _snapshot(pending_by_engine={"design": 1, "development": 1}, pending_tasks_total=2)
    decision = routing.deterministic_route(snapshot)
    assert decision is not None
    assert decision.target_engine is EngineName.DESIGN


def test_design_only_pending_work_routes_deterministically() -> None:
    """The case that actually broke: design work and nothing else queued."""
    snapshot = _snapshot(pending_by_engine={"design": 3}, pending_tasks_total=3)
    decision = routing.deterministic_route(snapshot)
    assert decision is not None
    assert decision.target_engine is EngineName.DESIGN
    assert decision.next_action is NextAction.ROUTE


def test_supervisor_only_pending_work_routes_deterministically_not_ambiguous() -> None:
    """Regression test: SUPERVISOR was previously absent from PRIORITY, so
    maintenance-only pending work (a common, not rare, steady state) always
    returned None here -- forcing a real orchestration LLM call every tick until
    the model happened to route to SUPERVISOR."""
    snapshot = _snapshot(pending_by_engine={"supervisor": 2}, pending_tasks_total=2)
    decision = routing.deterministic_route(snapshot)
    assert decision is not None
    assert decision.target_engine is EngineName.SUPERVISOR
    assert decision.next_action is NextAction.ROUTE


def test_supervisor_ranks_last_behind_every_revenue_bearing_engine() -> None:
    snapshot = _snapshot(
        pending_by_engine={"supervisor": 5, "discovery": 1, "finance": 1, "development": 1}
    )
    decision = routing.deterministic_route(snapshot)
    assert decision is not None
    assert decision.target_engine is EngineName.FINANCE

    only_supervisor_and_discovery = _snapshot(pending_by_engine={"supervisor": 5, "discovery": 1})
    decision = routing.deterministic_route(only_supervisor_and_discovery)
    assert decision is not None
    assert decision.target_engine is EngineName.DISCOVERY


# --- deliberation_reason --------------------------------------------------------


def test_first_tick_always_consults_the_model(settings: Settings) -> None:
    memo = routing.RoutingMemo()
    assert routing.deliberation_reason(_snapshot(), memo, settings) == "first_tick"


def test_ambiguous_routing_always_consults_the_model(settings: Settings) -> None:
    memo = routing.RoutingMemo(has_deliberated=True)
    snapshot = _snapshot(pending_by_engine={"some_future_engine": 3}, pending_tasks_total=3)
    assert routing.deliberation_reason(snapshot, memo, settings) == "ambiguous_route"


def test_a_stall_consults_the_model(settings: Settings) -> None:
    memo = routing.RoutingMemo(has_deliberated=True, repeat_count=settings.supervisor_stall_ticks)
    assert routing.deliberation_reason(_snapshot(), memo, settings) == "stalled"


def test_the_cadence_consults_the_model_even_without_a_stall(settings: Settings) -> None:
    memo = routing.RoutingMemo(
        has_deliberated=True, ticks_since_model=settings.supervisor_deliberation_ticks
    )
    assert routing.deliberation_reason(_snapshot(), memo, settings) == "cadence"


def test_steady_state_needs_no_deliberation(settings: Settings) -> None:
    memo = routing.RoutingMemo(has_deliberated=True, repeat_count=0, ticks_since_model=1)
    assert routing.deliberation_reason(_snapshot(), memo, settings) is None


# --- observe_tick ---------------------------------------------------------------


def test_repeat_count_grows_only_while_the_fingerprint_is_unchanged() -> None:
    memo = routing.RoutingMemo()
    routing.observe_tick(memo, "a")
    assert memo.repeat_count == 0
    routing.observe_tick(memo, "a")
    assert memo.repeat_count == 1
    routing.observe_tick(memo, "b")
    assert memo.repeat_count == 0


def test_ticks_since_model_grows_every_tick_regardless_of_fingerprint() -> None:
    memo = routing.RoutingMemo()
    routing.observe_tick(memo, "a")
    routing.observe_tick(memo, "b")
    routing.observe_tick(memo, "a")
    assert memo.ticks_since_model == 3


# --- record_decision / replay: the safety-critical pair -------------------------


def test_a_replayable_decision_is_cached() -> None:
    memo = routing.RoutingMemo()
    decision = OrchestratorDecision(
        next_action=NextAction.ROUTE, target_engine=EngineName.FINANCE, rationale="r"
    )
    routing.record_decision(memo, decision, "fp1")
    assert memo.decision is decision
    assert memo.decision_fingerprint == "fp1"
    assert memo.has_deliberated is True
    assert memo.ticks_since_model == 0
    assert memo.repeat_count == 0


@pytest.mark.parametrize("action", [NextAction.SPAWN_AGENT, NextAction.ESCALATE])
def test_a_side_effecting_decision_is_never_cached(action: NextAction) -> None:
    memo = routing.RoutingMemo()
    decision = OrchestratorDecision(
        next_action=action, target_engine=EngineName.SUPERVISOR, rationale="r"
    )
    routing.record_decision(memo, decision, "fp1")
    assert memo.decision is None


def test_replay_requires_an_exact_fingerprint_match(ctx: FactoryContext) -> None:
    memo = routing.RoutingMemo()
    decision = OrchestratorDecision(
        next_action=NextAction.ROUTE, target_engine=EngineName.FINANCE, rationale="r"
    )
    routing.record_decision(memo, decision, "fp1")
    assert routing.replay(memo, "fp1", ctx) is decision
    assert routing.replay(memo, "fp2", ctx) is None


def test_replay_refuses_a_now_ineligible_engine(ctx: FactoryContext) -> None:
    memo = routing.RoutingMemo()
    decision = OrchestratorDecision(
        next_action=NextAction.ROUTE, target_engine=EngineName.FINANCE, rationale="r"
    )
    routing.record_decision(memo, decision, "fp1")
    ctx.enabled_engines.discard(EngineName.FINANCE)
    assert routing.replay(memo, "fp1", ctx) is None


# --- end-to-end through orchestrator.decide() -----------------------------------


async def _fake_snapshot(
    monkeypatch: pytest.MonkeyPatch, snapshot: routing.FactorySnapshot
) -> None:
    async def fake(session: Any, context: Any) -> routing.FactorySnapshot:
        return snapshot

    monkeypatch.setattr(orchestrator, "build_snapshot", fake)


async def test_a_repeated_identical_tick_does_not_re_call_the_model(
    ctx: FactoryContext, gemini_fake: FakeProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _fake_snapshot(monkeypatch, _snapshot(pending_by_engine={"finance": 1}))
    gemini_fake.register(
        OrchestratorDecision,
        OrchestratorDecision(
            next_action=NextAction.ROUTE, target_engine=EngineName.FINANCE, rationale="r"
        ),
    )

    first = await orchestrator.decide(None, ctx)  # type: ignore[arg-type]
    second = await orchestrator.decide(None, ctx)  # type: ignore[arg-type]

    assert first.target_engine is EngineName.FINANCE
    assert second.target_engine is EngineName.FINANCE
    assert len(gemini_fake.calls) == 1, "the second identical tick must not re-ask the model"


async def test_spawn_agent_is_never_replayed_on_a_repeated_tick(
    ctx: FactoryContext, gemini_fake: FakeProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The dangerous case: if SPAWN_AGENT were cached and replayed, an identical
    factory state would spawn a new agent every tick forever."""
    await _fake_snapshot(monkeypatch, _snapshot(pending_by_engine={"finance": 1}))
    gemini_fake.register(
        OrchestratorDecision,
        OrchestratorDecision(
            next_action=NextAction.SPAWN_AGENT,
            target_engine=EngineName.SUPERVISOR,
            agent_role="researcher",
            agent_objective="find out something",
            rationale="no fixed engine covers this",
        ),
    )

    first = await orchestrator.decide(None, ctx)  # type: ignore[arg-type]
    second = await orchestrator.decide(None, ctx)  # type: ignore[arg-type]

    assert first.next_action is NextAction.SPAWN_AGENT
    assert len(gemini_fake.calls) == 1, "the model must have been asked exactly once"
    # With nothing cached, the second identical tick falls through to the
    # deterministic priority scan rather than asking again or replaying SPAWN_AGENT.
    assert second.next_action is NextAction.ROUTE
    assert second.target_engine is EngineName.FINANCE


async def test_the_model_is_asked_again_once_the_stall_threshold_is_reached(
    ctx: FactoryContext, gemini_fake: FakeProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx.settings.supervisor_stall_ticks = 2
    await _fake_snapshot(monkeypatch, _snapshot(pending_by_engine={"finance": 1}))
    decision = OrchestratorDecision(
        next_action=NextAction.ROUTE, target_engine=EngineName.FINANCE, rationale="r"
    )
    gemini_fake.register(OrchestratorDecision, decision, decision)

    await orchestrator.decide(None, ctx)  # type: ignore[arg-type]  # first_tick
    await orchestrator.decide(None, ctx)  # type: ignore[arg-type]  # replay
    await orchestrator.decide(None, ctx)  # type: ignore[arg-type]  # repeat_count hits 2 -> stalled

    assert len(gemini_fake.calls) == 2


async def test_the_model_is_asked_again_on_the_deliberation_cadence(
    ctx: FactoryContext, gemini_fake: FakeProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx.settings.supervisor_deliberation_ticks = 2
    ctx.settings.supervisor_stall_ticks = 999  # isolate the cadence trigger
    await _fake_snapshot(monkeypatch, _snapshot(pending_by_engine={"finance": 1}))
    decision = OrchestratorDecision(
        next_action=NextAction.ROUTE, target_engine=EngineName.FINANCE, rationale="r"
    )
    gemini_fake.register(OrchestratorDecision, decision, decision)

    await orchestrator.decide(None, ctx)  # type: ignore[arg-type]  # first_tick, resets clocks
    await orchestrator.decide(None, ctx)  # type: ignore[arg-type]  # ticks_since_model=1, replay
    await orchestrator.decide(None, ctx)  # type: ignore[arg-type]  # ticks_since_model=2 -> cadence

    assert len(gemini_fake.calls) == 2


async def test_a_provider_outage_does_not_reset_the_cadence_clock(
    ctx: FactoryContext,
    gemini_fake: FakeProvider,
    anthropic_fake: FakeProvider,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed consultation is not a real consultation — it must not suppress the
    next attempt for a full cadence window."""
    from factory.llm.providers.base import Provider, ProviderUnavailable

    # ORCHESTRATION may fail over to Anthropic; taking it out of service means this
    # is a clean single-provider outage rather than needing failures queued on both.
    anthropic_fake.available_flag = False
    ctx.settings.supervisor_stall_ticks = 999
    ctx.settings.supervisor_deliberation_ticks = 999
    await _fake_snapshot(monkeypatch, _snapshot(pending_by_engine={"finance": 1}))
    for _ in range(3):
        gemini_fake.register(
            OrchestratorDecision, ProviderUnavailable(Provider.GEMINI, "503", status=503)
        )

    decision = await orchestrator.decide(None, ctx)  # type: ignore[arg-type]

    # The provider failed, so `_fallback` answered deterministically, but nothing
    # about that success should be cached as though the model had actually spoken.
    assert decision.target_engine is EngineName.FINANCE
    assert ctx.routing.has_deliberated is False
    assert ctx.routing.decision is None


# --- the snapshot legend: a number the model cannot interpret is a bug ---------


def test_the_prompt_explains_that_an_empty_reinvestment_pool_is_normal() -> None:
    """Observed live, six times: the Orchestrator was handed a bare JSON dump
    containing `"spend_limit_cents":0`, with the system prompt telling it that
    escalation is "permitted only for a missing credential". It drew the obvious
    inference -- zero budget must be a broken budget credential -- and escalated
    daily, claiming the factory was "blocked", while 15 tasks and 60 model calls
    completed in the same six hours.

    The number was accessible and its meaning was not. That is the instrumentation
    principle exactly: access without an instrument produces confident flailing,
    and no amount of "be more careful" in the prompt fixes a missing legend.
    """
    prompt = orchestrator._system_prompt()
    assert "spend_limit_cents" in prompt
    assert "reinvestment pool" in prompt
    assert "Zero is the normal" in prompt


def test_every_money_field_in_the_snapshot_is_explained_to_the_model() -> None:
    """The drift guard. A future `*_cents` field added to the snapshot reaches the
    model as one more unlabelled integer in a JSON blob, which is how this bug
    happened the first time."""
    prompt = orchestrator._system_prompt()
    money_fields = [f for f in routing.FactorySnapshot.__annotations__ if f.endswith("_cents")]
    assert money_fields, "expected at least one money field to guard"
    undocumented = [f for f in money_fields if f not in prompt]
    assert not undocumented, f"snapshot money fields not explained to the model: {undocumented}"
