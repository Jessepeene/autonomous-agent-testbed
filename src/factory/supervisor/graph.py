"""The Supervisor graph.

One orchestrator node routes to one node per engine. Engine nodes are separate — not
a single generic dispatcher — so a checkpoint trace shows *which* engine ran, which
is the difference between a debuggable overnight run and a log full of "dispatch".

Each engine node claims a leased task, runs it, and records the outcome. Failures
retry up to ``max_task_attempts`` and then escalate; nothing is retried forever and
nothing fails silently.
"""

from __future__ import annotations

import re
from collections.abc import Hashable
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

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
from factory.llm.providers.base import ProviderCircuitOpen, ProviderRefusal, ProviderUnavailable
from factory.logging import get_logger
from factory.sandbox.runner import DevOpsError, SandboxUnavailable
from factory.settings import EngineName
from factory.state import repository
from factory.state.db import session_scope
from factory.state.orm import Idea, Product, Task
from factory.state.schemas import InterruptReason, NextAction
from factory.supervisor import backlog, orchestrator
from factory.supervisor.context import FactoryContext

log = get_logger(__name__)

#: Every id in this codebase is `uuid.uuid4().hex` (`state/orm.py::new_id`) -- 32
#: lowercase hex characters, no dashes. Used by `_spawn_agent`'s premise check
#: below to spot an id-shaped token in a free-text objective worth verifying.
_ID_TOKEN = re.compile(r"\b[0-9a-f]{32}\b")

#: How long a repair deferred for an outage or an exhausted budget waits before it is
#: retried. Short relative to the campaign/validation rechecks above: the conditions
#: this covers (a provider outage, the daily spend window advancing) clear on the
#: order of minutes, and the fallback if they do not is simply another cheap defer.
REPAIR_RECHECK_SECONDS = 600

