"""Ephemeral sub-agent creation.

The Orchestrator calls `spawn()` when a task needs capability no fixed engine has —
an open research question, a stubborn build failure, a health check on a live URL.
The agent exists for one objective and is gone when it returns; there is no
long-lived agent registry to drift out of sync with the code.

What the caller may vary: the role, the objective, the context handed in. What it may
*not* vary: the tool set (fixed by the role), the step ceiling, and whether the agent
can mutate anything. Those are the containment, so they are not parameters an LLM
gets to choose.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from factory.agents.roles import CALL_SITE_SCOPED_ROLES, AgentRole, RoleName, get_role
from factory.agents.runtime import AgentResult, AgentRuntime
from factory.llm.providers.base import ProviderRefusal
from factory.llm.router import model_for
from factory.logging import get_logger
from factory.memory import store as memory_store
from factory.settings import EngineName, TaskType
from factory.supervisor.context import FactoryContext

log = get_logger(__name__)


class RoleNotPermitted(ValueError):
    """A role was requested that this factory cannot currently run."""


@dataclass(slots=True)
class SpawnRequest:
    role: RoleName
    objective: str
    context: str = ""
    engine: EngineName | None = None
    product_id: str | None = None
    task_id: str | None = None


def resolve_role(name: str) -> AgentRole:
    try:
        return get_role(name)
    except (KeyError, ValueError) as exc:
        raise RoleNotPermitted(f"unknown agent role {name!r}") from exc


def can_spawn(
    ctx: FactoryContext, role: AgentRole, *, from_own_call_site: bool = False
) -> tuple[bool, str]:
    """Whether this role can run right now, given the factory's actual capabilities.

    ``from_own_call_site`` is the authorisation a scoped role's one legitimate
    caller passes to say "I am that caller" -- see CALL_SITE_SCOPED_ROLES.
    """
    if role.name in CALL_SITE_SCOPED_ROLES and not from_own_call_site:
        # These roles are only correct inside the one caller that supplies what they
        # depend on -- a held product lock and a workspace snapshot (WORKSPACE_SURGEON,
        # SEO_OPTIMIZER), a Discovery cycle's own task_id (DISCOVERY_SCOUT), or a
        # private registry holding a live browser page (AUDITOR). They are already
        # filtered out of `role_catalogue()`, but that only means the Orchestrator is
        # never *shown* them -- it does not stop a model emitting the name anyway, and
        # until this guard existed `resolve_role` would happily hand one over.
        return False, f"{role.name.value} may only run from its own call site, not a generic spawn"
    if "sandbox" in role.tool_categories and not ctx.docker_available:
        return False, "role needs a sandbox and no Docker daemon is reachable"
    if role.mutating and ctx.dry_run:
        return False, "role can mutate state and FACTORY_DRY_RUN is set"
    return True, "permitted"


async def spawn(
    ctx: FactoryContext,
    session: AsyncSession | None,
    request: SpawnRequest,
    *,
    from_own_call_site: bool = False,
) -> AgentResult:
    """Create, run, and discard one ephemeral agent.

    Anything the agent concludes is written to memory before the result is returned,
    so the next agent that faces the same situation starts from what this one learned
    rather than rediscovering it.

    ``from_own_call_site=True`` is how a CALL_SITE_SCOPED_ROLES role's one legitimate
    caller authorises itself (currently `discovery/scout.py`, which supplies the
    task_id DISCOVERY_SCOUT's `submit_signal` requires). Defaults to False so an
    Orchestrator-chosen role can never reach one.
    """
    role = resolve_role(request.role)
    permitted, reason = can_spawn(ctx, role, from_own_call_site=from_own_call_site)
    if not permitted:
        log.info("agent.spawn_refused", role=role.name.value, reason=reason)
        return AgentResult(
            objective=request.objective,
            answer=f"Agent not spawned: {reason}.",
            steps=0,
            stopped_because="not_permitted",
        )

    recalled = ""
    if session is not None:
        recalled = await memory_store.recall_block(
            session,
            query=f"{request.objective} {request.context}",
            scopes=memory_store.scopes_for(engine=request.engine, product_id=request.product_id),
        )

    runtime = AgentRuntime(ctx.llm, settings=ctx.settings)
    log.info(
        "agent.spawned",
        role=role.name.value,
        engine=str(request.engine) if request.engine else None,
        objective=request.objective[:160],
    )

    try:
        result = await runtime.run(
            objective=request.objective,
            system=role.system_prompt,
            tool_categories=list(role.tool_categories),
            task_type=role.task_type,
            engine=request.engine,
            task_id=request.task_id,
            max_steps=role.max_steps,
            deadline_seconds=role.deadline_seconds,
            docker_available=ctx.docker_available,
            context=_join(request.context, recalled),
            cost_cap_usd=role.cost_cap_usd,
            cost_model=_cost_model(ctx, role),
            max_context_chars=role.max_context_chars,
        )
    except ProviderRefusal:
        raise

    # `result.used_tools`, not just `result.succeeded`: Tier 1 finding #1 of the
    # system-wide verification plan. `succeeded` means only "the model emitted a
    # final turn with no tool calls and non-empty text" -- a fully hallucinated
    # answer with zero tool calls satisfied it exactly as well as one backed by
    # real investigation, and this was the *only* gate before a lesson became
    # permanent shared memory every future agent recalls as established fact.
    # Every role (`agents/roles.py`) has at least one real tool category, so
    # this never blocks a role that was designed to be tool-free -- none are.
    if session is not None and result.succeeded and result.used_tools and result.answer.strip():
        await memory_store.remember(
            session,
            content=result.answer[: memory_store.MAX_MEMORY_CHARS],
            kind=memory_store.MemoryKind.LESSON,
            scope=memory_store.scope_for(engine=request.engine, product_id=request.product_id),
            source_task_id=request.task_id,
            origin=f"agent:{role.name.value}",
        )

    return result


def _join(*blocks: str) -> str:
    return "\n\n".join(block.strip() for block in blocks if block and block.strip())


def _cost_model(ctx: FactoryContext, role: AgentRole) -> str | None:
    """Which model a role's `cost_cap_usd` should be priced against.

    Only CODE_GENERATION/CODE_REVIEW/CODE_REPAIR are in `router.NO_FAILOVER`, so
    only those are deterministically served by one configured model -- pricing
    against it is exact, the same reasoning `repair.py`'s own direct call already
    uses. `router.model_for` resolves the right one per task type (`model_code`
    for CODE_GENERATION/CODE_REVIEW, `model_deepseek` for CODE_REPAIR). Every
    other role's task type can fail over between providers mid-run, so there is
    no single correct model name to price against; `None` lets `_spent_usd` fall
    back to its mid-range default price, which errs toward stopping early rather
    than silently overspending.
    """
    if role.task_type in (TaskType.CODE_GENERATION, TaskType.CODE_REVIEW, TaskType.CODE_REPAIR):
        return model_for(role.task_type, ctx.settings)
    return None
