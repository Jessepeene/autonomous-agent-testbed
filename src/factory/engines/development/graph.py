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

from factory.engines.development import bug_council, repair
from factory.engines.development import deploy as deploy_module
from factory.engines.development.scaffold.generator import (
    LANDING_PAGE_SYSTEM,
    clear_workspace,
    generate_landing_page,
    generate_scaffold,
    read_workspace,
    write_plan,
)
from factory.hitl import interrupts
from factory.learning import prompts as learning_prompts
from factory.logging import get_logger
from factory.memory import store as memory_store
from factory.observability import costs
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

#: Failure kinds a surgical, in-place repair can actually address.
#:
#: `test_failure`: a real traceback to read and a suite to re-run -- the case the
#: whole approach is built around.
#: `guided`: a human has diagnosed the problem and said what to change, which is the
#: highest-information repair this factory ever performs.
#:
#: Everything else is deliberately absent. `dependency_*` cannot be fixed by an agent
#: whose sandbox has no network; `deploy_failure` is not a workspace bug at all;
#: `council_rejection:*` is plausible future work but the agent cannot re-run the
#: Council, so it would be editing blind to the only opinion that decides the outcome.
SURGICAL_CATEGORIES = frozenset({"test_failure", "guided"})


async def run(ctx: FactoryContext, session: AsyncSession, task: Task) -> str:
    kind = TaskKind(task.kind)

    # EVALUATE_VALIDATION is a pure DB decision -- no code generation, no sandbox
    # execution -- so it must not be blocked by sandbox unavailability the way every
    # other Development task kind correctly is.
    if kind is TaskKind.EVALUATE_VALIDATION:
        return await _evaluate_validation(ctx, session, task)

    if not ctx.sandbox.available:
        # Fail loudly rather than running model-written code on the host.
        raise SandboxUnavailable(
            "Development requires a Docker sandbox; generated code is never run on the host."
        )

    if kind is TaskKind.BUILD_MVP:
        return await _build(ctx, session, task)
    if kind is TaskKind.PATCH_PRODUCT:
        return await _patch(ctx, session, task)
    if kind is TaskKind.BUILD_LANDING_PAGE:
        return await _build_landing_page(ctx, session, task)
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
            # A retried build_mvp task (after a crash mid-attempt) reuses this same
            # product-scoped workspace -- clear it so a stale partial write from the
            # earlier attempt can't survive alongside this one.
            clear_workspace(workspace, ctx.settings)
            written = write_plan(plan, workspace, ctx.settings)
            # Recorded here, from the plan that actually produced these files, so the
            # agentic repair path can run the same suite later without regenerating
            # the plan to rediscover the command. Generation always overwrites it; a
            # repair never does.
            await repository.set_product_status(
                session, product_id, ProductStatus.BUILDING, test_command=plan.test_command
            )
            repair.write_initial_knowledge(
                workspace,
                ctx.settings,
                product_name=plan.product_name or product_name,
                summary=plan.summary,
                paths=[f.path for f in plan.files],
            )
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


_LANDING_PAGE_KEY = "factory.engines.development.scaffold.generator:LANDING_PAGE_SYSTEM"


