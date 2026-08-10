"""Development Engine runner.

Build → test in sandbox → Bug Council → deploy, with healing cycles in between.

Healing is re-enqueued as a new task rather than looped in-process. A three-hour
in-process retry loop would lose all its progress on a reboot; a task with an
incremented ``heal_attempts`` counter picks up exactly where it left off.

The product lock is held for the whole cycle, which is what pauses paid marketing
spend for that product until it is healthy again.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from factory.engines.development import bug_council
from factory.engines.development import deploy as deploy_module
from factory.engines.development.scaffold.generator import (
    generate_scaffold,
    read_workspace,
    write_plan,
)
from factory.hitl import interrupts
from factory.logging import get_logger
from factory.sandbox.runner import DevOpsError, SandboxUnavailable, is_devops_error
from factory.settings import EngineName
from factory.state import repository
from factory.state.orm import Idea, Product, Task
from factory.state.schemas import (
    InterruptReason,
    ProductStatus,
    TaskKind,
    ValidatedIdea,
)
from factory.supervisor.concurrency import LockUnavailable, product_lock
from factory.supervisor.context import FactoryContext

log = get_logger(__name__)

MAX_HEAL_CYCLES = 3


async def run(ctx: FactoryContext, session: AsyncSession, task: Task) -> str:
    if not ctx.sandbox.available:
        # Fail loudly rather than running model-written code on the host.
        raise SandboxUnavailable(
            "Development requires a Docker sandbox; generated code is never run on the host."
        )

    kind = TaskKind(task.kind)
    if kind is TaskKind.BUILD_MVP:
        return await _build(ctx, session, task)
    if kind is TaskKind.PATCH_PRODUCT:
        return await _patch(ctx, session, task)
    return f"development: unsupported task kind {task.kind}"


async def _load_idea(session: AsyncSession, idea_id: str) -> ValidatedIdea | None:
    row = await session.get(Idea, idea_id)
    if row is None:
        return None
    return ValidatedIdea(
        id=row.id,
        problem_statement=row.problem_statement,
        target_niche=row.target_niche,
        source_url=row.source_url,
        monetization_hypothesis=row.monetization_hypothesis,
        score=row.score,
        evidence_count=row.evidence_count,
        created_at=row.created_at,
    )


async def _build(ctx: FactoryContext, session: AsyncSession, task: Task) -> str:
    idea_id = task.idea_id or str(task.payload.get("idea_id", ""))
    idea = await _load_idea(session, idea_id) if idea_id else None
    if idea is None:
        return f"development: idea {idea_id!r} not found"

    product_name = idea.target_niche[:80]
    product_id = task.product_id or await repository.create_product(
        session, name=product_name, idea_id=idea.id
    )
    workspace = f"product-{product_id[:12]}"

    try:
        async with product_lock(
            session,
            product_id=product_id,
            holder=EngineName.DEVELOPMENT,
            reason="building MVP",
        ):
            await repository.set_product_status(
                session,
                product_id,
                ProductStatus.BUILDING,
                workspace_path=workspace,
            )
            plan = await generate_scaffold(ctx, idea, task_id=task.id)
            written = write_plan(plan, workspace, ctx.settings)
            log.info("development.scaffolded", product_id=product_id, files=len(written))

            return await _verify_and_ship(
                ctx,
                session,
                product_id=product_id,
                product_name=plan.product_name or product_name,
                workspace=workspace,
                test_command=plan.test_command,
                idea_id=idea.id,
                task_id=task.id,
            )
    except LockUnavailable:
        return f"development: product {product_id} is locked by another worker"


async def _patch(ctx: FactoryContext, session: AsyncSession, task: Task) -> str:
    product_id = task.product_id or str(task.payload.get("product_id", ""))
    product = await session.get(Product, product_id) if product_id else None
    if product is None:
        return f"development: product {product_id!r} not found"

    idea = await _load_idea(session, product.idea_id)
    if idea is None:
        return f"development: idea for product {product_id} is gone"

    workspace = product.workspace_path or f"product-{product_id[:12]}"
    healing = str(task.payload.get("healing", "Address the outstanding review findings."))

    try:
        async with product_lock(
            session,
            product_id=product_id,
            holder=EngineName.DEVELOPMENT,
            reason="patching product",
        ):
            await repository.set_product_status(session, product_id, ProductStatus.PATCHING)
            plan = await generate_scaffold(
                ctx,
                idea.model_copy(
                    update={
                        "problem_statement": (
                            f"{idea.problem_statement}\n\n"
                            f"REQUIRED REPAIRS (from the previous review):\n{healing}"
                        )
                    }
                ),
                task_id=task.id,
            )
            write_plan(plan, workspace, ctx.settings)

            return await _verify_and_ship(
                ctx,
                session,
                product_id=product_id,
                product_name=product.name,
                workspace=workspace,
                test_command=plan.test_command,
                idea_id=product.idea_id,
                task_id=task.id,
            )
    except LockUnavailable:
        return f"development: product {product_id} is locked by another worker"


async def _verify_and_ship(
    ctx: FactoryContext,
    session: AsyncSession,
    *,
    product_id: str,
    product_name: str,
    workspace: str,
    test_command: str,
    idea_id: str,
    task_id: str | None = None,
) -> str:
    await repository.set_product_status(session, product_id, ProductStatus.REVIEWING)

    # Network is explicitly enabled for this one step — dependency installation
    # cannot succeed against `--network none`, the sandbox's otherwise-correct
    # default. The `|| true` that used to swallow every install failure is gone: a
    # real failure now surfaces (as a devops escalation or a heal cycle, whichever
    # it actually is) instead of silently leaving requirements uninstalled and
    # producing a confusing downstream test failure.
    #
    # --target=/workspace/.deps, not a bare install: this and the test run below are
    # two *separate* `docker run` invocations (kept separate deliberately, so a
    # dependency-install failure and a test failure get distinct devops-error
    # detail). Nothing outside the bind-mounted /workspace survives between them --
    # a plain install lands in the ephemeral container's own filesystem (or, once
    # $HOME pointed at the tmpfs, in a tmpfs that dies with that container) and is
    # gone by the time the test container starts. Installing into /workspace and
    # pointing PYTHONPATH there for the test run is what makes the install actually
    # visible to it.
    install = await ctx.sandbox.execute(
        "python -m pip install --quiet --no-input --target=/workspace/.deps -r requirements.txt",
        workspace=workspace,
        network=True,
    )
    if install.timed_out:
        return await _heal(
            ctx,
            session,
            product_id,
            "Dependency installation timed out.",
            idea_id,
            category="dependency_timeout",
        )
    if is_devops_error(install):
        raise DevOpsError(
            "Dependency installation failed with what looks like a host/environment "
            f"error, not an application bug:\n{(install.stderr or install.stdout)[-2000:]}"
        )
    if not install.ok:
        detail = (install.stderr or install.stdout)[-2000:]
        return await _heal(
            ctx,
            session,
            product_id,
            f"Dependency installation failed (exit {install.exit_code}):\n{detail}",
            idea_id,
            category="dependency_failure",
        )

    # PATH is extended in the command itself, not via `env=`: an `--env PATH=...`
    # would *replace* the container's existing PATH rather than extend it, and that
    # base value depends on SANDBOX_IMAGE (configurable), so it can't be safely
    # hardcoded here. `$PATH` in the shell command expands to whatever it actually
    # is at runtime. Covers `test_command` invoking an installed console script
    # directly (e.g. a bare `pytest`) -- `pip install --target` does not reliably
    # put those on PATH the way a normal install does; `python -m pytest` would
    # have worked from PYTHONPATH alone, but nothing constrains the model to that
    # form.
    tests = await ctx.sandbox.execute(
        f'export PATH="/workspace/.deps/bin:$PATH"; {test_command}',
        workspace=workspace,
        env={"PYTHONPATH": "/workspace/.deps"},
    )
    if is_devops_error(tests):
        raise DevOpsError(
            "The test run failed with what looks like a host/environment error, not "
            f"an application bug:\n{(tests.stderr or tests.stdout)[-2000:]}"
        )
    if not tests.ok:
        detail = (tests.stderr or tests.stdout)[-2000:]
        return await _heal(
            ctx,
            session,
            product_id,
            f"The test suite failed (exit {tests.exit_code}):\n{detail}",
            idea_id,
            category="test_failure",
        )

    sources = read_workspace(workspace, ctx.settings)
    council = await bug_council.convene(ctx, sources, task_id=task_id)
    if not council.approved:
        healing = council.healing
        summary = healing.summary if healing else "Bug Council rejected the build."
        steps = "\n".join(healing.ordered_steps) if healing else ""
        blocked_by = sorted({str(v.category) for v in council.verdicts if not v.approved})
        return await _heal(
            ctx,
            session,
            product_id,
            f"{summary}\n{steps}",
            idea_id,
            category=f"council_rejection:{','.join(blocked_by)}",
        )

    result = await deploy_module.deploy(
        council=council,
        product_id=product_id,
        product_name=product_name,
        workspace=workspace,
        settings=ctx.settings,
        dry_run=ctx.dry_run,
    )
    if not result.ok:
        return await _heal(
            ctx,
            session,
            product_id,
            f"Deployment failed: {result.detail}",
            idea_id,
            category="deploy_failure",
        )

    await repository.resolve_pending_heal_attempts(session, product_id, "healed")
    await repository.set_product_status(
        session, product_id, ProductStatus.LIVE, deploy_url=result.url, heal_attempts=0
    )
    await repository.enqueue_task(
        session,
        kind=TaskKind.HARVEST_LEADS,
        engine=EngineName.MARKETING,
        idempotency_key=f"leads:{product_id}",
        payload={"product_id": product_id},
        product_id=product_id,
        priority=120,
    )
    return f"development: product {product_id} approved and deployed to {result.url}"


async def _heal(
    ctx: FactoryContext,
    session: AsyncSession,
    product_id: str,
    healing: str,
    idea_id: str,
    *,
    category: str,
) -> str:
    """Queue one repair cycle, or escalate once the budget is spent.

    Pairs this cycle's ``healing`` instructions with its eventual outcome in
    ``heal_attempts`` — audit-only today (the two prompts that could act on it,
    codegen and Bug Council, are both permanently excluded from the learning loop),
    but it closes the gap where that pairing previously existed only by scanning
    ``tasks.payload`` blobs ordered by creation time.
    """
    # Whatever the *previous* attempt tried did not work, or we would not be here.
    await repository.resolve_pending_heal_attempts(session, product_id, "failed_again")

    product = await session.get(Product, product_id)
    attempts = (product.heal_attempts if product else 0) + 1
    await repository.set_product_status(
        session, product_id, ProductStatus.PATCHING, heal_attempts=attempts
    )

    if attempts >= MAX_HEAL_CYCLES:
        await repository.record_heal_attempt(
            session,
            product_id=product_id,
            task_id=None,
            attempt_number=attempts,
            healing_text=healing,
            failed_categories=[category],
        )
        await repository.resolve_pending_heal_attempts(session, product_id, "escalated")
        await repository.set_product_status(session, product_id, ProductStatus.PLANNED)
        await repository.release_product_lock(session, product_id)
        await interrupts.request(
            session,
            reason=InterruptReason.LEGAL_UNCERTAINTY,
            subject=f"Product {product_id} failed {MAX_HEAL_CYCLES} heal cycles",
            detail=(
                "Automated repair did not converge. The last blocking feedback was:\n\n"
                f"{healing[:2000]}\n\n"
                "Reply with guidance, or REJECT to retire this product."
            ),
            product_id=product_id,
        )
        log.warning("development.heal_exhausted", product_id=product_id, attempts=attempts)
        return f"development: product {product_id} escalated after {attempts} heal cycles"

    patch_task_id = await repository.enqueue_task(
        session,
        kind=TaskKind.PATCH_PRODUCT,
        engine=EngineName.DEVELOPMENT,
        idempotency_key=f"patch:{product_id}:{attempts}",
        payload={"product_id": product_id, "idea_id": idea_id, "healing": healing[:6000]},
        product_id=product_id,
        priority=40,  # repairing a broken product outranks starting a new one
    )
    await repository.record_heal_attempt(
        session,
        product_id=product_id,
        task_id=patch_task_id,
        attempt_number=attempts,
        healing_text=healing,
        failed_categories=[category],
    )
    log.info("development.heal_queued", product_id=product_id, attempt=attempts)
    return f"development: product {product_id} needs repair (cycle {attempts})"
