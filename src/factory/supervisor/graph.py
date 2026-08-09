"""The Supervisor graph.

One orchestrator node routes to one node per engine. Engine nodes are separate — not
a single generic dispatcher — so a checkpoint trace shows *which* engine ran, which
is the difference between a debuggable overnight run and a log full of "dispatch".

Each engine node claims a leased task, runs it, and records the outcome. Failures
retry up to ``max_task_attempts`` and then escalate; nothing is retried forever and
nothing fails silently.
"""

from __future__ import annotations

from collections.abc import Hashable
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from factory.engines.base import get_runner
from factory.engines.marketing.campaign import RECHECK_SECONDS, CampaignApprovalPending
from factory.engines.marketing.deliverability import RECHECK_SECONDS as DELIVERABILITY_RECHECK
from factory.engines.marketing.deliverability import MarketingFrozen
from factory.hitl import interrupts
from factory.llm.providers.base import ProviderRefusal
from factory.logging import get_logger
from factory.sandbox.runner import DevOpsError, SandboxUnavailable
from factory.settings import EngineName
from factory.state import repository
from factory.state.db import session_scope
from factory.state.orm import Task
from factory.state.schemas import InterruptReason, NextAction
from factory.supervisor import backlog, orchestrator
from factory.supervisor.context import FactoryContext

log = get_logger(__name__)


def engine_node_name(engine: EngineName) -> str:
    """LangGraph reserves ':' in node names, so engine nodes use an underscore."""
    return f"engine_{engine.value}"


ENGINE_NODES: tuple[EngineName, ...] = (
    EngineName.DISCOVERY,
    EngineName.COMPLIANCE,
    EngineName.DEVELOPMENT,
    EngineName.MARKETING,
    EngineName.FINANCE,
)


class SupervisorState(TypedDict, total=False):
    tick: int
    action: str
    engine: str
    rationale: str
    agent_role: str | None
    agent_objective: str | None
    task_id: str | None
    note: str
    error: str | None


async def _orchestrate(state: SupervisorState, *, ctx: FactoryContext) -> SupervisorState:
    async with session_scope(ctx.settings) as session:
        decision = await orchestrator.decide(session, ctx)
    log.info(
        "supervisor.decision",
        tick=state.get("tick", 0),
        action=str(decision.next_action),
        engine=str(decision.target_engine),
        rationale=decision.rationale,
    )
    return {
        "action": decision.next_action.value,
        "engine": decision.target_engine.value,
        "rationale": decision.rationale,
        "agent_role": decision.agent_role,
        "agent_objective": decision.agent_objective,
    }


def _route(state: SupervisorState, *, ctx: FactoryContext) -> str:
    action = state.get("action", NextAction.IDLE_BACKLOG.value)
    if action == NextAction.BUDGET_PAUSED.value:
        return "budget_paused"
    if action in (NextAction.IDLE_BACKLOG.value, NextAction.HOLD_DISCOVERY.value):
        return "backlog"
    if action == NextAction.ESCALATE.value:
        return "escalate"
    if action == NextAction.SPAWN_AGENT.value:
        return "spawn_agent"

    engine_value = state.get("engine", EngineName.SUPERVISOR.value)
    try:
        engine = EngineName(engine_value)
    except ValueError:
        log.warning("supervisor.unknown_engine", engine=engine_value)
        return "backlog"
    if engine is EngineName.SUPERVISOR or not ctx.is_enabled(engine):
        return "backlog"
    return engine_node_name(engine)