async def _build_landing_page(ctx: FactoryContext, session: AsyncSession, task: Task) -> str:
    """The idea validation gate's first step: a cheap pitch page, not a real MVP.

    Same lock/status shape as `_build`, same `_verify_and_ship` pipeline (same
    sandboxed pytest + full Bug Council rigor -- the page is still public-facing
    code handling submitted email addresses) -- the only difference is what happens
    after a successful deploy, via `_verify_and_ship`'s `is_validation` flag: the
    product stays VALIDATING (not LIVE) and gets a validation-scoped lead harvest,
    not the real post-launch one.
    """
    idea_id = task.idea_id or str(task.payload.get("idea_id", ""))
    idea = await _load_idea(session, idea_id) if idea_id else None
    if idea is None:
        return f"development: idea {idea_id!r} not found"

    product_id = task.product_id or str(task.payload.get("product_id", ""))
    if not product_id:
        return f"development: task carried no product_id for validation of {idea_id}"
    workspace = f"product-{product_id[:12]}"

    try:
        async with product_lock(
            session,
            product_id=product_id,
            holder=EngineName.DEVELOPMENT,
            reason="building validation page",
        ):
            prompt_text, prompt_version = await learning_prompts.get_prompt(
                session, key=_LANDING_PAGE_KEY, default=LANDING_PAGE_SYSTEM
            )
            await repository.set_product_status(
                session,
                product_id,
                ProductStatus.BUILDING,
                workspace_path=workspace,
                landing_page_prompt_version=prompt_version,
            )
            plan = await generate_landing_page(
                ctx,
                idea,
                product_id=product_id,
                system=prompt_text,
                tunable_parameter_id=prompt_version,
                task_id=task.id,
            )
            clear_workspace(workspace, ctx.settings)
            written = write_plan(plan, workspace, ctx.settings)
            await repository.set_product_status(
                session, product_id, ProductStatus.BUILDING, test_command=plan.test_command
            )
            log.info("development.validation_scaffolded", product_id=product_id, files=len(written))

            return await _verify_and_ship(
                ctx,
                session,
                product_id=product_id,
                product_name=plan.product_name or idea.target_niche[:80],
                workspace=workspace,
                test_command=plan.test_command,
                idea_id=idea.id,
                task_id=task.id,
                is_validation=True,
            )
    except LockUnavailable:
        return f"development: product {product_id} is locked by another worker"


async def _evaluate_validation(ctx: FactoryContext, session: AsyncSession, task: Task) -> str:
    """The idea validation gate's decision point: enough real interest to build, or
    scrap it.

    Tallies distinct `validation_signals` against outreach actually sent, judged
    against a fixed threshold that is never learned or tuned -- see
    `learning/allowlist.py` for the one thing this stage DOES feed into the
    Learning Loop: the landing page copy itself (`LANDING_PAGE_SYSTEM`), not the
    bar it's judged against. A learnable bar would multiply the confound surface
    for no clear win, same reasoning as the existing non-learnable SCORE_THRESHOLD.
    """
    product_id = task.product_id or str(task.payload.get("product_id", ""))
    product = await session.get(Product, product_id) if product_id else None
    if product is None:
        return f"development: product {product_id!r} not found for validation evaluation"

    sent = await repository.count_outreach_sent(session, product_id)
    signals = await repository.tally_validation_signals(session, product_id)
    response_rate = (signals / sent) if sent else 0.0
    passed = (
        sent >= ctx.settings.validation_min_sample_size
        and response_rate >= ctx.settings.validation_min_response_rate
    )

    if product.landing_page_prompt_version:
        await learning_prompts.record_outcome(
            session,
            tunable_parameter_id=product.landing_page_prompt_version,
            success=passed,
            detail={"sent": sent, "signals": signals, "response_rate": response_rate},
            task_id=task.id,
        )

    if passed:
        await repository.set_product_status(session, product_id, ProductStatus.PLANNED)
        await repository.enqueue_task(
            session,
            kind=TaskKind.BUILD_MVP,
            engine=EngineName.DEVELOPMENT,
            idempotency_key=f"build:{product.idea_id}",
            payload={"idea_id": product.idea_id, "product_id": product_id},
            idea_id=product.idea_id,
            product_id=product_id,
            priority=50,
        )
        log.info(
            "development.validation_passed",
            product_id=product_id,
            sent=sent,
            signals=signals,
            response_rate=response_rate,
        )
        return f"development: validation for {product_id} passed ({signals}/{sent}), building"

    # retire_product (not a bare set_product_status) so this path also gets PII
    # retention -- see purge_product_pii -- exactly like the heal-exhaustion
    # retirement path. RETIRED itself is shared between the two (Product carries no
    # reason field), so the memory row below is the durable trail that lets a future
    # audit tell them apart, matching this codebase's instinct to never leave a
    # terminal decision unexplained.
    await repository.retire_product(session, product_id)
    await memory_store.remember(
        session,
        content=(
            f"Product {product.name} did not clear the validation gate: "
            f"{signals}/{sent} interest signals ({response_rate:.1%}), below the "
            f"{ctx.settings.validation_min_response_rate:.1%} bar on "
            f"{ctx.settings.validation_min_sample_size}+ sent required. Retired "
            "without a real build ever being attempted."
        ),
        kind=memory_store.MemoryKind.FAILURE,
        scope=memory_store.scope_for(product_id=product_id),
        origin="development:evaluate_validation",
    )
    log.info(
        "development.validation_failed",
        product_id=product_id,
        sent=sent,
        signals=signals,
        response_rate=response_rate,
    )
    return f"development: validation for {product_id} failed ({signals}/{sent}), retired"


