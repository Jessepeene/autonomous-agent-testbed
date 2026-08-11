"""Marketing Engine runner."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from factory.engines.compliance.rules import MAX_CONTACTS_PER_DOMAIN_PER_MONTH
from factory.engines.marketing import campaign, deliverability, outreach
from factory.engines.marketing import leads as leads_module
from factory.logging import get_logger
from factory.settings import EngineName
from factory.state import repository
from factory.state.orm import Idea, Product, Task
from factory.state.schemas import ProductStatus, TaskKind
from factory.supervisor.context import FactoryContext

log = get_logger(__name__)

_OUTREACH_BATCH = 15


async def run(ctx: FactoryContext, session: AsyncSession, task: Task) -> str:
    kind = TaskKind(task.kind)
    if kind is TaskKind.HARVEST_LEADS:
        return await _harvest(ctx, session, task)
    if kind is TaskKind.SEND_OUTREACH:
        return await _outreach(ctx, session, task)
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
            idempotency_key=f"evaluate-validation:{resolved_id}",
            payload={"product_id": resolved_id},
            product_id=resolved_id,
            not_before=not_before,
            priority=100,
        )

    return f"marketing/outreach: {outcome.summary()}"
