"""Marketing Engine runner."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from factory.engines.marketing import campaign, deliverability, outreach
from factory.engines.marketing import leads as leads_module
from factory.logging import get_logger
from factory.settings import EngineName
from factory.state import repository
from factory.state.orm import Idea, Product, Task
from factory.state.schemas import TaskKind
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


async def _harvest(ctx: FactoryContext, session: AsyncSession, task: Task) -> str:
    product_id = task.product_id or task.payload.get("product_id")
    summary, resolved_id = await _product_summary(session, product_id)

    report = await leads_module.ingest(
        ctx, session, product_summary=summary, product_id=resolved_id, task_id=task.id
    )
    if report.stored:
        await repository.enqueue_task(
            session,
            kind=TaskKind.SEND_OUTREACH,
            engine=EngineName.MARKETING,
            idempotency_key=f"outreach:{resolved_id or 'general'}:{report.stored}",
            payload={"product_id": resolved_id},
            product_id=resolved_id,
            priority=150,
        )
    return f"marketing/harvest: {report.summary()}"


async def _outreach(ctx: FactoryContext, session: AsyncSession, task: Task) -> str:
    product_id = task.product_id or task.payload.get("product_id")
    summary, resolved_id = await _product_summary(session, product_id)

    candidates = await repository.leads_for_outreach(session, limit=_OUTREACH_BATCH)
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
    return f"marketing/outreach: {outcome.summary()}"
