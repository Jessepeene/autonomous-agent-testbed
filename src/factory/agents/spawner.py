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

from factory.agents.roles import AgentRole, RoleName, get_role
from factory.agents.runtime import AgentResult, AgentRuntime
from factory.llm.providers.base import ProviderRefusal
from factory.logging import get_logger
from factory.memory import store as memory_store
from factory.settings import EngineName
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


def can_spawn(ctx: FactoryContext, role: AgentRole) -> tuple[bool, str]:
    """Whether this role can run right now, given the factory's actual capabilities."""
    if "sandbox" in role.tool_categories and not ctx.docker_available:
        return False, "role needs a sandbox and no Docker daemon is reachable"
    if role.mutating and ctx.dry_run:
        return False, "role can mutate state and FACTORY_DRY_RUN is set"
    return True, "permitted"


async def spawn(
    ctx: FactoryContext,
    session: AsyncSession | None,
    request: SpawnRequest,
) -> AgentResult:
    """Create, run, and discard one ephemeral agent.

    Anything the agent concludes is written to memory before the result is returned,
    so the next agent that faces the same situation starts from what this one learned
    rather than rediscovering it.
    """
    role = resolve_role(request.role)
    permitted, reason = can_spawn(ctx, role)
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
        )
    except ProviderRefusal:
        raise

    if session is not None and result.succeeded and result.answer.strip():
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
