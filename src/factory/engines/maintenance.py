"""Maintenance runner — the never-idle work.

This used to ask a model for a report and log it, which satisfied "never idle" in the
letter and not at all in the spirit: nothing was investigated and nothing changed.

Now each catalogue item spawns an ephemeral agent with real tools, and whatever it
concludes is written to memory so the next run starts from it rather than rediscovering
it. Items that map to concrete follow-up work enqueue that work as tasks.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from factory.agents.roles import RoleName
from factory.agents.spawner import SpawnRequest, spawn
from factory.logging import get_logger
from factory.memory import store as memory_store
from factory.settings import EngineName
from factory.state import repository
from factory.state.orm import Product, Task
from factory.state.schemas import ProductStatus, TaskKind, TaskStatus
from factory.supervisor.context import FactoryContext

log = get_logger(__name__)

#: Which catalogue item gets which capability envelope. An item not listed here runs
#: as an analyst, which is the read-only default.
ROLE_BY_SLUG: dict[str, RoleName] = {
    "audit-live-products": RoleName.AUDITOR,
    "triage-escalated-tasks": RoleName.ANALYST,
    "reduce-llm-cost": RoleName.ANALYST,
    "improve-test-coverage": RoleName.REPAIR_ENGINEER,
    "refine-agent-prompts": RoleName.ANALYST,
    "refresh-market-signals": RoleName.RESEARCHER,
}


async def run(ctx: FactoryContext, session: AsyncSession, task: Task) -> str:
    slug = str(task.payload.get("slug", "maintenance"))

    # These four are either fully deterministic or need the two-step structured
    # extraction shape from `learning.maintenance` rather than the generic
    # prose-`_act_on()` path below, so they're handled before the generic agent spawn.
    if slug == "propose-prompt-mutation":
        from factory.learning.maintenance import propose_mutations

        return await propose_mutations(ctx, session, task)
    if slug == "evaluate-tunable-trials":
        from factory.learning.trials import promote_and_evaluate

        return await promote_and_evaluate(session)
    if slug == "reconcile-outreach-conversions":
        from factory.engines.marketing.attribution import reconcile

        report = await reconcile(session)
        return f"marketing/attribution: {report.attributed}/{report.checked} attributed"
    if slug == "prune-memory":
        from factory.memory.store import prune as prune_memory

        prune_report = await prune_memory(session)
        return (
            f"memory/prune: {prune_report.pruned} evicted across "
            f"{prune_report.scopes_checked} scope(s)"
        )

    objective = str(task.payload.get("objective", "Improve the factory."))
    role = ROLE_BY_SLUG.get(slug, RoleName.ANALYST)

    context = await _gather_context(ctx, session, slug)
    result = await spawn(
        ctx,
        session,
        SpawnRequest(
            role=role,
            objective=objective,
            context=context,
            engine=EngineName.SUPERVISOR,
            task_id=task.id,
        ),
    )

    if not result.succeeded:
        log.info("maintenance.incomplete", slug=slug, stopped=result.stopped_because)
        return f"maintenance/{slug}: {result.summary()}"

    follow_ups = await _act_on(ctx, session, slug, result.answer)
    log.info(
        "maintenance.done",
        slug=slug,
        role=role.value,
        steps=result.steps,
        follow_ups=follow_ups,
    )
    return f"maintenance/{slug}: {result.summary()}; {follow_ups} follow-up task(s)"


async def _gather_context(ctx: FactoryContext, session: AsyncSession, slug: str) -> str:
    """Hand the agent the state it needs, so it does not have to guess or ask."""
    blocks: list[str] = [
        f"Factory mode: {'dry-run' if ctx.dry_run else 'live'}.",
        f"Enabled engines: {sorted(e.value for e in ctx.enabled_engines)}.",
        f"Disabled engines: {ctx.disabled_reasons or 'none'}.",
    ]

    if slug == "triage-escalated-tasks":
        stmt = (
            select(Task)
            .where(Task.status == TaskStatus.ESCALATED.value)
            .order_by(Task.updated_at.desc())
            .limit(10)
        )
        escalated = (await session.execute(stmt)).scalars().all()
        if escalated:
            blocks.append(
                "Escalated tasks:\n"
                + "\n".join(
                    f"- {t.id} [{t.kind}/{t.engine}] after {t.attempt_count} attempts: "
                    f"{(t.last_error or 'no error recorded')[:300]}"
                    for t in escalated
                )
            )
        else:
            blocks.append("No tasks are currently escalated.")

    elif slug == "audit-live-products":
        live_stmt = select(Product).where(Product.status == ProductStatus.LIVE.value).limit(10)
        live = (await session.execute(live_stmt)).scalars().all()
        if live:
            blocks.append(
                "Live products to check:\n"
                + "\n".join(f"- {p.id} {p.name}: {p.deploy_url or 'no URL'}" for p in live)
            )
        else:
            blocks.append("No products are live yet; report that and stop.")

    elif slug == "reduce-llm-cost":
        from factory.observability.costs import spend_by_model

        spend = await spend_by_model(session, hours=168)
        if spend:
            blocks.append(
                "LLM spend, last 7 days:\n"
                + "\n".join(
                    f"- {r.provider}/{r.model}: {r.calls} calls, "
                    f"{r.input_tokens} in / {r.output_tokens} out, ${r.usd:.4f}"
                    for r in spend
                )
            )
        else:
            blocks.append("No model calls recorded yet; report that and stop.")

    return "\n\n".join(blocks)


async def _act_on(ctx: FactoryContext, session: AsyncSession, slug: str, answer: str) -> int:
    """Turn findings into queued work where the mapping is unambiguous.

    Only mechanical follow-ups are created here. Anything requiring judgement about
    *what* to build stays a memory for the Orchestrator to weigh, because an agent
    that can silently enqueue arbitrary work is an agent that can spend arbitrary money.
    """
    created = 0

    if slug == "audit-live-products":
        stmt = select(Product).where(Product.status == ProductStatus.LIVE.value).limit(10)
        for product in (await session.execute(stmt)).scalars().all():
            if not _looks_degraded(answer, product.id, product.name):
                continue
            task_id = await repository.enqueue_task(
                session,
                kind=TaskKind.PATCH_PRODUCT,
                engine=EngineName.DEVELOPMENT,
                idempotency_key=f"patch:{product.id}:audit:{product.updated_at:%Y%m%d%H}",
                payload={
                    "product_id": product.id,
                    "idea_id": product.idea_id,
                    "healing": (
                        f"A health audit reported this product as degraded:\n{answer[:3000]}"
                    ),
                },
                product_id=product.id,
                priority=30,
            )
            if task_id:
                created += 1
                await memory_store.remember(
                    session,
                    content=(
                        f"Product {product.name} was found degraded by a health audit "
                        f"and a patch was queued. Symptoms: {answer[:600]}"
                    ),
                    kind=memory_store.MemoryKind.FAILURE,
                    scope=memory_store.scope_for(product_id=product.id),
                    origin="maintenance:audit",
                )

    return created


def _looks_degraded(answer: str, product_id: str, product_name: str) -> bool:
    """Conservative: the product must be named *and* a failure signal present.

    A loose match here would queue a patch — and therefore pause paid spend — for a
    healthy product every time the word "error" appeared in a report.
    """
    lowered = answer.lower()
    named = product_id.lower() in lowered or product_name.lower() in lowered
    if not named:
        return False
    signals = ("degraded", "down", "unreachable", "timeout", "5xx", "500", "503", "error")
    return any(signal in lowered for signal in signals)
