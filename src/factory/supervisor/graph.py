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
from factory.engines.compliance.graph import (
    VALIDATION_CAPACITY_RECHECK_SECONDS,
    ValidationCapacityExceeded,
)
from factory.engines.development.repair import RepairBlocked
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

#: How long a repair deferred for an outage or an exhausted budget waits before it is
#: retried. Short relative to the campaign/validation rechecks above: the conditions
#: this covers (a provider outage, the daily spend window advancing) clear on the
#: order of minutes, and the fallback if they do not is simply another cheap defer.
REPAIR_RECHECK_SECONDS = 600


def engine_node_name(engine: EngineName) -> str:
    """LangGraph reserves ':' in node names, so engine nodes use an underscore."""
    return f"engine_{engine.value}"


ENGINE_NODES: tuple[EngineName, ...] = (
    EngineName.DISCOVERY,
    EngineName.COMPLIANCE,
    EngineName.DEVELOPMENT,
    EngineName.MARKETING,
    EngineName.FINANCE,
    # SUPERVISOR must be a real engine node, not just a backlog-enqueue target:
    # `backlog.enqueue_maintenance()` only creates maintenance tasks, it never claims
    # or runs them. Without this, every maintenance item tagged `engine=SUPERVISOR`
    # (triage-escalated-tasks, the whole learning loop, prune-memory, ...) sits at
    # `status=pending` forever the moment the orchestrator ever says `ROUTE` to
    # supervisor instead of `IDLE_BACKLOG` -- which it does routinely in practice,
    # since both read the same "process interrupts / do maintenance" situation.
    EngineName.SUPERVISOR,
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
    if not ctx.is_enabled(engine):
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
                subject=f"{engine.value} task {task_id} refused by safety classifier",
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
                    subject=(
                        f"{engine.value} task {task_id} hit a host/environment "
                        "failure, not a code bug"
                    ),
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
    except ValidationCapacityExceeded as exc:
        # Not a failure: deferred without touching attempt_count, mirroring
        # CampaignApprovalPending exactly. The retry re-enters compliance/graph.py's
        # run(), which short-circuits past the assessor call for an already-approved
        # idea -- capacity freeing up costs no extra LLM spend to detect.
        recheck = timedelta(seconds=VALIDATION_CAPACITY_RECHECK_SECONDS)
        async with session_scope(ctx.settings) as session:
            await repository.defer_task(session, task_id, not_before=datetime.now(UTC) + recheck)
        log.info("engine.awaiting_validation_capacity", engine=str(engine), task_id=task_id)
        return {"task_id": task_id, "note": str(exc)[:500], "error": None}
    except RepairBlocked as exc:
        # A surgical repair that never got a fair attempt -- provider outage, or its
        # step/time/cost budget ran out before it could finish. Deferred rather than
        # failed for the same reason as the cases above: attempt_count must not move,
        # because none of those say anything about whether the product is fixable.
        # Counting them would let three consecutive outages permanently escalate a
        # product whose repair was never actually tried. The workspace has already
        # been rolled back by the caller.
        async with session_scope(ctx.settings) as session:
            await repository.defer_task(
                session,
                task_id,
                not_before=datetime.now(UTC) + timedelta(seconds=REPAIR_RECHECK_SECONDS),
            )
        log.info(
            "engine.repair_blocked", engine=str(engine), task_id=task_id, detail=str(exc)[:200]
        )
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
                    subject=f"{engine.value} task {task_id} exhausted its retries",
                    detail=(
                        f"Task {task_id} ({task_kind}) failed "
                        f"{ctx.settings.max_task_attempts} times. Last error: {exc!r}\n\n"
                        "If the underlying cause has been fixed, reply retry to give "
                        "this task one more attempt."
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
    # The subject carries the rationale (truncated), not a fixed string: interrupts
    # dedupe on (reason, subject, status), so a second, genuinely different
    # escalation with the same fixed subject would have silently collided with an
    # already-open, unrelated one and never reached Telegram at all -- the same bug
    # class as the generic per-engine task-failure subjects above.
    rationale = state.get("rationale", "no rationale supplied")
    async with session_scope(ctx.settings) as session:
        await interrupts.request(
            session,
            reason=InterruptReason.LEGAL_UNCERTAINTY,
            subject=f"Supervisor requested human review: {rationale[:120]}",
            detail=rationale,
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