async def _should_repair_surgically(
    ctx: FactoryContext, session: AsyncSession, product: Product, category: str
) -> bool:
    """Whether this cycle edits the workspace in place instead of regenerating it.

    Every condition is a case where a surgical repair is either impossible or worse
    than a clean rebuild -- the fallback is always today's full regeneration, which
    remains the only operation that can recover an arbitrarily damaged workspace.
    """
    if category not in SURGICAL_CATEGORIES:
        # A dependency failure cannot be fixed by an agent whose sandbox has no
        # network and therefore cannot run pip at all; a deploy failure is not a
        # workspace bug; a council rejection is plausible future work but the agent
        # cannot re-run the Council, so it would be editing blind to the only opinion
        # that matters.
        return False
    if not product.test_command:
        # Built before test_command was recorded. Guessing a default would have the
        # agent optimising against a different suite than the gate runs -- a repair
        # that looks successful right up until verification.
        return False
    if category != "guided" and product.heal_attempts >= MAX_HEAL_CYCLES - 1:
        # The last autonomous cycle always regenerates: two surgical attempts have
        # already failed, and a from-scratch rebuild is the only thing left that can
        # recover a workspace they may have made worse.
        #
        # A guided attempt is exempt because the ceiling is about *autonomous* retries
        # running out of ideas, and a guided patch is by definition not one -- it is
        # only ever created by a human replying to an escalation, arrives with
        # heal_attempts already at the maximum, and carries information no earlier
        # cycle had. Regenerating a live product from scratch over one human
        # instruction would discard everything that currently works to fix one thing.
        return False
    if not ctx.docker_available:
        return False
    cap = ctx.settings.daily_llm_spend_cap_usd
    if cap is not None:
        spent = await costs.total_usd(session, hours=24)
        if cap - spent < ctx.settings.repair_agent_min_budget_headroom_usd:
            log.info("development.repair_skipped_budget", spent_usd=spent, cap_usd=cap)
            return False
    return True


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
    category = str(task.payload.get("category", ""))

    try:
        async with product_lock(
            session,
            product_id=product_id,
            holder=EngineName.DEVELOPMENT,
            reason="patching product",
        ):
            await repository.set_product_status(session, product_id, ProductStatus.PATCHING)
            # Before anything else: a snapshot left behind by an earlier attempt means
            # that attempt died mid-edit. Its session rolled back, so heal_attempts
            # never moved and nothing anywhere records that the workspace was touched
            # -- and this task gets up to max_task_attempts retries. Without restoring
            # first, one crash becomes three successive passes over a progressively
            # more damaged workspace, each driven by healing text that no longer
            # describes what is actually wrong.
            if repair.restore_snapshot(workspace, ctx.settings):
                log.warning("development.recovered_partial_repair", product_id=product_id)

            # Surfaces this product's own heal-cycle history (written by _heal() on
            # every cycle) so a recurring category of failure is visible to the model
            # attempting the fix, not just to a future audit -- the whole point of
            # closing this loop is that repeating the same failed approach should get
            # less likely each cycle, not equally likely every time.
            recalled = await memory_store.recall_block(
                session,
                query=healing,
                scopes=memory_store.scopes_for(product_id=product_id),
            )

            if await _should_repair_surgically(ctx, session, product, category):
                return await _patch_surgically(
                    ctx,
                    session,
                    task,
                    product=product,
                    workspace=workspace,
                    healing=healing,
                    recalled=recalled,
                )

            existing = read_workspace(workspace, ctx.settings)
            repairs = f"REQUIRED REPAIRS (from the previous review):\n{healing}"
            if recalled:
                repairs = f"{repairs}\n\n{recalled}"
            plan = await generate_scaffold(
                ctx,
                idea.model_copy(
                    update={"problem_statement": f"{idea.problem_statement}\n\n{repairs}"}
                ),
                task_id=task.id,
                existing_files=existing,
            )
            # Clear *after* generating (so the model saw the old files) and *before*
            # writing (so nothing from the previous attempt survives alongside the
            # new plan) -- see clear_workspace's docstring for the failure this
            # prevents: two divergent, independently-buggy copies of the app.
            clear_workspace(workspace, ctx.settings)
            write_plan(plan, workspace, ctx.settings)
            await repository.set_product_status(
                session, product_id, ProductStatus.PATCHING, test_command=plan.test_command
            )
            # A full regeneration replaces the code the old notes described, so the
            # structure *and* the repair history recorded against it are both stale --
            # reseed rather than append.
            repair.write_initial_knowledge(
                workspace,
                ctx.settings,
                product_name=plan.product_name or product.name,
                summary=plan.summary,
                paths=[f.path for f in plan.files],
            )

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


