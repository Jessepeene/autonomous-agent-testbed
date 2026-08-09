"""Compliance Engine runner.

Handles idea-level assessment. Lead-level LIA is invoked inline by Marketing through
``lia.evaluate_lead`` so a prospect can never be contacted on a path that skips it.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from factory.engines.compliance import assessor
from factory.hitl import interrupts
from factory.logging import get_logger
from factory.settings import EngineName
from factory.state import repository
from factory.state.orm import Idea, Task
from factory.state.schemas import (
    ComplianceStatus,
    InterruptReason,
    TaskKind,
    ValidatedIdea,
)
from factory.supervisor.context import FactoryContext

log = get_logger(__name__)


async def run(ctx: FactoryContext, session: AsyncSession, task: Task) -> str:
    idea_id = task.idea_id or str(task.payload.get("idea_id", ""))
    if not idea_id:
        return "compliance: task carried no idea_id"

    row = await session.get(Idea, idea_id)
    if row is None:
        return f"compliance: idea {idea_id} no longer exists"

    idea = ValidatedIdea(
        id=row.id,
        problem_statement=row.problem_statement,
        target_niche=row.target_niche,
        source_url=row.source_url,
        monetization_hypothesis=row.monetization_hypothesis,
        score=row.score,
        evidence_count=row.evidence_count,
        created_at=row.created_at,
    )

    assessment = await assessor.assess_idea(ctx, idea, task_id=task.id)
    log.info(
        "compliance.idea_verdict",
        idea_id=idea_id,
        status=str(assessment.status),
        confidence=assessment.confidence,
    )

    if assessment.status is ComplianceStatus.APPROVED:
        await repository.set_idea_status(session, idea_id, "approved")
        # Approved ideas become build candidates. The Orchestrator's throttle decides
        # when Development actually picks this up.
        await repository.enqueue_task(
            session,
            kind=TaskKind.BUILD_MVP,
            engine=EngineName.DEVELOPMENT,
            idempotency_key=f"build:{idea_id}",
            payload={"idea_id": idea_id},
            idea_id=idea_id,
            priority=50,
        )
        return f"compliance: idea {idea_id} approved and queued for build"

    if assessment.status is ComplianceStatus.REJECTED:
        await repository.set_idea_status(session, idea_id, "rejected")
        return f"compliance: idea {idea_id} rejected — {assessment.justification[:200]}"

    await repository.set_idea_status(session, idea_id, "flagged")
    await interrupts.request(
        session,
        reason=InterruptReason.LEGAL_UNCERTAINTY,
        subject=f"Idea needs legal review: {idea.target_niche}"[:200],
        detail=(
            f"{idea.problem_statement}\n\n"
            f"Confidence: {assessment.confidence:.2f}\n"
            f"{assessment.justification}\n\n"
            "Reply APPROVE to allow the build, or REJECT to discard the idea."
        ),
        task_id=task.id,
    )
    return f"compliance: idea {idea_id} flagged for human review"
