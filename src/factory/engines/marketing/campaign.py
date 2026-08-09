"""The top-level legal gate on outbound campaigns.

The factory is strictly forbidden from opening the SMTP transport without an explicit
human "approve" for the specific cohort of leads about to be contacted. This is
independent of, and sits above, everything else in the outreach path: the LIA already
established a lawful basis for each individual lead, and this gate still runs anyway.
It is a standing policy checkpoint, not a legal-uncertainty escalation — the brief is
generated and the human is asked *every time*, never only when something looks wrong.

Mechanically this is a poll, not a callback: `ensure_approval` is called every time
`_outreach` attempts its task, and raises `CampaignApprovalPending` for as long as the
linked `Interrupt` sits unanswered. `sync_decisions` — run once per tick from
`Scheduler.housekeeping`, right after replies are collected — is what turns a human's
free-text Telegram reply into the row's real `status`, fail-closed: anything that is
not an unambiguous "approve" is treated as a rejection, because silence or confusion
must never be read as authorisation to spend money contacting people.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from factory.hitl import interrupts
from factory.logging import get_logger
from factory.state import repository
from factory.state.orm import Interrupt, Lead
from factory.state.schemas import InterruptReason

log = get_logger(__name__)

#: How long to wait before re-checking a still-pending approval. Cheap (no LLM call,
#: no send attempt) so an hourly recheck loses nothing while avoiding a tight
#: reschedule loop for a decision that may take a human hours or days to make.
RECHECK_SECONDS = 3600


class CampaignApprovalPending(RuntimeError):
    """Raised by `ensure_approval` while a cohort still awaits an explicit decision.

    Caught specifically in `supervisor/graph.py::_run_engine`, which defers the task
    (`repository.defer_task`) rather than treating it as a failure — an unanswered
    approval is not an error, and must never count toward `max_task_attempts`.
    """


class CampaignRejected(RuntimeError):
    """Raised by `ensure_approval` once a cohort has been explicitly rejected.

    Deliberately a *different* exception from `CampaignApprovalPending`: a rejection
    is a final decision, not a "come back later" state, so it is caught inside
    `marketing/graph.py::_outreach` itself and turned into an ordinary, non-retried
    task completion — rechecking a rejected cohort every hour forever would be both
    wasteful and wrong.
    """


def _build_brief(*, product_name: str, product_id: str | None, candidates: list[Lead]) -> str:
    """Role, company and domain only — never a full name or email address, matching
    the restraint already applied to every other HITL message this factory sends."""
    lines = [
        f"Campaign Compliance Brief — {product_name} ({product_id})",
        f"Leads ready to contact: {len(candidates)}",
        "Legal basis: GDPR Art. 6(1)(f) legitimate interest; each lead below has an "
        "individual Legitimate Interest Assessment on file.",
    ]
    sample = candidates[:5]
    if sample:
        lines.append("Sample recipients (role/company only, no personal data):")
        lines.extend(
            f"  - {lead.job_title} at {lead.company} "
            f"[{lead.provenance_kind}, source: {lead.provider}]"
            for lead in sample
        )
    if len(candidates) > len(sample):
        lines.append(f"  ...and {len(candidates) - len(sample)} more.")
    lines.append("")
    lines.append("Reply APPROVE to open the SMTP transport for this cohort, or REJECT to block it.")
    return "\n".join(lines)


async def ensure_approval(
    session: AsyncSession,
    *,
    product_id: str | None,
    product_name: str,
    task_id: str,
    candidates: list[Lead],
) -> None:
    """Blocks the caller from proceeding to a send until a human has approved.

    Returns normally only when the cohort's approval status is ``'approved'``.
    Raises `CampaignApprovalPending` while pending, or `CampaignRejected` once a
    human has explicitly declined this cohort — two distinct exception types because
    the two cases need different handling upstream (retry later, vs. never again).
    """
    brief = _build_brief(product_name=product_name, product_id=product_id, candidates=candidates)
    approval = await repository.get_or_create_campaign_approval(
        session,
        product_id=product_id,
        task_id=task_id,
        lead_count=len(candidates),
        brief=brief,
    )

    if approval.status == "approved":
        return
    if approval.status == "rejected":
        raise CampaignRejected(f"campaign for task {task_id} was rejected by the operator")

    if approval.interrupt_id is None:
        interrupt_id = await interrupts.request(
            session,
            reason=InterruptReason.CAMPAIGN_APPROVAL,
            subject=f"Campaign approval needed: {product_name} ({len(candidates)} leads)",
            detail=brief,
            task_id=task_id,
        )
        if interrupt_id is not None:
            await repository.set_campaign_approval_interrupt(session, approval.id, interrupt_id)
            log.info(
                "campaign.approval_requested",
                product_id=product_id,
                task_id=task_id,
                leads=len(candidates),
            )
    raise CampaignApprovalPending(f"campaign for task {task_id} awaits human approval")


def _parse_decision(response: str) -> str:
    """Fail-closed: only an unambiguous, explicit "approve" authorises a send.
    Anything else — REJECT, a question, silence rendered as "acknowledged" by the
    generic interrupt-resolution path — is treated as a rejection."""
    normalized = response.strip().lower()
    return "approved" if normalized.startswith("approve") else "rejected"


async def sync_decisions(session: AsyncSession) -> int:
    """Turn a resolved campaign-approval interrupt into the row's real decision.

    Run once per tick from `Scheduler.housekeeping`, after `interrupts.collect_replies`
    has already turned any tagged Telegram reply into `Interrupt.status='resolved'`.
    """
    synced = 0
    for approval in await repository.pending_campaign_approvals(session):
        assert approval.interrupt_id is not None  # guaranteed by the query filter
        interrupt = await session.get(Interrupt, approval.interrupt_id)
        if interrupt is None or interrupt.status != "resolved":
            continue
        decision = _parse_decision(interrupt.response or "")
        await repository.decide_campaign_approval(session, approval.id, decision)
        log.info(
            "campaign.decided",
            product_id=approval.product_id,
            task_id=approval.task_id,
            decision=decision,
        )
        synced += 1
    return synced