async def _patch_surgically(
    ctx: FactoryContext,
    session: AsyncSession,
    task: Task,
    *,
    product: Product,
    workspace: str,
    healing: str,
    recalled: str,
) -> str:
    """Repair by editing the workspace in place, then verify exactly as usual.

    Called with `product_lock` already held by `_patch`, which is what makes the
    workspace tools safe to use without taking it themselves.
    """
    product_id = product.id
    test_command = product.test_command or ""

    # `clear_workspace` used to wipe `.deps` on every cycle as a side effect of
    # wiping everything. Nothing regenerates here, so it has to be explicit -- see
    # clear_installed_deps for why a stale one silently defeats dependency fixes.
    repair.clear_installed_deps(workspace, ctx.settings)
    repair.take_snapshot(workspace, ctx.settings)

    try:
        outcome = await repair.attempt_repair(
            ctx,
            product_id=product_id,
            workspace=workspace,
            test_command=test_command,
            healing=healing,
            recalled=recalled,
            task_id=task.id,
        )
    except repair.RepairBlocked:
        # Never leave a half-edited workspace behind, and never charge this to the
        # product: RepairBlocked means the attempt was cut short by an outage or an
        # exhausted budget, which says nothing about whether the code is fixable.
        # `_run_engine` defers the task instead of counting an attempt.
        repair.restore_snapshot(workspace, ctx.settings)
        raise

    if not outcome.usable:
        repair.restore_snapshot(workspace, ctx.settings)
        reason = outcome.rejected_because or "no usable repair"
        # Straight to a heal cycle without running _verify_and_ship: that is the
        # expensive half (dependency install, full suite, three reviewers) and against
        # a workspace that is now byte-identical to the one that already failed, it
        # would fail in exactly the same way for exactly the same reason.
        log.info("development.repair_unusable", product_id=product_id, reason=reason)
        return await _heal(
            ctx,
            session,
            product_id,
            f"{healing}\n\nA surgical repair attempt was rejected: {reason}.",
            product.idea_id,
            category="repair_rejected",
        )

    result = await _verify_and_ship(
        ctx,
        session,
        product_id=product_id,
        product_name=product.name,
        workspace=workspace,
        test_command=test_command,
        idea_id=product.idea_id,
        task_id=task.id,
    )
    # Only now, with the suite and the Council having actually ruled on it, is the
    # agent's account of what it did worth keeping. Writing it at the point the agent
    # merely *claimed* success would persist an unverified belief that the next
    # cycle's recall would then read back as prior advice -- and near-duplicate
    # reinforcement would make a repeated wrong claim look progressively more
    # confident.
    if outcome.answer.strip():
        await memory_store.remember(
            session,
            content=outcome.answer[: memory_store.MAX_MEMORY_CHARS],
            kind=memory_store.MemoryKind.LESSON,
            scope=memory_store.scope_for(product_id=product_id),
            source_task_id=task.id,
            origin="development:surgical_repair",
        )
    # The durable half of the same fact. Memory is scored and pruned and can be
    # evicted; this file travels with the product and is what the next repair reads
    # first, however many months later that is.
    repair.record_verified_repair(
        workspace,
        ctx.settings,
        changed_files=outcome.changed_files,
        note=outcome.answer,
    )
    repair.discard_snapshot(workspace, ctx.settings)
    log.info(
        "development.repair_verified",
        product_id=product_id,
        files=len(outcome.changed_files),
        agent=outcome.summary,
    )
    return result


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
    is_validation: bool = False,
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
    try:
        council = await bug_council.convene(ctx, sources, task_id=task_id)
    except bug_council.CouncilUnavailable as exc:
        # A reviewer that could not run rendered no verdict on the code at all --
        # never route this into a heal cycle, which would spend an LLM call
        # regenerating code that was never actually found bad, or even reviewed.
        raise DevOpsError(f"Bug Council could not complete review: {exc}") from exc
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
    if not result.ok and result.is_infra:
        # A transport error, missing credential, auth/rate-limit problem, or 5xx
        # from Vercel is never the fault of the generated code -- regenerating it
        # cannot fix an outage. Route to the same devops-escalation path as a
        # sandbox infra failure instead of spending a heal cycle on it.
        raise DevOpsError(f"Deployment failed with an infrastructure error: {result.detail}")
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
    if is_validation:
        # Stays VALIDATING, not LIVE -- this is a pitch page, not a real product.
        # A distinct idempotency-key namespace (leads:validate:*) is deliberate:
        # the real post-launch harvest below uses leads:{product_id}, and if
        # this validation later succeeds and a genuine BUILD_MVP goes LIVE for
        # the same product_id, that harvest must still be able to fire -- reusing
        # the same key would make it silently no-op via ON CONFLICT DO NOTHING.
        await repository.set_product_status(
            session, product_id, ProductStatus.VALIDATING, deploy_url=result.url, heal_attempts=0
        )
        await repository.enqueue_task(
            session,
            kind=TaskKind.HARVEST_LEADS,
            engine=EngineName.MARKETING,
            idempotency_key=f"leads:validate:{product_id}",
            payload={"product_id": product_id},
            product_id=product_id,
            priority=120,
        )
        return f"development: validation page for {product_id} deployed to {result.url}"

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

    # Recorded on every cycle, not only at exhaustion: this is the signal that lets
    # the SAME category of failure recurring on this product be recognised as a
    # pattern (near-duplicate content reinforces one memory rather than piling up
    # fifty near-identical rows -- see memory/store.py's remember()), and it is what
    # _patch() recalls before its next generation attempt so the model sees what
    # already failed instead of repeating it blind. The attempt number is
    # deliberately left out of the text itself -- it would be a token that differs
    # every cycle and could push otherwise-identical repeats below the near-
    # duplicate threshold, defeating the reinforcement this exists for.
    await memory_store.remember(
        session,
        content=f"Heal cycle failed under category '{category}': {healing[:300]}",
        kind=memory_store.MemoryKind.FAILURE,
        scope=memory_store.scope_for(product_id=product_id),
        origin="development:heal",
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
        # A second, engine-scoped memory alongside the product-scoped one above: a
        # full heal-budget exhaustion is a stronger signal than one failed cycle, and
        # engine scope is what future spawned diagnostic agents draw on (see
        # agents/spawner.py's recall_block call), so a category that keeps burning
        # the whole heal budget across *different* products becomes visible there
        # too, not just to this one product's own future attempts.
        await memory_store.remember(
            session,
            content=(
                f"A product exhausted all {MAX_HEAL_CYCLES} heal cycles on category "
                f"'{category}' and was escalated to a human. Last blocking feedback: "
                f"{healing[:300]}"
            ),
            kind=memory_store.MemoryKind.FAILURE,
            scope=memory_store.scope_for(engine=EngineName.DEVELOPMENT),
            origin="development:heal_exhausted",
        )
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
        payload={
            "product_id": product_id,
            "idea_id": idea_id,
            "healing": healing[:6000],
            # Which kind of failure this is, so _patch can tell a test failure (worth
            # a surgical, in-place repair) from a dependency or deploy failure (which
            # an agent with no network structurally cannot fix).
            "category": category,
        },
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
