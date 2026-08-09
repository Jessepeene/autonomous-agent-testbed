"""Supervisor graph wiring.

These assertions exist because the graph is only exercised end-to-end with a
database attached. A node-name or edge mistake would otherwise surface for the first
time on the VPS, hours into an unattended run.
"""

from __future__ import annotations

import pytest

from factory.settings import EngineName
from factory.state.schemas import NextAction
from factory.supervisor.context import FactoryContext
from factory.supervisor.graph import (
    ENGINE_NODES,
    SupervisorState,
    _route,
    build_graph,
    engine_node_name,
)


def test_graph_compiles_with_every_engine_node(ctx: FactoryContext) -> None:
    full_ctx = ctx
    full_ctx.enabled_engines = set(EngineName)
    graph = build_graph(full_ctx, checkpointer=None)
    nodes = set(graph.get_graph().nodes)

    assert {"orchestrate", "backlog", "escalate", "spawn_agent", "budget_paused"} <= nodes
    for engine in ENGINE_NODES:
        assert engine_node_name(engine) in nodes


@pytest.mark.parametrize("engine", list(EngineName))
def test_node_names_avoid_langgraph_reserved_characters(engine: EngineName) -> None:
    """LangGraph rejects ':' in node names — a compile-time error, silent until boot."""
    assert ":" not in engine_node_name(engine)


def test_route_sends_hold_and_idle_to_backlog(ctx: FactoryContext) -> None:
    for action in (NextAction.HOLD_DISCOVERY, NextAction.IDLE_BACKLOG):
        state: SupervisorState = {"action": action.value, "engine": "discovery"}
        assert _route(state, ctx=ctx) == "backlog"


def test_route_sends_escalate_to_escalate(ctx: FactoryContext) -> None:
    state: SupervisorState = {"action": NextAction.ESCALATE.value, "engine": "supervisor"}
    assert _route(state, ctx=ctx) == "escalate"


def test_route_reaches_an_enabled_engine(ctx: FactoryContext) -> None:
    state: SupervisorState = {"action": NextAction.ROUTE.value, "engine": "discovery"}
    assert _route(state, ctx=ctx) == engine_node_name(EngineName.DISCOVERY)


def test_route_sends_a_supervisor_target_to_the_supervisor_engine_node(
    ctx: FactoryContext,
) -> None:
    """Regression test: `ROUTE` to `EngineName.SUPERVISOR` must reach the maintenance
    runner, not silently redirect to `backlog` (which only *enqueues* maintenance
    tasks, it never claims or runs them). The live orchestrator routes here often --
    its rationale text for this decision is indistinguishable from IDLE_BACKLOG's
    ("process interrupts", "run maintenance") -- so every `engine=SUPERVISOR`
    maintenance task (the whole learning loop, prune-memory, triage-escalated-tasks)
    sat at status=pending forever before this was wired up correctly.
    """
    state: SupervisorState = {"action": NextAction.ROUTE.value, "engine": "supervisor"}
    assert _route(state, ctx=ctx) == engine_node_name(EngineName.SUPERVISOR)


def test_route_diverts_a_disabled_engine_to_backlog(ctx: FactoryContext) -> None:
    """Development is disabled in the fixture (no Docker), so routing must not stall."""
    assert not ctx.is_enabled(EngineName.DEVELOPMENT)
    state: SupervisorState = {"action": NextAction.ROUTE.value, "engine": "development"}
    assert _route(state, ctx=ctx) == "backlog"


def test_route_survives_an_unknown_engine_name(ctx: FactoryContext) -> None:
    """A hallucinated engine falls back to useful work instead of raising."""
    state: SupervisorState = {"action": NextAction.ROUTE.value, "engine": "marketing_ops"}
    assert _route(state, ctx=ctx) == "backlog"


def test_route_sends_budget_paused_to_its_own_node(ctx: FactoryContext) -> None:
    """The financial circuit breaker must reach a true no-op — never backlog, which
    also spawns LLM-calling maintenance agents."""
    state: SupervisorState = {"action": NextAction.BUDGET_PAUSED.value, "engine": "supervisor"}
    assert _route(state, ctx=ctx) == "budget_paused"


def test_route_sends_spawn_agent_to_the_spawn_node(ctx: FactoryContext) -> None:
    state: SupervisorState = {
        "action": NextAction.SPAWN_AGENT.value,
        "engine": "supervisor",
        "agent_role": "analyst",
        "agent_objective": "figure out why costs spiked",
    }
    assert _route(state, ctx=ctx) == "spawn_agent"


def test_api_exposes_the_expected_surface() -> None:
    from factory.api.app import app

    paths = {r.path for r in app.routes if hasattr(r, "path")}
    assert {"/health", "/ready", "/metrics", "/webhooks/stripe", "/u/{token}"} <= paths