async def _run_engine(
    state: SupervisorState, *, ctx: FactoryContext, engine: EngineName
) -> SupervisorState:
    async with session_scope(ctx.settings) as session:
        task = await repository.claim_next_task(
            session, worker_id=ctx.worker_id, engines=[engine], settings=ctx.settings
        )
        if task is None:
            return {"note": f"{engine.value}: nothing ready", "task_id": None}
        task_id = task.id
        task_kind = task.kind

    log.info("engine.start", engine=str(engine), task_id=task_id, kind=task_kind)
    try:
        runner = get_runner(engine)
        async with session_scope(ctx.settings) as session:
            claimed = await session.get(Task, task_id)
            assert claimed is not None
            note = await runner(ctx, session, claimed)
            await repository.complete_task(session, task_id)
    except ProviderRefusal as exc:
        # A model declined on policy grounds. Retrying is pointless and switching
        # vendors to get a different answer is not something we do, so this is the
        # one model failure that goes straight to a human.
        async with session_scope(ctx.settings) as session:
            await repository.fail_task(session, task_id, f"provider refusal: {exc}")
            await interrupts.request(
                session,
                reason=InterruptReason.LEGAL_UNCERTAINTY,
                subject=f"{engine.value} task refused by safety classifier",
                detail=(
                    f"Task {task_id} ({task_kind}) was declined "
                    f"(category={exc.category}). A human decision is needed on whether "
                    "this work should proceed."
                ),
                task_id=task_id,
            )
        return {"task_id": task_id, "error": "refusal", "note": str(exc)[:500]}
    except DevOpsError as exc:
        # Rewriting the generated code cannot fix a host/environment problem, so this
        # never touches `heal_attempts`/`MAX_HEAL_CYCLES` — those are `_heal()`'s
        # counters and `_heal()` is never called here. Retried via the ordinary task
        # attempt/backoff mechanism first (a transient network blip during `pip
        # install` can genuinely self-resolve); only once *that* is exhausted does
        # this escalate, with a message that tells the human this is infrastructure,
        # not application logic, before they spend time reading a diff that was
        # never the problem.
        async with session_scope(ctx.settings) as session:
            exhausted = await repository.fail_task(session, task_id, f"devops error: {exc}")
            if exhausted:
                await interrupts.request(
                    session,
                    reason=InterruptReason.MISSING_CREDENTIAL,
                    subject=f"{engine.value} hit a host/environment failure, not a code bug",
                    detail=(
                        f"Task {task_id} ({task_kind}) failed "
                        f"{ctx.settings.max_task_attempts} times with what looks like an "
                        f"infrastructure problem rather than a bug in generated code:\n\n"
                        f"{str(exc)[:2000]}\n\n"
                        "Rewriting the generated code will not fix this — check the "
                        "sandbox image, host filesystem permissions, or network "
                        "reachability, then retry."
                    ),
                    task_id=task_id,
                )
        log.warning("engine.devops_error", engine=str(engine), task_id=task_id, exhausted=exhausted)
        return {"task_id": task_id, "error": "devops", "note": str(exc)[:500]}
    except MarketingFrozen as exc:
        # A standing state, not an error this task caused — deferred exactly like
        # CampaignApprovalPending, never touching attempt_count.
        async with session_scope(ctx.settings) as session:
            await repository.defer_task(
                session,
                task_id,
                not_before=datetime.now(UTC) + timedelta(seconds=DELIVERABILITY_RECHECK),
            )
        log.info("engine.marketing_frozen", engine=str(engine), task_id=task_id)
        return {"task_id": task_id, "note": f"marketing frozen: {exc}"[:500], "error": None}
    except CampaignApprovalPending as exc:
        # Not a failure: deferred without touching `attempt_count`, so an approval
        # that takes days does not exhaust the task's retries or escalate it.
        async with session_scope(ctx.settings) as session:
            await repository.defer_task(
                session, task_id, not_before=datetime.now(UTC) + timedelta(seconds=RECHECK_SECONDS)
            )
        log.info("engine.awaiting_campaign_approval", engine=str(engine), task_id=task_id)
        return {"task_id": task_id, "note": str(exc)[:500], "error": None}
    except SandboxUnavailable as exc:
        async with session_scope(ctx.settings) as session:
            await repository.fail_task(session, task_id, f"sandbox unavailable: {exc}")
            await interrupts.request(
                session,
                reason=InterruptReason.MISSING_CREDENTIAL,
                subject="Docker daemon unavailable",
                detail=(
                    "The Development Engine cannot execute generated code without a "
                    "sandbox and will not run it on the host. Install Docker and "
                    "restart the factory."
                ),
                task_id=task_id,
            )
        return {"task_id": task_id, "error": "sandbox", "note": str(exc)[:500]}
    except Exception as exc:
        log.exception("engine.failed", engine=str(engine), task_id=task_id)
        async with session_scope(ctx.settings) as session:
            exhausted = await repository.fail_task(session, task_id, repr(exc))
            if exhausted:
                await interrupts.request(
                    session,
                    reason=InterruptReason.LEGAL_UNCERTAINTY,
                    subject=f"{engine.value} task exhausted its retries",
                    detail=(
                        f"Task {task_id} ({task_kind}) failed "
                        f"{ctx.settings.max_task_attempts} times. Last error: {exc!r}"
                    ),
                    task_id=task_id,
                )
        return {"task_id": task_id, "error": repr(exc)[:500], "note": "failed"}

    log.info("engine.done", engine=str(engine), task_id=task_id)
    return {"task_id": task_id, "note": note, "error": None}