#: How long a task blocked by a tripped provider waits. Longer than the repair
#: recheck above because the breaker only trips on a *sustained* outage -- one that
#: has already failed every call for half an hour, and in practice needs a human to
#: top up a balance or replace a key. Rechecking hard would just refill the queue
#: with work that still cannot run; `scheduler._refresh_provider_health` is what
#: actually notices recovery, every tick, for free.
PROVIDER_OUTAGE_RECHECK = 900


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
        task_product_id = task.product_id
        # The parent link. A task enqueued from inside another task's handler
        # carries its origin here, which is what turns a pile of rows into a
        # readable chain -- review found a defect, which queued a patch, which
        # exhausted its budget, which escalated.
        caused_by = str(task.payload.get("caused_by_task_id") or "") or None
        await repository.record_task_event(
            session,
            event="engine.start",
            task_id=task_id,
            product_id=task_product_id,
            engine=engine,
            detail={"kind": task_kind, "attempt": task.attempt_count},
            caused_by_task_id=caused_by,
        )

    log.info("engine.start", engine=str(engine), task_id=task_id, kind=task_kind)
    try:
        runner = get_runner(engine)
        async with session_scope(ctx.settings) as session:
            claimed = await session.get(Task, task_id)
            assert claimed is not None
            note = await runner(ctx, session, claimed)
            await repository.complete_task(session, task_id)
            await repository.record_task_event(
                session,
                event="engine.done",
                task_id=task_id,
                product_id=task_product_id,
                engine=engine,
                detail={"kind": task_kind, "note": note[:500]},
            )
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
                    f"(category={exc.category}). Not retried automatically — a human "
                    "needs to look at this."
                ),
                headline="\U0001f6ab Safety classifier declined a task",
                reply_hint=(
                    'Include "retry" in your reply to give this task exactly one more '
                    "attempt as-is, or reply anything else to just close this out."
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
                explanation = await interrupts.plain_language_error(
                    ctx,
                    str(exc)[:2000],
                    context=(
                        f"A {engine.value} task ({task_kind}) failed "
                        f"{ctx.settings.max_task_attempts} times with what looks like an "
                        "infrastructure problem (sandbox, host filesystem, or network), "
                        "not a bug in the generated application code."
                    ),
                    task_id=task_id,
                )
                await interrupts.request(
                    session,
                    reason=InterruptReason.MISSING_CREDENTIAL,
                    subject=(
                        f"{engine.value} task {task_id} hit a host/environment "
                        "failure, not a code bug"
                    ),
                    detail=explanation,
                    headline="\U0001f5a5️ Infrastructure problem, not a code bug",
                    reply_hint="Reply retry once you believe the underlying issue is fixed.",
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
                reason="marketing_frozen",
            )
        log.info("engine.marketing_frozen", engine=str(engine), task_id=task_id)
        return {"task_id": task_id, "note": f"marketing frozen: {exc}"[:500], "error": None}
    except CampaignApprovalPending as exc:
        # Not a failure: deferred without touching `attempt_count`, so an approval
        # that takes days does not exhaust the task's retries or escalate it.
        async with session_scope(ctx.settings) as session:
            await repository.defer_task(
                session, task_id, not_before=datetime.now(UTC) + timedelta(seconds=RECHECK_SECONDS),
                reason="awaiting_campaign_approval",
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
            await repository.defer_task(
                session,
                task_id,
                not_before=datetime.now(UTC) + recheck,
                reason="validation_capacity_exceeded",
            )
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
                reason="repair_blocked",
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
                    "sandbox and will not run it on the host."
                ),
                headline="\U0001f433 Docker daemon unavailable",
                reply_hint="No reply needed — install Docker and restart the factory.",
                needs_reply=False,
                task_id=task_id,
            )
        return {"task_id": task_id, "error": "sandbox", "note": str(exc)[:500]}
    except ProviderCircuitOpen as exc:
        # Caught before ProviderUnavailable below, and handled completely
        # differently: no call was made, nothing was spent, and the human has
        # already been told once by `scheduler._refresh_provider_health`. So this
        # is a standing state to wait out, not a failure to escalate -- deferred
        # without touching `attempt_count`, exactly like CampaignApprovalPending
        # and MarketingFrozen. The task resumes on its own when the breaker clears.
        async with session_scope(ctx.settings) as session:
            await repository.defer_task(
                session,
                task_id,
                not_before=datetime.now(UTC) + timedelta(seconds=PROVIDER_OUTAGE_RECHECK),
                reason=f"provider_outage:{exc.provider.value}",
            )
        log.info("engine.provider_circuit_open", engine=str(engine), task_id=task_id)
        return {"task_id": task_id, "note": str(exc)[:500], "error": None}
    except ProviderUnavailable as exc:
        # A provider itself rejecting requests (quota/credit exhaustion, a
        # permanent 4xx, a sustained 5xx) is never a legal or content question --
        # and unlike the generic handler below, its explanation is already fully
        # structured (provider, status, message), so it never needs
        # `plain_language_error`'s own LLM call, which would otherwise fail for
        # the exact same reason the moment every provider is down at once.
        #
        # The subject deliberately excludes `task_id`: a fresh task hits this
        # same failure on every tick while the outage persists, and folding the
        # task_id into the subject (as the generic handler below does) defeats
        # `interrupts.request`'s duplicate-open-subject collapse -- see
        # `state/repository.py::open_interrupt` and PROVIDER_OUTAGE's own
        # docstring in `state/schemas.py` for the 40-interrupt incident this
        # fixes.
        async with session_scope(ctx.settings) as session:
            exhausted = await repository.fail_task(session, task_id, f"provider unavailable: {exc}")
            if exhausted:
                status_part = f" (HTTP {exc.status})" if exc.status is not None else ""
                await interrupts.request(
                    session,
                    reason=InterruptReason.PROVIDER_OUTAGE,
                    subject=f"{exc.provider.value} is rejecting requests{status_part}",
                    detail=(
                        f"{engine.value} task {task_id} ({task_kind}) failed because "
                        f"{exc.provider.value} rejected the request{status_part}: {exc}\n\n"
                        "This is a provider billing/infrastructure problem, not a legal "
                        "or content question -- check that provider's account credits "
                        "or billing status."
                    ),
                    headline="\U0001f50c Provider is rejecting requests",
                    reply_hint="Reply retry once the provider account is fixed.",
                    task_id=task_id,
                )
        log.warning(
            "engine.provider_unavailable",
            engine=str(engine),
            task_id=task_id,
            provider=exc.provider.value,
        )
        return {"task_id": task_id, "error": "provider_unavailable", "note": str(exc)[:500]}
    except Exception as exc:
        log.exception("engine.failed", engine=str(engine), task_id=task_id)
        async with session_scope(ctx.settings) as session:
            exhausted = await repository.fail_task(session, task_id, repr(exc))
            if exhausted:
                explanation = await interrupts.plain_language_error(
                    ctx,
                    repr(exc)[:2000],
                    context=(
                        f"A {engine.value} task ({task_kind}) failed "
                        f"{ctx.settings.max_task_attempts} times and exhausted its retries."
                    ),
                    task_id=task_id,
                )
                await interrupts.request(
                    session,
                    reason=InterruptReason.TASK_EXHAUSTED,
                    subject=f"{engine.value} task {task_id} exhausted its retries",
                    detail=explanation,
                    headline="⚠️ Task failed repeatedly",
                    reply_hint="Reply retry once you believe the underlying cause is addressed.",
                    task_id=task_id,
                )
        return {"task_id": task_id, "error": repr(exc)[:500], "note": "failed"}

    log.info("engine.done", engine=str(engine), task_id=task_id)
    return {"task_id": task_id, "note": note, "error": None}


