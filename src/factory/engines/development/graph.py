"""Development Engine runner.

Build → test in sandbox → Bug Council → deploy, with healing cycles in between.

Healing is re-enqueued as a new task rather than looped in-process. A three-hour
in-process retry loop would lose all its progress on a reboot; a task with an
incremented ``heal_attempts`` counter picks up exactly where it left off.

The product lock is held for the whole cycle, which is what pauses paid marketing
spend for that product until it is healthy again.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from factory import comms
from factory import issues as issues_mod
from factory.agents.roles import RoleName
from factory.agents.spawner import SpawnRequest, spawn
from factory.engines.development import (
    ab_testing,
    bug_council,
    contract,
    dependencies,
    design_brief,
    domain,
    preship,
    repair,
    seo,
)
from factory.engines.development import deploy as deploy_module
from factory.engines.development.gaps import find_requirement_gaps, record_gaps_as_issues
from factory.engines.development.release import decide_release
from factory.engines.development.requirements import (
    design_for_product,
    effective_design,
    render_for_prompt,
    requirements_for_product,
    write_landing_page_requirements,
)
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
from factory.llm.providers.base import LLMError
from factory.logging import get_logger
from factory.memory import store as memory_store
from factory.observability import costs
from factory.sandbox.runner import (
    DevOpsError,
    SandboxResult,
    SandboxUnavailable,
    is_devops_error,
    resolve_workspace,
)
from factory.settings import EngineName, Settings, TaskType, get_settings
from factory.state import repository
from factory.state.orm import ABTest, Idea, Product, Task, new_id
from factory.state.schemas import (
    CouncilResult,
    InterruptReason,
    ProductStatus,
    TaskKind,
    ValidatedIdea,
)
from factory.supervisor.concurrency import (
    LockUnavailable,
    defer_for_lock,
    product_lock,
)
from factory.supervisor.context import FactoryContext

log = get_logger(__name__)

#: Default only. The live value is `settings.max_heal_cycles` -- see its comment
#: in settings.py for why a counter is the wrong shape and what replaces it.
MAX_HEAL_CYCLES = 3

#: Failure kinds a surgical, in-place repair can actually address.
#:
#: `test_failure`: a real traceback to read and a suite to re-run -- the case the
#: whole approach is built around.
#: `guided`: a human has diagnosed the problem and said what to change, which is the
#: highest-information repair this factory ever performs.
#: `seo_incomplete`: a missing <title>/meta-description/sitemap.xml/robots.txt (see
#: seo.py) is a small, well-defined addition -- exactly what surgical repair is for,
#: and unlike a Council rejection the check is free to re-run after the edit.
#: `growth_seo`: GROWTH_ANALYST's own judgement that a LIVE product would benefit
#: from an organic-discoverability pass -- not a failure at all, but the same
#: "small, targeted, in-place edit" shape. Routed to SEO_OPTIMIZER instead of
#: WORKSPACE_SURGEON -- see `_patch_surgically`.
#: `dependency_not_on_pypi`: the one `dependency_*` category that *is* surgically
#: repairable, unlike the two below it. The edit is "delete or replace one line in
#: requirements.txt" -- no network needed to make it, only to have *diagnosed* it,
#: and that diagnosis (`dependencies.unresolvable_requirements`) re-runs in-process
#: on the next attempt rather than in the sandbox, exactly like `seo_incomplete`, so
#: the surgeon is never editing blind to a check it can't re-ask. A wrong
#: replacement simply heals again, bounded by `MAX_HEAL_CYCLES`.
#:
#: `council_rejection:*` (checked separately, see `_should_repair_surgically` --
#: not a member of this set because the category string is dynamic, e.g.
#: "council_rejection:pricing,security"): a targeted repair given Council's own
#: `healing.summary`/`ordered_steps` as guidance -- the same information a human
#: reading the escalation would get, and the exact reasoning that already made
#: `guided` reliable. The original exclusion here reasoned this would be
#: "editing blind to the only opinion that decides the outcome," but every
#: surgical repair already goes through the *same* full `_verify_and_ship`
#: (tests + a fresh Council review) afterward regardless of category -- a full
#: regeneration is exactly as blind to Council's live opinion mid-attempt as a
#: surgical one is. Found live: 4 separate products stuck on the identical,
#: narrowly-mechanical Council finding (wrong Stripe key env var name),
#: burning a full blind regeneration on each of 3 attempts before escalating,
#: when Council's own finding text already said precisely what to change.
#:
#: Everything else is deliberately absent. `dependency_timeout`/`dependency_failure`
#: cannot be fixed by an agent whose sandbox has no network -- an install that
#: failed for a *real* package needs to actually retry the install, not guess at an
#: edit; `deploy_failure` is not a workspace bug at all; `repair_rejected` (a prior
#: surgical attempt's own output already rejected as unusable) goes straight to a
#: full rebuild rather than surgically retrying against a workspace already shown
#: to fail the same way for the same reason.
#: `contract_incomplete`: a codegen contract fact the system prompt asked for but
#: the generated code doesn't actually satisfy (STRIPE_API_KEY or
#: PRODUCT_PRICE_CENTS never read, client_reference_id never set, the
#: beacon/interest URL never wired, or the privacy page missing/unlinked) -- see
#: contract.py. Same shape as `seo_incomplete`: a small, well-defined addition,
#: and the check is free and deterministic to re-run after the edit.
SURGICAL_CATEGORIES = frozenset(
    {
        "test_failure",
        "guided",
        "seo_incomplete",
        "growth_seo",
        "dependency_not_on_pypi",
        "contract_incomplete",
    }
)


async def run(ctx: FactoryContext, session: AsyncSession, task: Task) -> str:
    kind = TaskKind(task.kind)

    # EVALUATE_VALIDATION, EVALUATE_AB_TEST, GROWTH_REVIEW, ATTACH_DOMAIN and
    # VERIFY_DOMAIN are pure DB/API decisions -- no code generation, no sandbox
    # execution -- so none must be blocked by sandbox unavailability the way every
    # other Development task kind correctly is.
    if kind is TaskKind.EVALUATE_VALIDATION:
        return await _evaluate_validation(ctx, session, task)
    if kind is TaskKind.EVALUATE_AB_TEST:
        return await _evaluate_ab_test(ctx, session, task)
    if kind is TaskKind.GROWTH_REVIEW:
        return await _growth_review(ctx, session, task)
    if kind is TaskKind.ATTACH_DOMAIN:
        return await _attach_domain(ctx, session, task)
    if kind is TaskKind.VERIFY_DOMAIN:
        return await _verify_domain(ctx, session, task)
    if kind is TaskKind.GENERATE_DESIGN_BRIEF:
        return await _generate_design_brief(ctx, session, task)
    if kind is TaskKind.REVIEW_LIVE_PRODUCT:
        return await _review_live_product(ctx, session, task)
    if kind is TaskKind.ANSWER_QUESTION:
        # Another department is parked waiting on this. Above the sandbox check
        # because answering reads the record and touches no workspace -- refusing
        # to answer because Docker is down would leave the asker blocked on an
        # outage that has nothing to do with the question.
        from factory.answering import answer_question_task

        return await answer_question_task(ctx, session, task)

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
        self_serve_price_usd=row.self_serve_price_usd,
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
            first_requirements = await requirements_for_product(session, product_id)
            plan = await generate_scaffold(
                ctx,
                idea,
                product_id=product_id,
                task_id=task.id,
                requirements=(
                    render_for_prompt(first_requirements) if first_requirements else None
                ),
                design=effective_design(await design_for_product(session, product_id)),
            )
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
            # The transition out of validation, recorded where it actually happens.
            await repository.set_build_kind(session, product_id, "mvp")
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
        return await defer_for_lock(
            session, task_id=task.id, product_id=product_id, engine=EngineName.DEVELOPMENT
        )


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
            # Checked fresh on every (re)generation, not threaded through the task
            # payload -- a repair pass mid-test must still regenerate both variants
            # correctly without needing to already know a test was running.
            running_test = await repository.get_running_ab_test(session, product_id)
            ab_variants = (
                (running_test.variant_a_brief, running_test.variant_b_brief)
                if running_test is not None
                else None
            )
            # Written before the build, not after: this is the standard the
            # builder works to, the reviewer judges against, and the release
            # decision weighs. Generated once per product and superseded rather
            # than rewritten, so a regeneration is held to the same bar as the
            # build it replaces.
            requirements = await write_landing_page_requirements(
                ctx, session, product_id=product_id, idea=idea, task_id=task.id
            )
            if requirements is None:
                requirements = await requirements_for_product(session, product_id)

            plan = await generate_landing_page(
                ctx,
                idea,
                product_id=product_id,
                system=prompt_text,
                tunable_parameter_id=prompt_version,
                task_id=task.id,
                ab_variants=ab_variants,
                requirements=render_for_prompt(requirements) if requirements else None,
                design=await design_for_product(session, product_id),
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
            )
    except LockUnavailable:
        return await defer_for_lock(
            session, task_id=task.id, product_id=product_id, engine=EngineName.DEVELOPMENT
        )


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

    # A response rate this gate would otherwise trust as broad real interest can
    # still be one submitter clicking through several times -- proven live, see
    # DEPLOYMENT_CHECKLIST.md's signal-verification entry. Checked only once
    # there's enough confirmed volume for concentration to mean anything; five
    # confirmed signals from five different people is exactly what this gate
    # exists to let through, not something to second-guess.
    if passed:
        max_from_one_ip, confirmed_total = await repository.validation_signal_ip_concentration(
            session, product_id
        )
        if (
            confirmed_total >= 5
            and max_from_one_ip / confirmed_total > ctx.settings.validation_max_ip_concentration
        ):
            passed = False
            await interrupts.request(
                session,
                reason=InterruptReason.DATA_INTEGRITY,
                subject=f"{product.name[:80]} validation looks concentrated, not broad",
                detail=(
                    f"{max_from_one_ip} of {confirmed_total} confirmed interest signals for "
                    f"'{product.name}' came from the same submitter IP -- "
                    f"{max_from_one_ip / confirmed_total:.0%}, above the "
                    f"{ctx.settings.validation_max_ip_concentration:.0%} bar this gate treats as "
                    "broad real interest. The raw response rate cleared the normal threshold, "
                    "but this does not look like independent demand, so the product was "
                    "retired rather than built."
                ),
                headline="📋 Validation signals look concentrated",
                reply_hint="No reply needed — informational. The product has already been retired.",
                needs_reply=False,
                product_id=product_id,
            )

    if product.landing_page_prompt_version:
        await learning_prompts.record_outcome(
            session,
            tunable_parameter_id=product.landing_page_prompt_version,
            success=passed,
            detail={"sent": sent, "signals": signals, "response_rate": response_rate},
            task_id=task.id,
        )

    # Written once, regardless of outcome, at Discovery's own scope -- not
    # product_id's. The product-scoped memory below (failure only) is filed
    # under `product:{id}`, a scope nothing ever reads again once this product
    # is retired a few lines later; Discovery reads `engine:discovery`, so a
    # lesson filed anywhere else is write-only. This is also the only place a
    # *pass* gets remembered at all -- before this, the factory only ever
    # learned from validation losses, never from what actually worked.
    idea = await session.get(Idea, product.idea_id)
    if idea is not None:
        await memory_store.remember(
            session,
            content=(
                f"Validation outcome for the niche '{idea.target_niche}': "
                f"{signals}/{sent} confirmed interest signals ({response_rate:.1%}). "
                f"{'Cleared' if passed else 'Did not clear'} the gate."
            ),
            kind=memory_store.MemoryKind.LESSON if passed else memory_store.MemoryKind.FAILURE,
            scope=memory_store.scope_for(engine=EngineName.DISCOVERY),
            origin="development:evaluate_validation",
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


async def start_ab_test(
    session: AsyncSession,
    *,
    product_id: str,
    idea_id: str,
    variant_a_brief: str,
    variant_b_brief: str,
) -> str | None:
    """Start a live split test and queue the regeneration that renders it.

    Returns the new `ABTest` id, or `None` if this product already has one
    running (`create_ab_test`'s partial-unique-index no-op) -- the caller decides
    what to do with that, this function does not raise on it.

    The public entry point growth roles (Phase 5) use to act on their own
    judgment: this is the one function that actually mutates anything, so a
    read-only role can safely be handed it as its only "write" capability.
    """
    ab_test_id = await repository.create_ab_test(
        session,
        product_id=product_id,
        variant_a_brief=variant_a_brief,
        variant_b_brief=variant_b_brief,
    )
    if ab_test_id is None:
        return None
    # A fresh idempotency key, not the original "validate:{idea_id}" -- that one
    # is already consumed by the first landing-page build, and ON CONFLICT DO
    # NOTHING would silently no-op this regeneration otherwise.
    await repository.enqueue_task(
        session,
        kind=TaskKind.BUILD_LANDING_PAGE,
        engine=EngineName.DEVELOPMENT,
        idempotency_key=f"validate:{idea_id}:ab:{ab_test_id}",
        payload={"idea_id": idea_id, "product_id": product_id},
        idea_id=idea_id,
        product_id=product_id,
        priority=90,
    )
    log.info("development.ab_test_started", product_id=product_id, ab_test_id=ab_test_id)
    return ab_test_id


async def _evaluate_ab_test(ctx: FactoryContext, session: AsyncSession, task: Task) -> str:
    """Periodic, deterministic check on one running AB test (see `scheduler.py`'s
    every-tick sweep) -- concludes it once either arm is significantly ahead or its
    time budget runs out. Never spawns a model call; see `ab_testing.py`.
    """
    ab_test_id = str(task.payload.get("ab_test_id", ""))
    ab_test = await session.get(ABTest, ab_test_id) if ab_test_id else None
    if ab_test is None or ab_test.status != "running":
        return f"development: AB test {ab_test_id!r} not running, nothing to evaluate"

    outcomes = await repository.tally_ab_test_outcomes(
        session, ab_test.product_id, since=ab_test.started_at
    )
    a_views, a_signals = outcomes["a"]
    b_views, b_signals = outcomes["b"]
    verdict = ab_testing.evaluate_ab_test(
        a_views=a_views,
        a_signals=a_signals,
        b_views=b_views,
        b_signals=b_signals,
        min_views_per_arm=ctx.settings.ab_test_min_views_per_arm,
        alpha=ctx.settings.ab_test_alpha,
    )

    age_days = (datetime.now(UTC) - ab_test.started_at).days
    expired = age_days >= ctx.settings.ab_test_max_days
    if verdict == "inconclusive" and not expired:
        return (
            f"development: AB test {ab_test_id} still running "
            f"(a={a_signals}/{a_views}, b={b_signals}/{b_views})"
        )

    await repository.conclude_ab_test(session, ab_test_id, winner=verdict)
    inconclusive_and_expired = expired and verdict == "inconclusive"
    await memory_store.remember(
        session,
        content=(
            f"AB test on product {ab_test.product_id}: variant a got "
            f"{a_signals}/{a_views} signals, variant b got {b_signals}/{b_views}. "
            f"Winner: {verdict}"
            + (" (time budget expired before significance)" if inconclusive_and_expired else "")
        ),
        kind=memory_store.MemoryKind.LESSON,
        scope=memory_store.scope_for(product_id=ab_test.product_id),
        origin="development:evaluate_ab_test",
    )
    log.info(
        "development.ab_test_concluded",
        product_id=ab_test.product_id,
        ab_test_id=ab_test_id,
        winner=verdict,
        a=f"{a_signals}/{a_views}",
        b=f"{b_signals}/{b_views}",
    )
    return f"development: AB test {ab_test_id} concluded, winner={verdict}"


async def _growth_review(ctx: FactoryContext, session: AsyncSession, task: Task) -> str:
    """Spawn GROWTH_ANALYST for one product -- see `scheduler.py`'s periodic
    backstop sweep (`products_needing_growth_review`) and `agents/roles.py`.

    The Orchestrator can also reach GROWTH_ANALYST directly via SPAWN_AGENT
    (it is in the catalogue); this is the reliability net so a quiet product
    still gets looked at even if the Orchestrator's own judgement never picks
    it. Marked reviewed regardless of what the agent decided to do -- "this
    product looks fine, no action taken" is a complete, legitimate outcome, and
    the point of the timestamp is only to stop the same product being reviewed
    again tomorrow, not to track whether anything changed.
    """
    product_id = task.product_id or str(task.payload.get("product_id", ""))
    product = await session.get(Product, product_id) if product_id else None
    if product is None:
        return f"development: product {product_id!r} not found for growth review"

    objective = f"Review product {product_id} ({product.name}) and act if the data justifies it."
    result = await spawn(
        ctx,
        session,
        SpawnRequest(
            role=RoleName.GROWTH_ANALYST,
            objective=objective,
            engine=EngineName.DEVELOPMENT,
            product_id=product_id,
            task_id=task.id,
        ),
    )
    await repository.set_growth_reviewed(session, product_id)
    log.info("development.growth_reviewed", product_id=product_id, outcome=result.summary())
    return f"development/growth_review: {result.summary()}"


_REVIEW_VERDICT_SYSTEM = """\
You read a QA engineer's account of testing a live web page and extract what they \
actually concluded.

Set defect_found=true only for something genuinely wrong with the product: the main \
action does not work (a form that fired no request, or got a 4xx/5xx), a date, year, \
price or claim that is wrong or stale, placeholder or unfinished content, a dead \
link, a self-contradiction. Set it false for taste, phrasing, a missing feature \
nobody promised, or a reviewer who simply ran out of steps without establishing \
anything -- "I could not finish checking" is not a defect.

When true, write summary and details for an engineer who cannot see the page: what \
was done, what happened, what should have happened. Use only what the reviewer \
actually reported -- never invent a symptom, a status code, or a cause they did not \
state.

Length matters: keep summary to one sentence under 200 characters, and details \
under 1500. Be specific but do not restate the reviewer's whole transcript -- the \
engineer needs the finding, not the narration.

Also set:
- location: where the defect lives, as precisely as the reviewer reported it -- an \
endpoint like "POST /api/billing/checkout", a selector like "#subscribe-btn", or a \
file:line. This is how the same defect found again next week is recognised as the \
same one instead of being filed a second time, so prefer the exact string the \
reviewer used over a description of it. Leave empty only if they truly named none.
- kind: bug, ui, content, regression, improvement, or feature_request.
- severity: critical, high, medium, or low. Critical means the product cannot be \
paid for or cannot be used at all.\
"""


async def _record_council_findings(
    session: AsyncSession, product_id: str, council: CouncilResult
) -> int:
    """File every Bug Council finding as an issue. Returns how many were recorded.

    This is what makes a severity-gated deploy gate safe. Letting a medium finding
    through is only defensible if the finding survives as tracked work rather than
    as a log line in a build that shipped; without this, "ship it and track it"
    quietly becomes "ship it and forget".

    `record_issue` deduplicates, so a finding the Council raises on every review --
    which is most of them, since the code stays the same until someone fixes it --
    accumulates `times_seen` on one row instead of filing a fresh copy each build.
    That count is itself the signal a manager wants: *this has been flagged on
    fourteen consecutive reviews and nobody has touched it.*
    """
    recorded = 0
    for finding in council.blocking:
        _, is_new = await repository.record_issue(
            session,
            product_id=product_id,
            # Council findings are code-quality defects, not the user-visible kind
            # AUDITOR files; `bug` is the honest bucket for all six categories.
            kind="bug",
            severity=str(finding.severity).lower(),
            title=finding.summary,
            # `file:line` is a genuinely stable location, which makes the identity
            # far sharper than anything derived from a reviewer's prose.
            location=f"{finding.file}:{finding.line or 0}",
            expected=finding.remediation,
            actual=finding.failure_scenario,
            found_by=f"bug_council:{finding.category}",
        )
        recorded += 1
        if is_new:
            log.info(
                "development.council_finding_filed",
                product_id=product_id,
                category=str(finding.category),
                severity=str(finding.severity),
            )
    return recorded


async def _ensure_deps_for_repair(
    ctx: FactoryContext, workspace: str, *, product_id: str
) -> None:
    """Make sure the repair agent has a toolchain before it is told it has one.

    Moving the `.deps` clear to install time is not sufficient on its own: a
    workspace that was regenerated (`clear_workspace` + `write_plan`) has no `.deps`
    at all until a verification run installs one, so the first repair after a
    regeneration still met an empty environment. Observed live -- a product's
    workspace had been rewritten hours earlier and every repair since found no
    pytest.

    Best-effort by design. A failure here is not a repair failure: the agent simply
    works without being able to run the suite, exactly as it did before, and
    `_verify_and_ship` remains the authoritative gate either way. So this never
    raises and never consumes a heal cycle.
    """
    deps = resolve_workspace(workspace, ctx.settings) / ".deps"
    if deps.exists():
        return
    if not ctx.sandbox.available:
        return
    log.info("development.repair_deps_missing", product_id=product_id, workspace=workspace)
    try:
        result = await ctx.sandbox.execute(
            "python -m pip install --quiet --no-input --target=/workspace/.deps "
            "-r requirements.txt",
            workspace=workspace,
            network=True,
        )
    except Exception as exc:  # never turn an environment hiccup into a repair failure
        log.warning("development.repair_deps_install_failed", detail=str(exc)[:200])
        return
    log.info(
        "development.repair_deps_installed",
        product_id=product_id,
        exit_code=result.exit_code,
        timed_out=result.timed_out,
    )


async def _review_live_product(ctx: FactoryContext, session: AsyncSession, task: Task) -> str:
    """Walk one LIVE product with a real browser and commission a fix for anything
    genuinely wrong -- the Development department's standing product oversight.

    Why this exists alongside the deterministic `audit-live-products` slug rather
    than replacing it: that check answers "does the URL still answer" (cheaply,
    reliably, with no LLM call) and then hands the *markup* to a reviewer. Neither
    half ever presses a button. A live product shipped with a signup form that
    silently fails on submit, and a hardcoded year that had since gone stale, and
    both checks called it healthy for three weeks. Reading a page cannot find that;
    using it can.

    Constructed directly rather than through `spawn()` -- like `repair.py` does for
    WORKSPACE_SURGEON -- because AUDITOR's tools only exist in a private per-run
    registry: `BrowserSession` holds the live page across steps, and the report tool
    is pinned to this product so a model-supplied id cannot redirect a patch at a
    different one. See CALL_SITE_SCOPED_ROLES.
    """
    from factory.agents.roles import categories_for, get_role
    from factory.agents.runtime import AgentRuntime
    from factory.tools.browser import BrowserSession, allowed_origins_for
    from factory.tools.handbook import build_handbook_tools
    from factory.tools.product_review import ReviewVerdict, queue_review_patch

    product_id = task.product_id or str(task.payload.get("product_id", ""))
    product = await session.get(Product, product_id) if product_id else None
    if product is None:
        return f"development: product {product_id!r} not found for live review"
    if not product.deploy_url:
        return f"development: product {product_id} has no deploy_url to review"

    origins = allowed_origins_for(product.deploy_url, product.custom_domain)
    if not origins:
        return f"development: product {product_id} has no reviewable origin"

    role = get_role(RoleName.AUDITOR)
    objective = (
        f"Review the live product at {product.deploy_url}.\n\n"
        f"It is meant to be: {product.name}.\n\n"
        "Open it, work out what it is offering, and then actually try to do that "
        "thing yourself. Report anything genuinely wrong; if it is fine, say so.\n\n"
        "If it asks for an email address, submit exactly this one: "
        f"{preship.SELF_TEST_EMAIL}\n"
        "It is a reserved self-test address -- the endpoint answers it exactly like "
        "a real submission but records nothing and mails nothing. Using any other "
        "address files a fake signup against the very demand signal this product "
        "exists to measure, and a review that corrupts what it is measuring is "
        "worse than no review. Observed: a run invented "
        "`qa.tester.lead@example-agency.test` and it landed in the real table."
    )

    # The standard, if anyone wrote one. Without it a reviewer can only judge the
    # product against itself -- which finds things that look wrong but never things
    # that are missing, because it has no idea what was supposed to be there.
    requirements = await requirements_for_product(session, product_id)
    if requirements is not None:
        objective += (
            "\n\nThis product was built to meet the criteria below. Check it against "
            "them specifically -- a MUST that is not met is a real finding even if "
            "nothing looks broken, and anything listed as out of scope is absent on "
            "purpose and is not a defect.\n\n" + render_for_prompt(requirements)
        )
    recalled = await memory_store.recall_block(
        session,
        query=f"{product.name} live page defect",
        scopes=memory_store.scopes_for(engine=EngineName.DEVELOPMENT, product_id=product_id),
    )

    # Captured before the agent runs: anything it re-reports bumps that issue's
    # `last_seen_at` past this mark, which is how verification tells "looked and did
    # not find it" from "did not look".
    review_started_at = datetime.now(UTC)

    async with BrowserSession(allowed_origins=origins) as tools:
        runtime = AgentRuntime(
            ctx.llm,
            settings=ctx.settings,
            # A QA engineer who does not know what the product is *for* can only
            # report that a thing is broken, never that a working thing is the
            # wrong thing. The dossier is what makes the second judgement possible,
            # and it is the judgement this role exists for.
            tools=build_handbook_tools(
                role=role.name.value,
                task_id=task.id,
                product_id=product_id,
                engine=EngineName.DEVELOPMENT,
                base=tools,
            ),
        )
        result = await runtime.run(
            objective=objective,
            system=role.system_prompt,
            tool_categories=categories_for(role),
            task_type=role.task_type,
            engine=EngineName.DEVELOPMENT,
            task_id=task.id,
            max_steps=role.max_steps,
            deadline_seconds=role.deadline_seconds,
            docker_available=ctx.docker_available,
            context=recalled,
            cost_cap_usd=role.cost_cap_usd,
            max_context_chars=role.max_context_chars,
        )

    # The verdict is extracted, never self-reported. Both of this feature's first
    # live runs exhausted their step budget mid-investigation and never called the
    # reporting tool they had, so a real review was performed and then discarded --
    # and because neither emitted a final text turn, `result.answer` was empty too.
    # Extracting from whatever the agent did say, always, removes the dependence on
    # it choosing to stop and file: the same investigate-then-extract split
    # `learning/maintenance.py` uses on the proposal path.
    if not result.answer.strip():
        log.info("development.live_review_empty", product_id=product_id, outcome=result.summary())
        return f"development/review_live_product: {result.summary()}, no verdict to extract"

    try:
        verdict = await ctx.llm.parse(
            ReviewVerdict,
            task_type=TaskType.ANALYSIS,
            system=_REVIEW_VERDICT_SYSTEM,
            prompt=(
                "The reviewer's own account of what it did and found:\n\n"
                f"{result.answer[:4000]}"
            ),
            engine=EngineName.DEVELOPMENT,
            task_id=task.id,
        )
    except LLMError as exc:
        # A provider failure here must not look like a clean bill of health.
        log.warning("development.live_review_verdict_failed", detail=str(exc)[:200])
        return f"development/review_live_product: {result.summary()}, verdict extraction failed"

    patch_id, reason = await queue_review_patch(
        session, product_id=product_id, verdict=verdict, dry_run=ctx.dry_run
    )

    # Closure, and the only place it happens. An issue previously marked `fixed`
    # that this review did *not* re-report is now verified gone and closed; one
    # still being reported stays open however many patches it has had.
    #
    # A patch completing only ever means work happened. Treating that as closure is
    # what let a broken checkout be "fixed" four times and still be broken on the
    # fifth review, 24 days after it was first found.
    closed = await repository.verify_unseen_issues(
        session,
        product_id,
        review_started_at=review_started_at,
        reviewed_by=f"auditor:{task.id}",
    )
    if closed:
        log.info("development.issues_verified_closed", product_id=product_id, closed=closed)

    # Recorded either way: "this looked fine on <date>" is exactly the prior the
    # next review should start from, and without it every run re-derives the same
    # judgement from scratch.
    await memory_store.remember(
        session,
        content=f"Live review of {product.name}: {result.answer[:1500]}",
        kind=(
            memory_store.MemoryKind.FAILURE
            if verdict.defect_found
            else memory_store.MemoryKind.FACT
        ),
        scope=memory_store.scope_for(product_id=product_id),
        source_task_id=task.id,
        origin="agent:auditor",
    )

    log.info(
        "development.live_reviewed",
        product_id=product_id,
        outcome=result.summary(),
        defect_found=verdict.defect_found,
        patch_task_id=patch_id,
        patch_reason=reason,
    )
    return f"development/review_live_product: {result.summary()}, verdict: {reason}"


async def _attach_domain(ctx: FactoryContext, session: AsyncSession, task: Task) -> str:
    """Attach a human-supplied hostname to a product's Vercel project and relay
    the required DNS records back -- see `development/domain.py` and
    `hitl/interrupts.py::_act_on_domain_reply`, which enqueues this task."""
    product_id = task.product_id or str(task.payload.get("product_id", ""))
    hostname = str(task.payload.get("hostname", ""))
    product = await session.get(Product, product_id) if product_id else None
    if product is None or not hostname:
        return f"development: cannot attach domain -- product {product_id!r} or hostname missing"
    if not product.vercel_project:
        return (
            f"development: product {product_id} has no recorded vercel_project, "
            "cannot attach a domain"
        )

    result = await domain.attach(ctx.settings, project=product.vercel_project, hostname=hostname)
    if not result.ok and result.is_infra:
        # Same reasoning as deploy_module's own infra failures -- never the fault
        # of the generated product, never worth a heal cycle.
        raise DevOpsError(f"Domain attachment failed with an infrastructure error: {result.detail}")
    if not result.ok:
        log.warning("development.domain_attach_failed", product_id=product_id, detail=result.detail)
        return f"development: domain attachment for {product_id} failed: {result.detail}"

    status = "verified" if result.verified else "attached"
    extra = {"deploy_url": f"https://{hostname}"} if result.verified else {}
    await repository.set_domain_status(session, product_id, status, **extra)
    if ctx.notifier.available:
        await ctx.notifier.send(
            f"Domain {hostname} attached to {product.name}.\n\n"
            + domain.format_dns_instructions(hostname, result.records)
        )
    log.info(
        "development.domain_attached",
        product_id=product_id,
        hostname=hostname,
        verified=result.verified,
    )
    return f"development: domain {hostname} attached to {product_id} (verified={result.verified})"


async def _verify_domain(ctx: FactoryContext, session: AsyncSession, task: Task) -> str:
    """Periodic, deterministic check on one attached-but-unverified domain (see
    `scheduler.py`'s sweep) -- confirms it once Vercel reports the DNS is in
    place. Never spawns a model call.
    """
    product_id = task.product_id or str(task.payload.get("product_id", ""))
    product = await session.get(Product, product_id) if product_id else None
    if (
        product is None
        or product.domain_status != "attached"
        or not product.custom_domain
        or not product.vercel_project
    ):
        return f"development: domain for {product_id!r} not awaiting verification"

    verified = await domain.check_verified(
        ctx.settings, project=product.vercel_project, hostname=product.custom_domain
    )
    if verified is None:
        return f"development: could not check domain verification for {product_id} this pass"
    if not verified:
        return f"development: {product.custom_domain} for {product_id} still not verified"

    await repository.set_domain_status(
        session, product_id, "verified", deploy_url=f"https://{product.custom_domain}"
    )
    if ctx.notifier.available:
        await ctx.notifier.send(
            f"{product.custom_domain} is now verified and live for {product.name}."
        )
    log.info("development.domain_verified", product_id=product_id, hostname=product.custom_domain)
    return f"development: {product.custom_domain} verified for {product_id}"


async def _generate_design_brief(ctx: FactoryContext, session: AsyncSession, task: Task) -> str:
    """The dashboard-triggered path: `api/dashboard.py` only ever enqueues this
    task (it never calls an LLM itself, see `design_brief.py`'s own module
    docstring), this is where the real work actually happens. Backfills a
    product that went live before this feature existed; a product built after
    it already got one automatically in `_verify_and_ship`, so this is a no-op
    there (`build_design_brief`'s own cache check)."""
    product_id = task.product_id or str(task.payload.get("product_id", ""))
    product = await session.get(Product, product_id) if product_id else None
    if product is None or not product.deploy_url:
        return f"development: product {product_id!r} has no live deploy_url to describe"

    brief = await design_brief.build_design_brief(
        ctx, session, product_id=product_id, deploy_url=product.deploy_url, task_id=task.id
    )
    if brief is None:
        return f"development: design brief for {product_id} could not be generated this pass"
    return f"development: design brief generated for {product_id}"


async def _should_repair_surgically(
    ctx: FactoryContext, session: AsyncSession, product: Product, category: str
) -> bool:
    """Whether this cycle edits the workspace in place instead of regenerating it.

    Every condition is a case where a surgical repair is either impossible or worse
    than a clean rebuild -- the fallback is always today's full regeneration, which
    remains the only operation that can recover an arbitrarily damaged workspace.
    """
    if category not in SURGICAL_CATEGORIES and not category.startswith("council_rejection:"):
        # A dependency failure cannot be fixed by an agent whose sandbox has no
        # network and therefore cannot run pip at all; a deploy failure is not a
        # workspace bug. `council_rejection:*` is checked separately -- see
        # SURGICAL_CATEGORIES' own docstring for why it's eligible despite not
        # being a literal member (the category string is dynamic).
        return False
    if not product.test_command:
        # Built before test_command was recorded. Guessing a default would have the
        # agent optimising against a different suite than the gate runs -- a repair
        # that looks successful right up until verification.
        return False
    if category != "guided" and product.heal_attempts >= ctx.settings.max_heal_cycles - 1:
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

    # If this task was parked on a question, it is resuming because somebody
    # answered. Without this the channel is decorative: the work would be released
    # and then re-run with exactly the information that blocked it the first time,
    # and it would block again on the same thing.
    answered = await comms.answers_for(session, task.id)
    if answered:
        healing = f"{answered}\n\n{healing}"
        log.info("development.resumed_with_answers", product_id=product_id)
    category = str(task.payload.get("category", ""))

    try:
        async with product_lock(
            session,
            product_id=product_id,
            holder=EngineName.DEVELOPMENT,
            reason="patching product",
        ):
            # A discretionary growth-motivated pass (GROWTH_ANALYST's own judgement,
            # not a real failure) must never fall through to a full regeneration --
            # unlike every other category here, "not safe for a surgical repair right
            # now" means "skip it, try again another time," not "rebuild the product
            # from scratch over an optional optimisation." Checked before the status
            # flip below, so a skip leaves the live product completely untouched.
            surgical_ok = await _should_repair_surgically(ctx, session, product, category)
            if category == "growth_seo" and not surgical_ok:
                log.info("development.growth_seo_skipped", product_id=product_id)
                return (
                    f"development: growth SEO pass for {product_id} skipped "
                    "(not safe for a discretionary edit right now)"
                )

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

            if surgical_ok:
                return await _patch_surgically(
                    ctx,
                    session,
                    task,
                    product=product,
                    workspace=workspace,
                    healing=healing,
                    recalled=recalled,
                    category=category,
                )

            existing = read_workspace(workspace, ctx.settings)
            repairs = f"REQUIRED REPAIRS (from the previous review):\n{healing}"
            if recalled:
                repairs = f"{repairs}\n\n{recalled}"
            briefed = idea.model_copy(
                update={"problem_statement": f"{idea.problem_statement}\n\n{repairs}"}
            )
            design = await design_for_product(session, product_id)
            requirements = await requirements_for_product(session, product_id)

            # **Regenerate the kind of thing this product actually is.** This branch
            # called `generate_scaffold` unconditionally, and that prompt mandates a
            # FastAPI backend, token auth with password hashing, and Stripe Checkout.
            # So any full repair of a *validation page* -- whose entire job is to
            # collect one email address -- rebuilt it as a complete SaaS MVP. The
            # agent's own note on the last one reads "Repaired the landing page and
            # added the complete FastAPI MVP... token auth with scrypt password
            # hashing... and Stripe Checkout", and the release judgement then
            # correctly refused a pitch page carrying an unauthenticated account
            # system.
            #
            # The same bug was already found and fixed once, on the other side: see
            # `_verify_and_ship`'s `is_validation`, whose comment ends "The product
            # became an MVP because this gate kept insisting it was one." The *gate*
            # was taught to read `build_kind`. The *generator* was not, so the bug
            # survived in its other half and produced exactly the symptom that note
            # predicted.
            if await repository.get_build_kind(session, product_id) == "validation":
                prompt_text, prompt_version = await learning_prompts.get_prompt(
                    session, key=_LANDING_PAGE_KEY, default=LANDING_PAGE_SYSTEM
                )
                plan = await generate_landing_page(
                    ctx,
                    briefed,
                    product_id=product_id,
                    system=prompt_text,
                    tunable_parameter_id=prompt_version,
                    task_id=task.id,
                    existing_files=existing,
                    requirements=render_for_prompt(requirements) if requirements else None,
                    design=design,
                )
            else:
                plan = await generate_scaffold(
                    ctx,
                    briefed,
                    product_id=product_id,
                    task_id=task.id,
                    existing_files=existing,
                    requirements=render_for_prompt(requirements) if requirements else None,
                    design=effective_design(design),
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
        return await defer_for_lock(
            session, task_id=task.id, product_id=product_id, engine=EngineName.DEVELOPMENT
        )


def _workspace_differs_from_deployment(
    ctx: FactoryContext, product: Product, workspace: str
) -> bool:
    """Does this workspace hold anything the customer has not been given?

    Asked, not remembered. The digest of the last successful deploy is on the
    product; hashing the workspace the same way and comparing answers the question
    from reality on both sides, so there is no flag for two code paths to keep
    honest between them.

    `workspace_undeployed` remains the fallback for products deployed before the
    digest existed, where the honest answer is that nobody knows -- and that is the
    case this was needed for: a workspace genuinely holding an unshipped fix
    reported `false`, because the column was added after the edits were made.

    Degrades to the flag on any failure. A workspace that cannot be read is not
    evidence that it matches what is live.
    """
    if not product.deployed_content_hash:
        return bool(product.workspace_undeployed)
    try:
        current = deploy_module.workspace_digest(workspace, ctx.settings)
    except OSError as exc:
        log.warning("development.workspace_digest_failed", detail=str(exc)[:200])
        return bool(product.workspace_undeployed)
    return current != product.deployed_content_hash


async def _patch_surgically(
    ctx: FactoryContext,
    session: AsyncSession,
    task: Task,
    *,
    product: Product,
    workspace: str,
    healing: str,
    recalled: str,
    category: str,
) -> str:
    """Repair by editing the workspace in place, then verify exactly as usual.

    Called with `product_lock` already held by `_patch`, which is what makes the
    workspace tools safe to use without taking it themselves.
    """
    product_id = product.id
    test_command = product.test_command or ""

    # `.deps` is deliberately NOT cleared here any more -- that moved to just
    # before `_verify_and_ship`'s install, which is the only place it matters.
    #
    # Clearing it here stripped the agent's toolchain moments before telling it
    # "dependencies are already installed; the test command sees them" (its own
    # operating rules) and forbidding `pip install` (no network, by design). The
    # agent then found no pytest, could not install it, and spent its remaining
    # budget searching the filesystem -- `find / -name 'pytest*'`, `ls -la
    # /workspace/.deps`. Every repair on every product was affected.
    #
    # The reason for clearing is real but belongs at install time: pip with
    # `--target` and no `--upgrade` skips files already present, so a stale tree
    # would silently swallow a genuine requirements.txt bump. That is a property of
    # the install, not of the repair, so it is enforced there.
    await _ensure_deps_for_repair(ctx, workspace, product_id=product_id)
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
            category=category,
        )
    except repair.RepairBlocked:
        # Never leave a half-edited workspace behind, and never charge this to the
        # product: RepairBlocked means the attempt was cut short by an outage or an
        # exhausted budget, which says nothing about whether the code is fixable.
        # `_run_engine` defers the task instead of counting an attempt.
        repair.restore_snapshot(workspace, ctx.settings)
        raise

    if outcome.no_change_needed:
        # The agent investigated and concluded the brief was wrong. That ends the
        # work: spending the remaining heal budget trying to make a false premise
        # true is exactly what this used to do, because usability was
        # `bool(changed_files)` and a reasoned refusal looked like a failed attempt.
        repair.restore_snapshot(workspace, ctx.settings)

        # The agent judged the *workspace*, and the brief described the *deployed
        # page*. Those are the same thing only while every workspace change has
        # shipped, and they diverge exactly when a repair applied a fix that then
        # failed to deploy -- which is when this path is most likely to be reached,
        # because the fix really is sitting there in the files.
        #
        # Observed live: a repair fixed two defects in the workspace, the verify
        # step escalated before deploying, and the next repair looked at the same
        # workspace, correctly saw the work already done, and closed both issues as
        # invalid while the defects were still on the page in front of visitors.
        # "Already fixed here" is not "fixed for the customer".
        if _workspace_differs_from_deployment(ctx, product, workspace):
            log.info("development.repair_already_applied", product_id=product_id)
            return await _verify_and_ship(
                ctx,
                session,
                product_id=product_id,
                product_name=product.name,
                workspace=workspace,
                test_command=test_command,
                idea_id=product.idea_id,
                task_id=task.id,
            )

        # Status is restored, never forced. Promoting a VALIDATING landing page to
        # LIVE here treated a pitch page as a shipped product -- starting outreach
        # against it and taking it out of the validation gate that decides whether
        # it is worth building at all.
        await repository.set_product_status(session, product_id, ProductStatus(product.status))
        await repository.release_product_lock(session, product_id)
        # Close what commissioned this. An agent that refuses a false brief without
        # the issue behind it closing is a loop with extra steps: the next review
        # sees the same open issue and commissions the same wrong work again.
        closed = await repository.close_issues_as_invalid(
            session, task_id=task.id, reason=outcome.answer[:1000]
        )
        log.info(
            "development.repair_not_needed",
            product_id=product_id,
            issues_closed=closed,
            detail=outcome.summary[:200],
        )
        return f"development: no change needed for {product_id} -- {outcome.answer[:300]}"

    if outcome.blocked_detail:
        # The agent said it could not proceed, and said why. Until now that was
        # parsed, logged, and branched on by nothing -- so it was handed another
        # retry with exactly the information that stopped it, and whatever it needed
        # to know was discarded.
        #
        # Asking is a state the work is in, not an action the agent had to remember
        # to take: it reports BLOCKED because its result contract requires one of
        # four words, and the routing happens here. The work parks rather than
        # failing, so this costs no heal budget -- being blocked on someone else is
        # not the same as failing.
        # Keep what it worked out before rolling back what it wrote. Parking used to
        # mean abandoning: the edits went, and so did the investigation that produced
        # them, so the resumed attempt re-read the codebase and re-derived the same
        # diagnosis at full price. The edits still go -- a half-finished repair must
        # not sit somewhere deployable -- but `.factory` is excluded from the
        # rollback by design and is read into the top of every repair prompt, so the
        # findings survive and are the first thing the next attempt sees.
        question_text, _, _ = comms.parse_blocked_report(outcome.blocked_detail)
        repair.record_blocked_attempt(
            workspace,
            ctx.settings,
            question=question_text or outcome.blocked_detail,
            found=outcome.answer or outcome.blocked_detail,
        )
        repair.restore_snapshot(workspace, ctx.settings)
        question = await comms.raise_blocked_question(
            session,
            asked_by=EngineName.DEVELOPMENT,
            detail=outcome.blocked_detail,
            blocking_task_id=task.id,
            asked_by_task_id=task.id,
            product_id=product_id,
            context=healing[:1500],
        )
        if question is not None:
            await comms.park_until_answered(
                session,
                task.id,
                reason=f"blocked on question {question.id} to {question.asked_of}",
            )
            await repository.release_product_lock(session, product_id)
            log.info(
                "development.blocked_on_question",
                product_id=product_id,
                question_id=question.id,
                asked_of=question.asked_of,
            )
            return (
                f"development: blocked on a question to {question.asked_of} "
                f"-- {question.question[:200]}"
            )
        # BLOCKED with nothing to ask about is just a failed attempt; fall through.

    if not outcome.usable:
        repair.restore_snapshot(workspace, ctx.settings)
        reason = outcome.rejected_because or "no usable repair"
        if category == "growth_seo":
            # A declined discretionary optimisation is not a product failure --
            # unlike every other rejection here, it must never enter the _heal()
            # escalation ladder: that increments heal_attempts (shrinking the
            # budget a REAL future bug fix gets to work with) and, on repeat,
            # would eventually page a human about what was only ever an SEO
            # experiment that did not pan out. Restore the product's own status
            # (whatever _patch found it in -- always LIVE in practice, since
            # request_seo_pass_action only ever fires on a LIVE product) and stop.
            await repository.set_product_status(session, product_id, ProductStatus.LIVE)
            log.info("development.growth_seo_declined", product_id=product_id, reason=reason)
            return f"development: growth SEO pass for {product_id} declined ({reason})"
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

    # Marked before verification, not after, because the interesting case is the one
    # where verification does *not* reach a deploy: the files are changed either way,
    # and it is exactly then that the workspace and the live page diverge. Cleared on
    # any successful ship.
    await repository.set_product_status(
        session, product_id, ProductStatus.PATCHING, workspace_undeployed=True
    )

    # The issues this repair was commissioned for have had their work done. `fixed`
    # is a claim, not a conclusion -- the next review either fails to find the defect
    # and closes it, or reports it again and reopens it -- and that distinction is
    # already why this status exists.
    #
    # It has to happen here rather than only after a successful deploy, because
    # `blocking_now` counts open issues and the wall check runs on the way to that
    # deploy. Left open, the count cannot fall on the attempt that actually fixed
    # something: observed as `repair.applied` followed four seconds later by
    # `heal_stopped blocking_now=2 blocking_before=2`, escalating the one attempt
    # that had worked.
    if task.id:
        await repository.mark_issue_fixed(
            session, task_id=task.id, resolution="Repaired in the workspace, pending verification"
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
) -> str:
    # Read from the product, never taken as a parameter. It *was* a parameter, and
    # two of this function's four call sites -- both repair paths -- never passed
    # it. So a landing page verified correctly when built, and the moment it needed
    # any repair it was re-verified as a full MVP: the codegen contract then
    # demanded STRIPE_API_KEY, PRODUCT_PRICE_CENTS and client_reference_id on a page
    # whose own requirements list payment as out of scope. The repair agent built
    # the checkout it was told to, that checkout 422'd because nothing was behind
    # it, and fixing that ran for 25 days. The product became an MVP because this
    # gate kept insisting it was one.
    is_validation = await repository.get_build_kind(session, product_id) == "validation"

    await repository.set_product_status(session, product_id, ProductStatus.REVIEWING)

    # Cheapest, earliest check, same reasoning as the SEO gate below: this runs
    # in-process against the real PyPI registry -- not in the sandbox, which has
    # no network at all before the install step turns it on -- so a hallucinated
    # or suspiciously-fresh package name is caught before ever paying for the
    # install attempt, let alone three Opus reviewers. See dependencies.py's
    # module docstring for why this specifically runs before install, not after.
    requirements_path = resolve_workspace(workspace, ctx.settings) / "requirements.txt"
    if requirements_path.exists():
        unresolvable = await dependencies.unresolvable_requirements(
            session, requirements_path.read_text(encoding="utf-8"), ctx.settings
        )
        if unresolvable:
            return await _heal(
                ctx,
                session,
                product_id,
                "requirements.txt names one or more packages that could not be "
                "verified against the real PyPI registry:\n"
                + "\n".join(f"- {item}" for item in unresolvable),
                idea_id,
                category="dependency_not_on_pypi",
            )

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
    # Cleared here, not before the repair agent ran: pip with `--target` and no
    # `--upgrade` skips files already present, so a stale tree would silently
    # swallow a genuine requirements.txt bump and the suite would fail identically
    # with nothing in the log explaining why. This is the only moment that matters,
    # and doing it earlier left the repair agent with no toolchain -- see
    # `_ensure_deps_for_repair`.
    repair.clear_installed_deps(workspace, ctx.settings)
    # **Prepare what this workspace declares, not what the pipeline assumes.**
    #
    # This ran unconditionally, so every product was treated as a Python project.
    # A validation page is static HTML with nothing to install, and pip answered the
    # only way it can: `Could not open requirements file`. That became
    # `category="dependency_failure"`, which is not surgical, so it triggered a full
    # regeneration -- which (until this file's `_patch` learned to read `build_kind`)
    # rebuilt the pitch page as a full SaaS MVP. A stack assumption four lines below
    # a guard that already knew better is what started that.
    #
    # Because the guard *was* already here: the PyPI pre-check above is wrapped in
    # `if requirements_path.exists()`. The code knew the file was optional and then
    # installed as though it were not.
    #
    # Deliberately keyed on the workspace's own contents rather than on `build_kind`.
    # "Is this a business validation or a real product" and "does this need Python
    # dependencies" are two different facts that happen to agree today, and holding
    # two facts as one is the failure this codebase keeps paying for. A workspace
    # that later declares a `package.json` slots in here by the same rule; one that
    # declares nothing has nothing to prepare, and that is a fact about the product
    # rather than a fault in it.
    if not requirements_path.exists():
        log.info("development.nothing_to_install", product_id=product_id, workspace=workspace)
        install = SandboxResult(exit_code=0, stdout="", stderr="", timed_out=False)
    else:
        install = await ctx.sandbox.execute(
            "python -m pip install --quiet --no-input "
            "--target=/workspace/.deps -r requirements.txt",
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
    # PRODUCT_ID/STRIPE_API_KEY/PRODUCT_PRICE_CENTS: every prompt tells the model to
    # read these "at request time" specifically so a lookup never fires during test
    # collection -- but that's a prompt convention, not a guarantee. A generated
    # test that *does* exercise the request path (a real checkout/attribution test,
    # exactly what a thorough test suite should have) would otherwise crash on
    # KeyError before ever reaching the contract/SEO checks below, which are
    # supposed to be the cheapest, earliest signal. Dummy values, not real
    # credentials -- these tests never make a real network call to Stripe.
    tests = await ctx.sandbox.execute(
        f'export PATH="/workspace/.deps/bin:$PATH"; {test_command}',
        workspace=workspace,
        env={
            "PYTHONPATH": "/workspace/.deps",
            "PRODUCT_ID": "test-product-id",
            "STRIPE_API_KEY": "sk_test_dummy",
            "PRODUCT_PRICE_CENTS": "4900",
        },
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

    # Cheapest, earliest check: no LLM call, so a missing <title> is caught before
    # ever paying for three Opus reviewers to review code that was going to heal
    # regardless. Deliberately not part of Bug Council's own gate -- see seo.py's
    # module docstring for why this is a separate, surgically-repairable category.
    missing_seo = seo.missing_seo_requirements(sources)
    # Neither of these heals on its own any more. They used to short-circuit, and
    # the cost was serial discovery: a missing <title> meant the contract gaps, the
    # Council findings and the requirement gaps were never even looked for. Fix the
    # title, rebuild, meet the next one, repeat. A human running CI gets every
    # failure at once; this now does too.
    #
    # What they lose is the veto, not the check. Both still run and both file
    # issues; the contract's environment facts still hold a release, below.
    missing_contract = contract.missing_contract_requirements(
        sources, product_id=product_id, is_validation=is_validation
    )
    for item in missing_seo:
        # Quality, not breakage. A page with no <title> is worse, not broken, and a
        # release judgement can weigh that against the cost of not shipping -- which
        # is exactly the trade `should` exists for.
        await repository.record_issue(
            session,
            product_id=product_id,
            kind="ui",
            severity="low",
            title=item,
            location=f"seo: {item[:120]}",
            found_by="seo_check",
        )
    for item in missing_contract:
        # Environment facts nothing else in the company knows: which exact key name
        # the deploy pipeline sets, which exact URL captured emails must reach.
        # Getting one wrong is silent, total breakage -- billing fails closed, or
        # addresses go nowhere -- so these stay high and still hold a release.
        await repository.record_issue(
            session,
            product_id=product_id,
            kind="bug",
            severity="high",
            title=item,
            location=f"contract: {item[:120]}",
            found_by="contract_check",
        )
    # Both checks are deterministic and examine everything every run, so anything
    # they reported before and do not report now is genuinely gone. Closing it is
    # what keeps the convergence signal honest: an issue filed `open` closes only if
    # something marked it `fixed` first, so without this a quietly-repaired defect
    # left its issue open forever and every later run read "not converging".
    await repository.close_resolved_issues(
        session,
        product_id=product_id,
        found_by="seo_check",
        still_present={
            issues_mod.fingerprint_for(
                product_id=product_id, kind="ui", location=f"seo: {item[:120]}", title=item
            )
            for item in missing_seo
        },
        verified_by="seo_check",
    )
    await repository.close_resolved_issues(
        session,
        product_id=product_id,
        found_by="contract_check",
        still_present={
            issues_mod.fingerprint_for(
                product_id=product_id, kind="bug", location=f"contract: {item[:120]}", title=item
            )
            for item in missing_contract
        },
        verified_by="contract_check",
    )
    if missing_seo or missing_contract:
        log.info(
            "development.gate_findings",
            product_id=product_id,
            seo=len(missing_seo),
            contract=len(missing_contract),
        )

    try:
        council = await bug_council.convene(ctx, sources, task_id=task_id)
    except bug_council.CouncilUnavailable as exc:
        # A reviewer that could not run rendered no verdict on the code at all --
        # never route this into a heal cycle, which would spend an LLM call
        # regenerating code that was never actually found bad, or even reviewed.
        raise DevOpsError(f"Bug Council could not complete review: {exc}") from exc
    # Every finding becomes an issue, whether or not it blocks. This runs *before*
    # the gate decision on purpose: a finding must be durably recorded before it can
    # be waved past, or "ship it and track it" degrades into "ship it and forget".
    recorded = await _record_council_findings(session, product_id, council)

    deploy_blocking = council.deploy_blocking
    # The contract's environment facts are a rail, not a review: a wrong key name
    # or a wrong capture URL is silent, total breakage. They lost the power to
    # short-circuit the rest of verification, not the power to stop a release.
    hard_blocked = bool(deploy_blocking or missing_contract)

    # The one comparison that asks whether this build does what it was for.
    # Everything above judges it against something else -- the codegen contract
    # against hardcoded environment facts, Bug Council against six fixed categories
    # -- and neither can read the requirements at all. A page can satisfy every one
    # of them and still tell its visitors "no tracking scripts" while running one.
    #
    # Gaps become issues on the same footing as any other finding, so the release
    # judgement below weighs them with everything else. What makes them different is
    # that they are finite: fixing a gap removes it, and when there are none the work
    # is done -- which "no outstanding findings" can never be.
    requirements = await requirements_for_product(session, product_id)
    if requirements is not None:
        report = await find_requirement_gaps(
            ctx,
            sources=sources,
            requirements=requirements,
            task_id=task_id,
            design=effective_design(await design_for_product(session, product_id)),
        )
        if report is not None:
            await record_gaps_as_issues(session, product_id, report)

    # The release judgement, where an `if` used to be. Whether this is good enough
    # to put in front of a real person is the highest-judgement moment in the
    # company, and it was a boolean -- which is what made the build a retry loop
    # rather than a process: every transition was automatic, so the system had one
    # move and made it until a counter ran out.
    product_row = await repository.get_product(session, product_id)
    heal_attempts_so_far = product_row.heal_attempts if product_row else 0
    decision = await decide_release(
        ctx,
        session,
        product_id=product_id,
        product_name=product_name,
        council=council,
        requirements=requirements,
        heal_attempts=heal_attempts_so_far,
        task_id=task_id,
    )

    if decision is not None and decision.decision in ("rethink", "abandon"):
        # Both say more patching will not help, which is a conclusion no amount of
        # repair budget reaches on its own -- and retiring a product is a human's
        # call, so this asks rather than acts.
        await interrupts.request(
            session,
            reason=InterruptReason.HEAL_EXHAUSTED,
            subject=f"{product_name[:80]} — {decision.decision}",
            detail=(
                f"The release decision was **{decision.decision}**, not a defect list.\n\n"
                f"{decision.rationale}\n\n"
                + (
                    "Requirements not met: " + "; ".join(decision.unmet_must)
                    if decision.unmet_must
                    else "No specific requirement was called out as unmet."
                )
            ),
            headline=f"\U0001f9ed Release decision: {decision.decision}",
            reply_hint=(
                "Reply with guidance to try again, or REJECT to retire this product."
            ),
            product_id=product_id,
        )
        await repository.set_product_status(session, product_id, ProductStatus.PLANNED)
        await repository.release_product_lock(session, product_id)
        return f"development: release decision for {product_id} was {decision.decision}"

    ships = decision is not None and decision.decision == "ship"
    if hard_blocked or (decision is not None and not ships):
        healing = council.healing
        summary = healing.summary if healing else "Bug Council rejected the build."
        steps = "\n".join(healing.ordered_steps) if healing else ""
        blocked_by = sorted({str(f.category) for f in deploy_blocking}) or (
            ["contract"] if missing_contract else ["release_decision"]
        )
        # The decision names the fewest changes that would make this shippable;
        # Council's own healing text is the fallback for when no judgement ran. This
        # is the triage step nothing previously did -- "fix all findings" is how a
        # product never ships.
        brief = (
            f"{decision.rationale}\n\nFix these, and only these:\n"
            + "\n".join(f"- {item}" for item in decision.fix_first)
            if decision is not None and decision.fix_first
            else f"{summary}\n{steps}"
        )
        # Everything found in one brief, not one gate's half. Previously the SEO and
        # contract checks each healed alone, so a repair only ever saw the first
        # thing that tripped and the rest were discovered a rebuild at a time.
        if missing_contract:
            brief = (
                "These are environment facts and they hold the release regardless of "
                "anything else here:\n"
                + "\n".join(f"- {item}" for item in missing_contract)
                + "\n\n"
                + brief
            )
        log.info(
            "development.release_blocked",
            product_id=product_id,
            decision=decision.decision if decision else "none",
            blocking=len(deploy_blocking),
            recorded_total=recorded,
            categories=blocked_by,
        )
        return await _heal(
            ctx,
            session,
            product_id,
            brief,
            idea_id,
            category=f"council_rejection:{','.join(blocked_by)}",
        )

    if not council.approved:
        # Reviewers withheld approval and the release decision shipped anyway, with
        # the remaining findings accepted deliberately. They are issues now, worked
        # on their own merits rather than as a precondition for every future deploy.
        log.info(
            "development.shipped_with_open_findings",
            product_id=product_id,
            findings=len(council.blocking),
            accepted=len(decision.accepted_issues) if decision else 0,
            recorded=recorded,
        )

    # is_validation products have no Stripe wiring at all (LANDING_PAGE_SYSTEM), so
    # they get no PRODUCT_PRICE_CENTS -- see _runtime_env's own None handling. A
    # fresh lookup rather than threading the price through every _verify_and_ship
    # caller: only this one call site needs it, and idea_id is already in scope.
    price_usd: float | None = None
    if not is_validation:
        idea_row = await repository.get_idea(session, idea_id)
        price_usd = idea_row.self_serve_price_usd if idea_row is not None else None

    result = await deploy_module.deploy(
        council=council,
        product_id=product_id,
        product_name=product_name,
        workspace=workspace,
        settings=ctx.settings,
        dry_run=ctx.dry_run,
        self_serve_price_usd=price_usd,
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

    # The product is deployed but not yet shipped. Everything that judged it up to
    # here read source -- the gap check against criteria, Bug Council's categories,
    # the generated test against HTML on disk, the contract and SEO greps. None of
    # them can answer "does it work", and the difference is not academic: a missing
    # CORS configuration meant every form on every page this factory ever deployed
    # failed in the browser while all five source reviewers passed it.
    #
    # This has to run after the deploy -- there is no URL to open before one -- so
    # "before shipping" means before the product counts as live and before marketing
    # starts working it.
    preship_outcome = await preship.review_before_ship(
        ctx,
        session,
        product_id=product_id,
        product_name=product_name,
        deploy_url=result.url or "",
        custom_domain=getattr(product_row, "custom_domain", None),
        requirements=requirements,
        task_id=task_id,
    )
    if not preship_outcome.ran:
        # A reviewer that could not run must never be silent. The first version of
        # this shipped on `ran=False` with nothing but a log warning, which recreates
        # the exact failure this whole gate exists to fix: a check that quietly does
        # not happen looks identical to a check that passed. A broken browser in the
        # image would have sent every product out unreviewed and said nothing.
        #
        # It still ships -- refusing to release a product that deployed correctly
        # because the reviewer's browser would not start punishes the product for an
        # infrastructure fault. But "this went out unreviewed" is a fact somebody has
        # to be told, so it is said out loud, once, per product.
        log.warning(
            "development.shipped_unreviewed", product_id=product_id, reason=preship_outcome.summary
        )
        await interrupts.request(
            session,
            reason=InterruptReason.DATA_INTEGRITY,
            subject=f"{product_name[:80]} shipped without a pre-ship review",
            detail=(
                f"{product_name} deployed to {result.url} and went out **without the "
                "pre-ship browser review running**.\n\n"
                f"Why it could not run: {preship_outcome.summary}\n\n"
                "The product is live and may be fine -- nothing found a defect, "
                "because nothing looked. Worth checking the page by hand, and worth "
                "finding out why the reviewer could not start: if it is the browser "
                "itself, every product after this one ships unreviewed too."
            ),
            headline="👁️ Shipped without review",
            reply_hint="No reply needed -- informational, but do not ignore twice.",
            needs_reply=False,
            product_id=product_id,
        )
    elif not preship_outcome.may_ship:
        # Findings are already filed as issues. Route to repair rather than
        # escalating: these are ordinary defects with a concrete brief, and the
        # convergence logic decides when that stops being worth another attempt.
        log.info(
            "development.preship_blocked",
            product_id=product_id,
            blocking=len(preship_outcome.blocking),
            url=result.url,
        )
        return await _heal(
            ctx,
            session,
            product_id,
            "The page deployed, but a reviewer using it in a real browser could not "
            "ship it. Fix these, and only these:\n"
            + "\n".join(f"- {item}" for item in preship_outcome.blocking[:10]),
            idea_id,
            category="preship_review",
        )

    await repository.resolve_pending_heal_attempts(session, product_id, "healed")

    # Above the `is_validation` branch deliberately. This used to sit below it, past
    # the early `return`, so the only product type this factory actually ships was
    # the one that never got a design brief -- every landing page deployed with the
    # dashboard's design panel empty. Never blocks shipping: `build_design_brief`
    # degrades to None on any failure and no-ops when one is already cached.
    if result.url:
        await design_brief.build_design_brief(
            ctx, session, product_id=product_id, deploy_url=result.url, task_id=task_id
        )

    if is_validation:
        # Stays VALIDATING, not LIVE -- this is a pitch page, not a real product.
        # A distinct idempotency-key namespace (leads:validate:*) is deliberate:
        # the real post-launch harvest below uses leads:{product_id}, and if
        # this validation later succeeds and a genuine BUILD_MVP goes LIVE for
        # the same product_id, that harvest must still be able to fire -- reusing
        # the same key would make it silently no-op via ON CONFLICT DO NOTHING.
        await repository.set_product_status(
            session,
            product_id,
            ProductStatus.VALIDATING,
            deploy_url=result.url,
            heal_attempts=0,
            # The workspace and the customer now agree again -- recorded as the
            # digest of what was actually uploaded, so the next caller can verify
            # it rather than trust the flag beside it.
            workspace_undeployed=False,
            deployed_content_hash=result.content_hash,
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
        session,
        product_id,
        ProductStatus.LIVE,
        deploy_url=result.url,
        heal_attempts=0,
        # The workspace and the customer now agree again -- recorded as the digest
        # of what was actually uploaded, so the next caller can verify it rather
        # than trust the flag beside it.
        workspace_undeployed=False,
        deployed_content_hash=result.content_hash,
    )
    # The work this issue was assigned shipped. That is `fixed` -- a claim, not a
    # conclusion. The next review either fails to find the defect (and closes it) or
    # reports it again (and reopens it). Nothing here may close an issue, because
    # "the patch deployed" and "the defect is gone" are different facts and treating
    # them as one is what let a broken checkout be repaired four times and stay
    # broken.
    if task_id:
        await repository.mark_issue_fixed(
            session, task_id=task_id, resolution=f"Patch deployed to {result.url}"
        )
    if result.project:
        await repository.set_vercel_project(session, product_id, result.project)
    await repository.enqueue_task(
        session,
        kind=TaskKind.HARVEST_LEADS,
        engine=EngineName.MARKETING,
        idempotency_key=f"leads:{product_id}",
        payload={"product_id": product_id},
        product_id=product_id,
        priority=120,
    )
    # Asked once, ever, per product -- never blocking (the product is already
    # reachable on its Vercel subdomain). A redeploy of an already-live product
    # (a repair pass, a growth_seo edit) must not ask again, hence the guard.
    live_product = await repository.get_product(session, product_id)
    if live_product is not None and live_product.domain_status is None:
        await domain.ensure_requested(session, product_id=product_id, product_name=product_name)
        await repository.set_domain_requested(session, product_id)
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

    # Stop on a wall, not on a count.
    #
    # This was `attempts >= max_heal_cycles`, and it is the reason a blank-slate
    # landing page that had *already been fixed* escalated anyway: by attempt three
    # the workspace passed the contract check and the SEO check with zero violations
    # and carried no backend, no auth and no Stripe -- and a counter ended it
    # regardless, because nothing here ever asked whether the work was going
    # anywhere. The same shape as every other bug found today: a blind rule
    # overruling an informed judgement, and firing first.
    #
    # `product_progress` answers the question the counter could not. A run that
    # closed blockers since the last decision is working, and there is no principled
    # number of attempts at which working should stop. A run whose blocking set has
    # not moved, or that keeps re-finding the same criterion, is a wall -- and one
    # more attempt at a wall is worse than saying so.
    progress = await repository.product_progress(session, product_id)
    walled = progress.converging is False or bool(progress.stuck)
    # The cost backstop, deliberately not the decision. It exists so an oscillating
    # repair cannot bill indefinitely, and it sits far enough out that it is not
    # what normally ends a run.
    out_of_budget = attempts >= ctx.settings.max_heal_cycles * 2

    if walled or out_of_budget:
        log.info(
            "development.heal_stopped",
            product_id=product_id,
            attempts=attempts,
            reason="budget" if out_of_budget and not walled else "not_converging",
            blocking_now=progress.blocking_now,
            blocking_before=progress.blocking_at_last_attempt,
            stuck=len(progress.stuck),
        )
        await repository.record_heal_attempt(
            session,
            product_id=product_id,
            task_id=None,
            attempt_number=attempts,
            healing_text=healing,
            failed_categories=[category],
            blocking_at_attempt=progress.blocking_now,
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
                f"A product exhausted all {ctx.settings.max_heal_cycles} heal cycles on category "
                f"'{category}' and was escalated to a human. Last blocking feedback: "
                f"{healing[:300]}"
            ),
            kind=memory_store.MemoryKind.FAILURE,
            scope=memory_store.scope_for(engine=EngineName.DEVELOPMENT),
            origin="development:heal_exhausted",
        )
        display_name = product.name if product else product_id
        # A short, fresh id suffix -- not `product_id` alone -- so a product that
        # heals successfully (heal_attempts resets to 0 on success, see below) and
        # later exhausts its budget again on a different bug gets a subject that
        # doesn't collide with the first escalation's now-resolved row. Same
        # precedent as `propose_channel`'s `experiment_id[:8]`, `interrupts.py`'s
        # own subject-collapse-by-(reason, subject, status) uniqueness otherwise
        # blocks the *second* one from ever reaching 'resolved' -- found live,
        # see DEPLOYMENT_CHECKLIST.md.
        # **State the findings from the rows, before any retelling of them.**
        #
        # This escalation used to *be* a paraphrase of `healing`, and a paraphrase of
        # a statement about what is wrong can invert it. Observed twice: once
        # mischaracterising a wrong-env-var bug as "hardcoded price" (which is why
        # `plain_language_error` appends a raw excerpt at all), and once reporting
        # "the page is loading an external web font ... it needs to be removed" about
        # a page whose actual finding was `MUST not met: the page loads at least one
        # webfont`. Exactly reversed -- and acting on it would have guaranteed the
        # same failure again.
        #
        # An issue title is already a short sentence written to be read, and it is the
        # record rather than a retelling of it. Rendering cannot reverse a negation;
        # summarising can. The paraphrase stays below, because it genuinely helps with
        # the raw technical tail a healing brief sometimes carries (a pip failure, a
        # pytest dump) -- but it is now the supporting detail, not the claim.
        blocking_issues = [
            i
            for i in await repository.open_issues_for(session, product_id)
            if i.severity in ("critical", "high")
        ]
        findings = (
            "What is blocking it, exactly as recorded:\n\n"
            + "\n".join(f"- [{i.severity}] {i.title}" for i in blocking_issues[:12])
            if blocking_issues
            else "No blocking issue is recorded against this product, which is itself "
            "worth a look -- the repair budget ran out without anything holding the "
            "release."
        )
        explanation = await interrupts.plain_language_error(
            ctx,
            healing[:2000],
            context=(
                f"Automated repair on '{display_name}' did not converge after {attempts} tries."
            ),
        )
        await interrupts.request(
            session,
            reason=InterruptReason.HEAL_EXHAUSTED,
            subject=f"{display_name[:80]} needs help ({new_id()[:8]})",
            detail=f"{findings}\n\n---\n\nWhat the repair loop was working from:\n\n{explanation}",
            headline="\U0001f527 Repair stuck — needs your input",
            reply_hint=(
                "Reply with guidance for one more attempt, DEFER to deploy as-is and "
                "accept this as a known, un-fixed risk, or REJECT to retire this product."
            ),
            product_id=product_id,
        )
        log.warning("development.heal_exhausted", product_id=product_id, attempts=attempts)
        return f"development: product {product_id} escalated after {attempts} heal cycles"

    # The hour bucket is what makes this key unique across heal *cycles*, not just
    # within one. `attempts` comes from `product.heal_attempts`, which
    # `_verify_and_ship` resets to 0 on every successful ship -- so a product that
    # heals, ships, and later regresses starts a second cycle at 1 and collides with
    # the first cycle's key. `enqueue_task` then returns None and, before this, the
    # code simply carried on: the heal attempt was recorded, "heal_queued" was
    # logged, and the product was left in PATCHING with no task in existence and
    # nothing to notice. Observed live -- the exact reason a real checkout bug sat
    # unrepaired while every log line claimed a repair had been queued. (Same
    # resetting-counter-as-a-lifetime-key bug as `record_heal_attempt`'s, found the
    # same day, in the other place it occurs.)
    #
    # Deduping within the hour still covers what this key is actually for: two
    # `_heal` calls racing on the same cycle.
    cycle_bucket = int(datetime.now(UTC).timestamp()) // 3600
    patch_task_id = await repository.enqueue_task(
        session,
        kind=TaskKind.PATCH_PRODUCT,
        engine=EngineName.DEVELOPMENT,
        idempotency_key=f"patch:{product_id}:heal:{attempts}:{cycle_bucket}",
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
    if patch_task_id is None:
        # Should now be unreachable (the key above is unique per cycle), but a
        # silently-dropped repair is the failure that produced a product stranded
        # in PATCHING with nothing queued and nothing to notice -- so it fails
        # loudly rather than being inferred from an absence months later. The
        # product goes back to LIVE rather than being left mid-repair: the next
        # audit can then see it and re-raise, which a PATCHING product is invisible
        # to.
        log.error(
            "development.heal_not_queued",
            product_id=product_id,
            attempt=attempts,
            detail="enqueue_task returned None; a repair was requested but never created",
        )
        await repository.set_product_status(session, product_id, ProductStatus.LIVE)
        return f"development: product {product_id} repair could not be queued (cycle {attempts})"

    await repository.record_heal_attempt(
        session,
        product_id=product_id,
        task_id=patch_task_id,
        attempt_number=attempts,
        healing_text=healing,
        failed_categories=[category],
        # The anchor the next pass measures against: how much was blocking when
        # this repair was commissioned. Recorded here, at the attempt, because
        # convergence is a claim about attempts -- reading it off the last release
        # decision meant reading a number written moments earlier in the same pass.
        blocking_at_attempt=progress.blocking_now,
    )
    log.info("development.heal_queued", product_id=product_id, attempt=attempts)
    return f"development: product {product_id} needs repair (cycle {attempts})"


async def deploy_accepting_risk(
    session: AsyncSession,
    *,
    product_id: str,
    override_reason: str,
    settings: Settings | None = None,
) -> str:
    """Force-deploy a product's current workspace exactly as it stands, bypassing
    Bug Council's approval gate entirely -- reached only from a human's own
    explicit "defer/accept the risk" reply to a heal_exhausted escalation (see
    `hitl/interrupts.py::_act_on_product_reply`), never from the normal
    build/patch pipeline. The whole point of this path is that the product's
    actual owner, not the Council, gets the final call on their own product's
    launch-readiness bar -- e.g. shipping a known, deferred issue to test real
    market traction before spending more repair budget hardening it. No test
    suite or Council review runs here; there is nothing to re-check that would
    change the outcome, since the whole point is to ship the workspace as it
    already is.
    """
    settings = settings or get_settings()
    product = await session.get(Product, product_id)
    if product is None:
        return f"development: product {product_id!r} not found"

    workspace = product.workspace_path or f"product-{product_id[:12]}"
    try:
        async with product_lock(
            session,
            product_id=product_id,
            holder=EngineName.DEVELOPMENT,
            reason="deploying with an accepted, known risk",
        ):
            idea_row = await repository.get_idea(session, product.idea_id)
            price_usd = idea_row.self_serve_price_usd if idea_row is not None else None

            result = await deploy_module.deploy(
                council=None,
                override_reason=override_reason,
                product_id=product_id,
                product_name=product.name,
                workspace=workspace,
                settings=settings,
                dry_run=settings.factory_dry_run,
                self_serve_price_usd=price_usd,
            )
            if not result.ok:
                await repository.set_product_status(session, product_id, ProductStatus.PLANNED)
                return f"development: override deploy for {product_id} failed: {result.detail}"

            await repository.set_product_status(
                session, product_id, ProductStatus.LIVE, deploy_url=result.url, heal_attempts=0
            )
            if result.project:
                await repository.set_vercel_project(session, product_id, result.project)
            await repository.enqueue_task(
                session,
                kind=TaskKind.HARVEST_LEADS,
                engine=EngineName.MARKETING,
                idempotency_key=f"leads:{product_id}",
                payload={"product_id": product_id},
                product_id=product_id,
                priority=120,
            )
            if product.domain_status is None:
                await domain.ensure_requested(
                    session, product_id=product_id, product_name=product.name
                )
                await repository.set_domain_requested(session, product_id)
            # "Make a note of it" -- PREFERENCE is the one MemoryKind that means "an
            # operator decision to honour going forward," not a bug or a lesson: this
            # is neither, it's a deliberate, informed risk acceptance that the next
            # human or agent touching this product needs to see.
            await memory_store.remember(
                session,
                content=(
                    f"Deployed with a known, deliberately deferred issue (human override, "
                    f"not fixed): {override_reason[:600]}"
                ),
                kind=memory_store.MemoryKind.PREFERENCE,
                scope=memory_store.scope_for(product_id=product_id),
                origin="development:deploy_accepting_risk",
            )
            log.warning(
                "development.deployed_with_accepted_risk",
                product_id=product_id,
                reason=override_reason[:300],
            )
            return (
                f"development: product {product_id} deployed with an accepted, "
                f"known risk to {result.url}"
            )
    except LockUnavailable:
        # Deliberately not deferred, unlike the runner paths above: this is reached
        # from a human's own reply to an escalation, so there is no task to put back
        # on the queue -- and retrying it silently later would act on a decision the
        # person made about the workspace as it looked when they replied. Say so.
        return (
            f"development: product {product_id} is being worked on by another task "
            "right now, so it was not force-deployed. Reply again once it settles."
        )
