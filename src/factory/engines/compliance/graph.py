"""Compliance Engine runner.

Handles idea-level assessment. Lead-level LIA is invoked inline by Marketing through
``lia.evaluate_lead`` so a prospect can never be contacted on a path that skips it.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from factory.engines.compliance import assessor
from factory.engines.compliance.rules import idea_risk_category_hits
from factory.engines.discovery.demand import assess_demand
from factory.hitl import interrupts
from factory.logging import get_logger
from factory.memory import store as memory_store
from factory.settings import EngineName
from factory.state import repository
from factory.state.orm import Idea, Task
from factory.state.schemas import (
    ComplianceStatus,
    InterruptReason,
    ProductStatus,
    TaskKind,
    ValidatedIdea,
)
from factory.supervisor.context import FactoryContext

log = get_logger(__name__)

#: How long before a compliance task deferred on validation capacity is retried.
#: Same order of magnitude as marketing/campaign.py's RECHECK_SECONDS -- cheap (no
#: LLM call on retry, see the idea.status short-circuit below), so a lower bound
#: costs nothing while avoiding a tight reschedule loop.
VALIDATION_CAPACITY_RECHECK_SECONDS = 3600


class ValidationCapacityExceeded(RuntimeError):
    """Raised when an idea is approved but `max_concurrent_validations` is already
    full.

    Caught in `supervisor/graph.py::_run_engine`, which defers the task
    (`repository.defer_task`) rather than treating it as a failure -- mirrors
    `marketing.campaign.CampaignApprovalPending` exactly: capacity freeing up is
    not something this task caused or can control, and must never count toward
    `max_task_attempts`.
    """


async def run(ctx: FactoryContext, session: AsyncSession, task: Task) -> str:
    idea_id = task.idea_id or str(task.payload.get("idea_id", ""))
    if not idea_id:
        return "compliance: task carried no idea_id"

    row = await session.get(Idea, idea_id)
    if row is None:
        return f"compliance: idea {idea_id} no longer exists"

    # A retry after ValidationCapacityExceeded: the idea was already assessed and
    # approved on a previous pass, just deferred for capacity. Skip straight to the
    # validation-creation step rather than paying for another assessor call to
    # re-derive the same answer.
    if row.status == "approved":
        return await _create_validation(session, ctx, row)

    idea = ValidatedIdea(
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

    assessment = await assessor.assess_idea(ctx, idea, session=session, task_id=task.id)
    log.info(
        "compliance.idea_verdict",
        idea_id=idea_id,
        status=str(assessment.status),
        confidence=assessment.confidence,
    )

    if assessment.status is ComplianceStatus.APPROVED:
        risk_hits = idea_risk_category_hits(idea.problem_statement, idea.monetization_hypothesis)
        if risk_hits:
            # Tier 2 finding #6, system-wide verification plan: a deterministic
            # backstop the model's own APPROVED verdict cannot talk past,
            # mirroring the spam-phrase backstop already behind outreach
            # copy's prompt-level instructions. REJECTED is left untouched --
            # it is already the safe, conservative outcome (see the comment
            # on the confidence check below) and does not need a second gate.
            log.info("compliance.idea_risk_category_override", idea_id=idea_id, hits=risk_hits)
            assessment = assessment.model_copy(update={"status": ComplianceStatus.FLAG_FOR_REVIEW})
        else:
            await repository.set_idea_status(session, idea_id, "approved")
            return await _create_validation(session, ctx, row)

    if assessment.status is ComplianceStatus.REJECTED:
        await repository.set_idea_status(session, idea_id, "rejected")
        await _remember_for_discovery(session, idea, assessment.justification, verdict="rejected")
        return f"compliance: idea {idea_id} rejected — {assessment.justification[:200]}"

    await repository.set_idea_status(session, idea_id, "flagged")
    await _remember_for_discovery(
        session, idea, assessment.justification, verdict="flagged for human review"
    )
    await interrupts.request(
        session,
        reason=InterruptReason.LEGAL_UNCERTAINTY,
        subject=f"Idea needs legal review: {idea.target_niche}"[:200],
        detail=(
            # Tier 3 finding #13, system-wide verification plan: the full set
            # of structured facts the justification below is reasoning about,
            # rendered from the real Idea row rather than trusted to have been
            # faithfully repeated by the model's own prose -- a human can spot
            # a justification that argues about a different niche or a
            # monetization plan it never actually saw.
            f"Niche: {idea.target_niche}\n"
            f"Problem: {idea.problem_statement}\n"
            f"Monetization: {idea.monetization_hypothesis}\n"
            f"Source: {idea.source_url}\n"
            f"Confidence: {assessment.confidence:.2f}\n\n"
            f"{assessment.justification}"
        ),
        headline="⚖️ Legal review needed — new idea",
        reply_hint="Reply APPROVE to let this idea proceed to build, or REJECT to discard it.",
        task_id=task.id,
    )
    return f"compliance: idea {idea_id} flagged for human review"


async def _remember_for_discovery(
    session: AsyncSession, idea: ValidatedIdea, justification: str, *, verdict: str
) -> None:
    """Filed at *Discovery's* scope, not Compliance's -- Discovery is the reader
    that needs it (see memory/store.py's own module docstring: "the last three
    products in this niche all failed compliance for the same reason, stop
    proposing them" -- a loop this closes for the first time). Compliance
    itself never reads memory back; see assessor.py's structured-facts
    alternative for why."""
    await memory_store.remember(
        session,
        content=(
            f"Compliance {verdict} an idea in the niche '{idea.target_niche}': "
            f"{justification[:300]}"
        ),
        kind=memory_store.MemoryKind.FAILURE,
        scope=memory_store.scope_for(engine=EngineName.DISCOVERY),
        origin="compliance:run",
    )


async def _create_validation(session: AsyncSession, ctx: FactoryContext, idea_row: Idea) -> str:
    """Approved ideas no longer go straight to a full build. A lightweight Product
    is created (VALIDATING) so leads/outreach/interest signals have somewhere to
    attach; BUILD_MVP only gets queued later, by EVALUATE_VALIDATION, if real
    interest clears the bar. See the idea validation gate design:
    development/graph.py's _build_landing_page / _evaluate_validation, and
    marketing/graph.py's validation-aware outreach.

    Capped by `max_concurrent_validations`, separate from real build capacity --
    checked here, before anything is created, so a retry after
    ValidationCapacityExceeded starts clean rather than needing to reconcile a
    half-created product.
    """
    active = await repository.count_active_validations(session)
    if active >= ctx.settings.max_concurrent_validations:
        raise ValidationCapacityExceeded(
            f"{active}/{ctx.settings.max_concurrent_validations} validations already running"
        )

    # Does the evidence actually justify spending on this? Nothing asked before.
    # Every idea this factory has produced carries `evidence_count = 2`, all of them
    # from one GitHub thread, and the only gate on that number anywhere is
    # `if evidence_count == 0` -- so two comments bought a real build as easily as
    # two hundred would have. This reads the posts instead of counting them, which
    # only became possible once `idea_evidence` stopped throwing them away.
    #
    # Here rather than in Discovery on purpose: Discovery emits many ideas per
    # cycle and most never reach this point, so assessing them all would pay for
    # judgement nobody uses. This runs once, on the one idea about to cost money.
    assessment = await assess_demand(
        ctx,
        session,
        idea_id=idea_row.id,
        problem_statement=idea_row.problem_statement,
        target_niche=idea_row.target_niche,
    )
    if assessment is not None:
        await repository.record_demand_assessment(
            session, idea_id=idea_row.id, assessment=assessment
        )
        if assessment.verdict == "insufficient":
            # Not a capacity error and not a failure -- a judgement, recorded, with
            # its reasoning kept so a human can disagree with it.
            await repository.set_idea_status(session, idea_row.id, "rejected")
            log.info(
                "compliance.demand_insufficient",
                idea_id=idea_row.id,
                reasoning=assessment.reasoning[:300],
            )
            return (
                f"compliance: idea {idea_row.id} approved on compliance but not built -- "
                f"the evidence does not support it: {assessment.reasoning[:200]}"
            )
    # A `thin` verdict deliberately still builds. A landing page is the cheapest
    # instrument there is for exactly the question thin evidence leaves open, and
    # refusing to run it because the evidence is weak is how a factory only ever
    # builds things it already knew. The assessment is stored either way, so the
    # release judgement and the requirements both see how strong the case was.

    product_id = await repository.create_product(
        session, name=idea_row.target_niche[:80], idea_id=idea_row.id
    )
    await repository.set_product_status(session, product_id, ProductStatus.VALIDATING)
    await repository.enqueue_task(
        session,
        kind=TaskKind.BUILD_LANDING_PAGE,
        engine=EngineName.DEVELOPMENT,
        idempotency_key=f"validate:{idea_row.id}",
        payload={"idea_id": idea_row.id, "product_id": product_id},
        idea_id=idea_row.id,
        product_id=product_id,
        priority=50,
    )
    return f"compliance: idea {idea_row.id} approved and queued for validation"
