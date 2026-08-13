"""The lawful-basis gate for contacting a person.

Order matters and is deliberate: deterministic rules run first, so a personal
mailbox or a domain that has already been contacted three times this month is
rejected without ever reaching a model. That is both cheaper and stricter — there is
no prompt that can talk its way past a rule that already returned False.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from factory.engines.compliance import assessor, rules
from factory.engines.compliance.rules import RuleResult
from factory.hitl import interrupts
from factory.logging import get_logger
from factory.state import repository
from factory.state.schemas import (
    ComplianceAssessment,
    ComplianceStatus,
    InterruptReason,
    LeadRecord,
)
from factory.supervisor.context import FactoryContext

log = get_logger(__name__)


@dataclass(slots=True)
class LeadDecision:
    allowed: bool
    reason: str
    assessment: ComplianceAssessment | None = None
    rule_violations: tuple[str, ...] = ()

    @property
    def justification(self) -> str:
        if self.assessment is not None:
            return self.assessment.justification
        return self.reason


async def _decision_for(
    session: AsyncSession,
    lead: LeadRecord,
    assessment: ComplianceAssessment,
    *,
    task_id: str | None = None,
    product_id: str | None = None,
) -> LeadDecision:
    if assessment.status is ComplianceStatus.FLAG_FOR_REVIEW:
        # Uncertainty is one of the only two things a human may be asked about.
        #
        # open_interrupt() dedups on (reason, subject, status) -- a subject keyed
        # only on email_domain means two different uncertain leads at the same
        # company (different name/title, or even different products) collide, and
        # the second escalation silently collapses into the first: it is never
        # delivered, and that second uncertain case is never actually reviewed.
        # work_email is unique per lead by construction, so it disambiguates.
        await interrupts.request(
            session,
            reason=InterruptReason.LEGAL_UNCERTAINTY,
            subject=f"LIA uncertain for {lead.work_email}",
            detail=(
                f"Role: {lead.job_title} at {lead.company}\n"
                f"Source: {lead.source_url}\n"
                f"Confidence: {assessment.confidence:.2f}\n\n"
                f"{assessment.justification}"
            ),
            headline="⚖️ Legal review needed — outreach segment",
            reply_hint="Reply APPROVE to permit outreach to this segment, or REJECT to block it.",
            task_id=task_id,
            product_id=product_id,
        )
        return LeadDecision(allowed=False, reason="flagged for human review", assessment=assessment)

    if not assessment.approved:
        return LeadDecision(allowed=False, reason="LIA rejected", assessment=assessment)

    return LeadDecision(allowed=True, reason="LIA passed", assessment=assessment)


async def evaluate_leads(
    ctx: FactoryContext,
    session: AsyncSession,
    leads: Sequence[LeadRecord],
    *,
    product_summary: str,
    task_id: str | None = None,
    product_id: str | None = None,
) -> list[LeadDecision]:
    """Evaluate every lead's lawful basis to contact, in input order.

    Rules run per-lead, before any batching — a personal mailbox must never enter a
    batch. Survivors are then assessed either one at a time
    (``compliance_batch_size <= 1``, the default, which is the literal historical
    code path unchanged) or in chunks via ``assessor.assess_leads``.
    """
    rule_checked: list[tuple[LeadRecord, RuleResult]] = []
    for lead in leads:
        contacts = await repository.outreach_count_for_domain(session, lead.email_domain)
        result = rules.check_lead_eligibility(lead, contacts_this_month=contacts)
        rule_checked.append((lead, result))

    survivors = [lead for lead, result in rule_checked if result.passed]

    batch_size = max(1, ctx.settings.compliance_batch_size)
    assessments: list[ComplianceAssessment] = []
    if batch_size <= 1:
        for lead in survivors:
            assessments.append(
                await assessor.assess_lead(
                    ctx, lead, product_summary=product_summary, task_id=task_id
                )
            )
    else:
        for start in range(0, len(survivors), batch_size):
            chunk = survivors[start : start + batch_size]
            assessments.extend(
                await assessor.assess_leads(
                    ctx, chunk, product_summary=product_summary, task_id=task_id
                )
            )

    survivor_assessments = iter(zip(survivors, assessments, strict=True))
    decisions: list[LeadDecision] = []
    for lead, rule_result in rule_checked:
        if not rule_result.passed:
            log.info(
                "compliance.rule_block",
                domain=lead.email_domain,
                violations=rule_result.violations,
            )
            decisions.append(
                LeadDecision(
                    allowed=False,
                    reason="; ".join(rule_result.violations),
                    rule_violations=tuple(rule_result.violations),
                )
            )
            continue
        _, assessment = next(survivor_assessments)
        decisions.append(
            await _decision_for(session, lead, assessment, task_id=task_id, product_id=product_id)
        )
    return decisions


async def evaluate_lead(
    ctx: FactoryContext,
    session: AsyncSession,
    lead: LeadRecord,
    *,
    product_summary: str,
    task_id: str | None = None,
    product_id: str | None = None,
) -> LeadDecision:
    decisions = await evaluate_leads(
        ctx,
        session,
        [lead],
        product_summary=product_summary,
        task_id=task_id,
        product_id=product_id,
    )
    return decisions[0]
