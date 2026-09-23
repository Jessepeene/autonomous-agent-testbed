"""Marketing Engine runner."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from urllib.parse import urlparse

from sqlalchemy.ext.asyncio import AsyncSession

from factory.agents.roles import RoleName
from factory.agents.spawner import SpawnRequest, spawn
from factory.engines.compliance.rules import MAX_CONTACTS_PER_DOMAIN_PER_MONTH
from factory.engines.marketing import campaign, deliverability, outreach
from factory.engines.marketing import leads as leads_module
from factory.hitl import interrupts
from factory.logging import get_logger
from factory.memory import store as memory_store
from factory.settings import EngineName
from factory.state import repository
from factory.state.orm import ChannelExperiment, Idea, Product, Task, new_id
from factory.state.schemas import InterruptReason, ProductStatus, TaskKind
from factory.supervisor.context import FactoryContext

log = get_logger(__name__)

_OUTREACH_BATCH = 15


async def run(ctx: FactoryContext, session: AsyncSession, task: Task) -> str:
    if TaskKind(task.kind) is TaskKind.ANSWER_QUESTION:
        # A question from another department, arriving as ordinary work.
        from factory.answering import answer_question_task

        return await answer_question_task(ctx, session, task)

    kind = TaskKind(task.kind)
    if kind is TaskKind.HARVEST_LEADS:
        return await _harvest(ctx, session, task)
    if kind is TaskKind.SEND_OUTREACH:
        return await _outreach(ctx, session, task)
    if kind is TaskKind.CHANNEL_SCOUT_REVIEW:
        return await _channel_scout_review(ctx, session, task)
    if kind is TaskKind.MEASURE_CHANNEL_EXPERIMENT:
        return await _measure_channel_experiment(ctx, session, task)
    return f"marketing: unsupported task kind {task.kind}"


async def _product_summary(session: AsyncSession, product_id: str | None) -> tuple[str, str | None]:
    if not product_id:
        return "an unnamed internal tool", None
    product = await session.get(Product, product_id)
    if product is None:
        return "an unnamed internal tool", None
    idea = await session.get(Idea, product.idea_id)
    if idea is None:
        return product.name, product_id
    summary = (
        f"{product.name} — solves: {idea.problem_statement}\n"
        f"For: {idea.target_niche}\n"
        f"Pricing thesis: {idea.monetization_hypothesis}"
    )
    return summary, product_id


async def _is_validating(session: AsyncSession, product_id: str | None) -> bool:
    if not product_id:
        return False
    product = await session.get(Product, product_id)
    return product is not None and product.status == ProductStatus.VALIDATING.value


async def _harvest(ctx: FactoryContext, session: AsyncSession, task: Task) -> str:
    product_id = task.product_id or task.payload.get("product_id")
    summary, resolved_id = await _product_summary(session, product_id)

    # A validation run sources roughly as many leads as it intends to email, not
    # the steady-state default -- over-sourcing for a cheap pre-build check burns
    # the shared, metered lead provider quota before the idea has proven out.
    validating = await _is_validating(session, resolved_id)
    limit = ctx.settings.validation_outreach_batch_size if validating else 50

    report = await leads_module.ingest(
        ctx, session, product_summary=summary, product_id=resolved_id, limit=limit, task_id=task.id
    )
    if resolved_id:
        # `IngestReport` itself is ephemeral (built fresh per call, consumed only
        # by the credential-problem check right below) -- this is the only place
        # its "why" survives past this one call, so a later CHANNEL_SCOUT_REVIEW
        # (see `_channel_scout_review` and `scheduler.py`'s stalled-validation
        # third strike) can be told why the current channel came up empty.
        await repository.set_last_lead_sourcing_summary(session, resolved_id, report.summary())
    if resolved_id and (report.credential_problem or report.request_problem):
        await _escalate_lead_sourcing_problem(session, resolved_id, report)
    if report.stored:
        # Validation harvest uses a distinct idempotency-key namespace from the
        # real post-launch one below (outreach:{id}:*) -- if this validation later
        # succeeds and a genuine build goes LIVE for the same product_id, that
        # product's real outreach must still be able to fire.
        #
        # Suffixed with this harvest task's own id, not report.stored: two
        # different harvest runs can coincidentally store the same *number* of new
        # leads, which previously produced an identical key and silently collapsed
        # the second run's SEND_OUTREACH enqueue into the first (ON CONFLICT DO
        # NOTHING) -- the second batch of leads would then never be contacted.
        # task.id is unique per harvest attempt and stable across retries of the
        # *same* task, so a retry after a crash still correctly dedups.
        key_prefix = "outreach:validate" if validating else "outreach"
        await repository.enqueue_task(
            session,
            kind=TaskKind.SEND_OUTREACH,
            engine=EngineName.MARKETING,
            idempotency_key=f"{key_prefix}:{resolved_id or 'general'}:{task.id}",
            payload={"product_id": resolved_id},
            product_id=resolved_id,
            priority=150,
        )
    return f"marketing/harvest: {report.summary()}"


async def _escalate_lead_sourcing_problem(
    session: AsyncSession, product_id: str, report: leads_module.IngestReport
) -> None:
    """Raised immediately, not deferred to the stalled-validation sweep's next
    check: a diagnosed credential/request problem is not something waiting
    will fix, so there is no reason to make a human wait up to
    `validation_stalled_hours` to be asked about something they could fix in
    two minutes -- the same reasoning `MISSING_CREDENTIAL` already asks about
    a genuinely absent key immediately rather than waiting for it to matter.

    Skips raising a second escalation for a product already awaiting one --
    a persistently broken credential must not re-ask on every later harvest
    cycle (the periodic live-product sweep runs every 12h; a validating
    product's own automatic retry could otherwise pile a second ask on top).
    """
    if await repository.has_open_interrupt_for_product(session, product_id):
        return
    product = await repository.get_product(session, product_id)
    name = product.name if product is not None else product_id

    if report.credential_problem:
        headline = "\U0001f511 Account problem — can't source leads"
        detail = (
            f"{report.credential_problem}\n\n"
            "HTTP 401/403 means the API key is present but being refused -- "
            "invalid, expired, or the plan/credits do not cover this call. "
            "This is an account problem, not a code bug: check the provider's "
            "dashboard directly (key validity, plan, remaining credits)."
        )
    else:
        headline = "\U0001f41b Code bug — can't source leads"
        detail = (
            f"{report.request_problem}\n\n"
            "HTTP 400/422 means the provider rejected the request itself as "
            "malformed -- this looks like a bug in how the factory calls this "
            "provider, not something fixable in its dashboard. Worth a look "
            "at the request-building code, not just a retry."
        )
    # A fresh id, not product_id[:8]: product_id is stable across this product's
    # whole life, so it would collide with an earlier, now-resolved escalation
    # for the same product the moment a *second* one needed to reach 'resolved'
    # (the exact uq_interrupts_open_subject bug -- see
    # 0017_interrupt_open_subject_partial.py). Safe to make each raise unique:
    # this function already returns early above if one is already open for
    # this product, so a fresh id here is never covering up a real duplicate.
    await interrupts.request(
        session,
        reason=InterruptReason.LEAD_SOURCING_STALLED,
        subject=f"{name[:80]} can't source leads ({new_id()[:8]})",
        detail=detail,
        headline=headline,
        reply_hint="Reply anything to retry sourcing leads, or REJECT to retire this product.",
        product_id=product_id,
    )


async def _outreach(ctx: FactoryContext, session: AsyncSession, task: Task) -> str:
    product_id = task.product_id or task.payload.get("product_id")
    summary, resolved_id = await _product_summary(session, product_id)

    validating = await _is_validating(session, resolved_id)
    if validating:
        # Scoped to leads sourced for *this* idea -- a validation's response rate
        # must measure interest in this idea specifically, not a global pool that
        # may be dominated by leads harvested for something else entirely.
        candidates = await repository.leads_for_outreach(
            session, limit=ctx.settings.validation_outreach_batch_size, product_id=resolved_id
        )
    else:
        # Also scoped to resolved_id, matching the validating branch: every
        # SEND_OUTREACH task is enqueued by _harvest() for one specific product
        # (`payload={"product_id": resolved_id}`), and `summary` above is that
        # same product's pitch -- pulling from the unscoped global pool here
        # would apply this product's outreach copy to leads actually sourced for
        # a completely different one.
        candidates = await repository.leads_for_outreach(
            session, limit=_OUTREACH_BATCH, product_id=resolved_id
        )
    if not candidates:
        return "marketing/outreach: no LIA-approved leads are waiting"

    # The spam-complaint circuit breaker: checked before the campaign-approval gate
    # since a frozen state is a standing fact about the whole channel, not something
    # a fresh human approval for this one cohort could override.
    await deliverability.ensure_not_frozen(session)

    # The top-level legal gate: strictly forbidden from opening the SMTP transport
    # for this cohort without an explicit human "approve" on file. A still-pending
    # decision raises `CampaignApprovalPending`, which propagates to
    # `supervisor/graph.py::_run_engine` — it defers the task rather than failing it,
    # so this keeps being retried, indefinitely, on `campaign.RECHECK_SECONDS`. A
    # rejection is final, not "come back later", so it is caught here and completes
    # the task normally rather than retrying forever.
    try:
        await campaign.ensure_approval(
            session,
            product_id=resolved_id,
            product_name=summary,
            task_id=task.id,
            candidates=candidates,
        )
    except campaign.CampaignRejected as exc:
        return f"marketing/outreach: {exc}"

    outcome = outreach.OutreachOutcome()
    for lead_row in candidates:
        outcome.attempted += 1

        # The per-domain cap is checked at LIA time (compliance/lia.py), against
        # the contact count *as of ingestion* -- if several leads at the same
        # company all clear LIA before any of them are actually sent to, this
        # batch can still send to all of them at once, blowing through the cap by
        # however many share a domain. Re-checked live, immediately before each
        # send: record_outreach() flushes (not just adds) its row, so an earlier
        # send in this same batch is already visible to this count without needing
        # a separate in-memory tally.
        domain = lead_row.work_email.partition("@")[2]
        contacts = await repository.outreach_count_for_domain(session, domain)
        if contacts >= MAX_CONTACTS_PER_DOMAIN_PER_MONTH:
            outcome.suppressed += 1
            log.info(
                "marketing.domain_cap_reached_mid_batch",
                domain=domain,
                contacts=contacts,
            )
            continue

        sent = await outreach.send_to_lead(
            ctx,
            session,
            lead_row,
            product_summary=summary,
            product_id=resolved_id,
            task_id=task.id,
        )
        if sent:
            outcome.sent += 1
        else:
            outcome.suppressed += 1

    log.info("marketing.outreach_batch", summary=outcome.summary(), dry_run=ctx.dry_run)

    if validating and resolved_id:
        # Anchored here, at real send completion, not at landing-page deploy time:
        # campaign.ensure_approval above is a human-paced, unbounded wait (it defers
        # this whole task via CampaignApprovalPending until approved), so anchoring
        # any earlier would silently shrink the real observation window by however
        # long approval took.
        not_before = datetime.now(UTC) + timedelta(days=ctx.settings.validation_window_days)
        await repository.enqueue_task(
            session,
            kind=TaskKind.EVALUATE_VALIDATION,
            engine=EngineName.DEVELOPMENT,
            # Scoped to this send, not to the product's lifetime. A bare
            # `evaluate-validation:{resolved_id}` answers "has this product's
            # validation ever been evaluated" -- so once the first evaluation ran
            # and came back inconclusive (development/graph.py now retries lead
            # sourcing rather than retiring), the *second* real outreach batch's
            # attempt to queue a re-evaluation would return None, silently, and the
            # product would sit at VALIDATING forever with signals trickling in
            # that nothing ever tallies again. Same key shape as BUG_LOG 38 and 42.
            idempotency_key=f"evaluate-validation:{resolved_id}:{task.id}",
            payload={"product_id": resolved_id},
            product_id=resolved_id,
            not_before=not_before,
            priority=100,
        )

    return f"marketing/outreach: {outcome.summary()}"


async def _channel_scout_review(ctx: FactoryContext, session: AsyncSession, task: Task) -> str:
    """Spawn CHANNEL_SCOUT for one product -- see `scheduler.py`'s periodic backstop
    sweep (`products_needing_channel_scout`) and `agents/roles.py`.

    Same reasoning as `development/graph.py::_growth_review`: the Orchestrator can
    also reach CHANNEL_SCOUT directly via SPAWN_AGENT (it is in the catalogue);
    this is the reliability net so a product's distribution never goes unexamined
    purely because the Orchestrator's own judgement never picked it.
    """
    product_id = task.product_id or str(task.payload.get("product_id", ""))
    product = await session.get(Product, product_id) if product_id else None
    if product is None:
        return f"marketing: product {product_id!r} not found for channel scout review"

    objective = (
        f"Research a new organic growth channel for product {product_id} "
        f"({product.name}) and propose one if you find a genuinely good fit."
    )
    # Why this scout was called matters: a product whose only channel just went
    # quiet (see `_harvest`'s write and `scheduler.py`'s stalled-validation third
    # strike) needs a genuinely different channel, not a repeat of the one that
    # already failed -- without this the scout would be reasoning blind about
    # what's already been tried.
    context = product.last_lead_sourcing_summary or ""
    result = await spawn(
        ctx,
        session,
        SpawnRequest(
            role=RoleName.CHANNEL_SCOUT,
            objective=objective,
            context=context,
            engine=EngineName.MARKETING,
            product_id=product_id,
            task_id=task.id,
        ),
    )
    await repository.set_channel_scouted(session, product_id)
    log.info("marketing.channel_scouted", product_id=product_id, outcome=result.summary())
    return f"marketing/channel_scout: {result.summary()}"


async def _measure_channel_experiment(
    ctx: FactoryContext, session: AsyncSession, task: Task
) -> str:
    """Periodic, deterministic check on one approved channel experiment (see
    `scheduler.py`'s sweep) -- tallies referrer hits against the channel's own
    domain since it was approved, and concludes it. Never spawns a model call.

    Best-effort by construction: `page_views.referrer` is whatever the visitor's
    browser sent (often nothing at all), so a zero count means "no attributable
    traffic," not "definitely no effect." Still a real, automated measurement --
    the point is to close the loop CHANNEL_SCOUT's next proposal can learn from,
    not to produce an attribution-grade number.
    """
    experiment_id = str(task.payload.get("experiment_id", ""))
    experiment = await session.get(ChannelExperiment, experiment_id) if experiment_id else None
    if experiment is None or experiment.status != "approved":
        return f"marketing: channel experiment {experiment_id!r} not awaiting measurement"

    since = experiment.decided_at or experiment.created_at
    host = urlparse(experiment.url).netloc or experiment.channel
    views = await repository.count_page_views_from_referrer(
        session, experiment.product_id, since=since, referrer_contains=host
    )
    outcome = f"{views} visit(s) referred from {host} since approval on {since.date().isoformat()}."
    await repository.conclude_channel_experiment(session, experiment_id, outcome=outcome)
    memory_content = (
        f"Channel experiment on {experiment.channel} ({experiment.url}) for "
        f"product {experiment.product_id}: {outcome}"
    )
    await memory_store.remember(
        session,
        content=memory_content,
        kind=memory_store.MemoryKind.LESSON,
        scope=memory_store.scope_for(product_id=experiment.product_id),
        origin="marketing:channel_experiment",
    )
    # Second write, scoped by niche rather than product: the product-scoped memory
    # above dies with this one product, but "(channel, niche, outcome)" is exactly
    # the evidence CHANNEL_SCOUT's next spawn on a *different* product in the same
    # niche needs -- `agents/spawner.py::spawn` already recalls on every spawn (see
    # `Product.last_lead_sourcing_summary`'s sibling read above), so this is the
    # write side of a loop that was previously silently product-scoped only, the
    # same scoping gap Phase 3 fixed for validation outcomes.
    niche = await repository.get_niche_for_product(session, experiment.product_id)
    if niche:
        await memory_store.remember(
            session,
            content=f"[{niche}] {memory_content}",
            kind=memory_store.MemoryKind.LESSON,
            scope=memory_store.scope_for(engine=EngineName.MARKETING),
            origin="marketing:channel_experiment",
        )
    log.info(
        "marketing.channel_experiment_concluded",
        product_id=experiment.product_id,
        experiment_id=experiment_id,
        views=views,
    )
    return f"marketing: channel experiment {experiment_id} concluded -- {outcome}"