async def _backlog(state: SupervisorState, *, ctx: FactoryContext) -> SupervisorState:
    async with session_scope(ctx.settings) as session:
        created = await backlog.enqueue_maintenance(session, enabled_engines=ctx.enabled_engines)
    return {"note": f"backlog: {', '.join(created) if created else 'already queued'}"}


async def _spawn_agent(state: SupervisorState, *, ctx: FactoryContext) -> SupervisorState:
    """Run an ephemeral agent for work no fixed engine covers.

    An unknown or impermissible role falls back to backlog work rather than raising:
    the Orchestrator proposing a role that does not exist is a bad suggestion, not a
    reason to lose the tick.
    """
    from factory.agents.spawner import RoleNotPermitted, SpawnRequest, resolve_role, spawn

    role_name = state.get("agent_role") or ""
    objective = state.get("agent_objective") or state.get("rationale") or ""
    try:
        role = resolve_role(role_name)
    except RoleNotPermitted:
        log.warning("supervisor.unknown_role", role=role_name)
        return await _backlog(state, ctx=ctx)

    async with session_scope(ctx.settings) as session:
        result = await spawn(
            ctx,
            session,
            SpawnRequest(role=role.name, objective=objective, engine=EngineName.SUPERVISOR),
        )
    return {"note": f"agent/{role.name.value}: {result.summary()}"}


async def _budget_paused(state: SupervisorState, *, ctx: FactoryContext) -> SupervisorState:
    """Terminal, no-op node: the tick does nothing at all. Not even backlog
    maintenance, which also spawns LLM-calling agents — a tripped breaker means
    *every* generative task stops, not just the ones on the revenue path. Escalation
    to Telegram already happened in `orchestrator._check_budget`; this node only
    records that the tick was skipped."""
    return {"note": "budget circuit breaker tripped: tick skipped"}


async def _escalate(state: SupervisorState, *, ctx: FactoryContext) -> SupervisorState:
    async with session_scope(ctx.settings) as session:
        await interrupts.request(
            session,
            reason=InterruptReason.LEGAL_UNCERTAINTY,
            subject="Supervisor requested human review",
            detail=state.get("rationale", "no rationale supplied"),
        )
    return {"note": "escalated"}


def build_graph(ctx: FactoryContext, checkpointer: Any | None = None) -> Any:
    graph: StateGraph[SupervisorState] = StateGraph(SupervisorState)
    graph.add_node("orchestrate", partial(_orchestrate, ctx=ctx))
    graph.add_node("backlog", partial(_backlog, ctx=ctx))
    graph.add_node("escalate", partial(_escalate, ctx=ctx))
    graph.add_node("spawn_agent", partial(_spawn_agent, ctx=ctx))
    graph.add_node("budget_paused", partial(_budget_paused, ctx=ctx))

    path_map: dict[Hashable, str] = {
        "backlog": "backlog",
        "escalate": "escalate",
        "spawn_agent": "spawn_agent",
        "budget_paused": "budget_paused",
    }
    for engine in ENGINE_NODES:
        node_name = engine_node_name(engine)
        graph.add_node(node_name, partial(_run_engine, ctx=ctx, engine=engine))
        graph.add_edge(node_name, END)
        path_map[node_name] = node_name

    graph.add_edge(START, "orchestrate")
    graph.add_conditional_edges("orchestrate", partial(_route, ctx=ctx), path_map)
    graph.add_edge("backlog", END)
    graph.add_edge("escalate", END)
    graph.add_edge("spawn_agent", END)
    graph.add_edge("budget_paused", END)

    return graph.compile(checkpointer=checkpointer)