async def _backlog(state: SupervisorState, *, ctx: FactoryContext) -> SupervisorState:
    async with session_scope(ctx.settings) as session:
        created = await backlog.enqueue_maintenance(session, enabled_engines=ctx.enabled_engines)
    return {"note": f"backlog: {', '.join(created) if created else 'already queued'}"}


async def _referenced_ids_exist(session: AsyncSession, ids: set[str]) -> bool:
    """Tier 3 finding #15, system-wide verification plan: the same category of
    check `orchestrator.py`'s ROUTE path already applies to a provably empty
    queue, extended to SPAWN_AGENT -- a `product_id`/`task_id`/`idea_id` the
    objective names must actually exist, not just be well-formed. Checked
    across all three tables since a bare id token in free text doesn't say
    which kind it is."""
    if not ids:
        return True
    found: set[str] = set()
    for model in (Product, Task, Idea):
        rows = await session.execute(select(model.id).where(model.id.in_(ids)))
        found.update(rows.scalars().all())
    return ids <= found


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
        referenced_ids = set(_ID_TOKEN.findall(objective))
        if not await _referenced_ids_exist(session, referenced_ids):
            # Same posture as orchestrator.py's route-to-empty-queue guard: an
            # unverifiable premise is a bad suggestion, not a reason to fail the
            # tick -- fall back to backlog work instead of spawning an agent
            # to investigate a product/task that was never real to begin with.
            log.warning(
                "supervisor.spawn_agent_fabricated_referent",
                role=role.name.value,
                objective=objective[:200],
            )
            return await _backlog(state, ctx=ctx)
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
    # The subject is keyed on the DAY, not on the rationale.
    #
    # It used to embed `rationale[:120]` verbatim, reasoning that a fixed subject
    # would make two genuinely different escalations collide and silently hide the
    # second. True in principle -- but `rationale` is free text an LLM writes
    # fresh every tick, so it is never byte-identical twice even when the
    # situation is unchanged, and `open_interrupt`'s dedup therefore never
    # matched. Observed live: 45 open interrupts in three hours, all reporting the
    # same stall in slightly different words ("Factory is stuck with 16 pending
    # discovery tasks...", "Factory is stuck in a loop with 16 pending...", ...).
    #
    # A day bucket is the right granularity for what this path actually is: an
    # informational, `needs_reply=False` "someone should look at the factory"
    # note. One a day is a signal; forty-five is noise that buries the genuine
    # escalations next to it. Same shape as `supervisor/audit.py`'s
    # "Business-health check, {date}". The varying rationale still reaches the
    # human in full -- it moves to `detail`, which is not part of the dedup key.
    #
    # Tier 3 finding #14 still stands and is still accepted: `rationale` is the
    # orchestrator's own account with nothing to cross-check it against. There is
    # no task/idea/lead here to render structured facts from, so a fix would have
    # no evidence to add.
    rationale = state.get("rationale", "no rationale supplied")
    day = datetime.now(UTC).date().isoformat()
    async with session_scope(ctx.settings) as session:
        await interrupts.request(
            session,
            reason=InterruptReason.TASK_EXHAUSTED,
            subject=f"Supervisor requested human review ({day})",
            detail=rationale,
            headline="\U0001f9ed Orchestrator requested human review",
            reply_hint=(
                "No fixed action here — this is a general review request, not tied "
                "to a specific task. Replying just closes it out."
            ),
            needs_reply=False,
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
