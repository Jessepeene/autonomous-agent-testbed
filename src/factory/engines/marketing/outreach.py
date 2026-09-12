"""Outreach composition and sending.

Organic outreach runs whenever there is budget-free work to do. Paid acquisition is
gated twice: on available funds, and on whether Development currently holds the
product — money is never spent driving traffic at a product that is mid-patch.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass

from pydantic import Field
from sqlalchemy.ext.asyncio import AsyncSession

from factory.engines.compliance.rules import check_outreach_content, render_compliance_footer
from factory.engines.marketing.email import send_email
from factory.learning import prompts as learning_prompts
from factory.logging import get_logger
from factory.settings import EngineName, TaskType
from factory.state import repository
from factory.state.orm import Lead, new_id
from factory.state.schemas import Contract, LeadRecord
from factory.supervisor import budget, concurrency
from factory.supervisor.context import FactoryContext

log = get_logger(__name__)

_OUTREACH_PROMPT_KEY = "factory.engines.marketing.outreach:SYSTEM_PROMPT"
_SENDER_IDENTITY_KEY = "factory.engines.marketing.outreach:SENDER_IDENTITY"
#: Bootstrapped once as version 1 the first time this key is ever read — matches the
#: real display name already deployed on the verified sending domain (see
#: `learning/allowlist.py`'s comment on this key's metric).
_DEFAULT_SENDER_IDENTITY = "Alex Reyes"

SYSTEM_PROMPT = """\
You write one short cold email to one named professional about one B2B tool.

Rules:
- Under 120 words in the body. Plain text, no markdown, no emoji.
- Open with the specific problem their role faces, not with who we are.
- One concrete sentence on what the tool does. No adjectives like "revolutionary", \
"seamless", or "game-changing".
- Exactly one call to action, and make it easy to decline.
- Never claim a prior relationship, a referral, or that they signed up for anything.
- Never invent a statistic, customer name, or case study.

Deliverability: a spam filter reads this before the recipient does, and a complaint
from even one recipient in a thousand risks the sending domain for every product this
factory runs. Never use:
- Spam-trigger phrases: "act now", "buy now", "click here", "limited time", "risk-free",
  "no obligation", "100% free", "congratulations", "you have been selected", "double
  your income", "work from home", "cash bonus", "not spam", "this is not a scam".
- ALL-CAPS words for emphasis, more than one exclamation mark anywhere, or "$" / "€"
  followed directly by a number in the subject line.
- A subject line that promises something the body doesn't specifically deliver, or
  that could read as a personal message from someone the recipient knows (a fake
  "Re:", a first name with no context).
- Excessive urgency ("today only", "expires soon") — the tool being useful is the
  reason to reply, not a manufactured deadline.

Write the email as if the recipient will reply asking one hard question about it.

Finally, step back from the copy you just wrote and apply one holistic check: would \
a real salesperson at this company be comfortable sending this exact email, to this \
exact recipient, under their own name? Set reads_like_a_real_person_wrote_it=false if \
it reads generic, robotic, over-eager, or like a template that was never actually \
customized to this recipient's role or company -- and say specifically what in \
tone_concern.\
"""


class OutreachDraft(Contract):
    subject: str = Field(min_length=5, max_length=140)
    body: str = Field(min_length=50, max_length=1500)
    #: Holistic self-check, same "rides along with a call that already exists"
    #: pattern as `ScoredIdea.gut_check_passed` -- weaker evidence than an
    #: independent reviewer would give (same call, same context, no adversarial
    #: distance from the copy it's judging -- see score.py's own module
    #: docstring on why generator/discriminator are deliberately separate
    #: calls), but real signal at zero extra LLM cost, which matters here since
    #: outreach runs at real volume. Complements, never replaces, the
    #: deterministic factual-claim backstop in
    #: `compliance/rules.py::check_outreach_content`.
    reads_like_a_real_person_wrote_it: bool = True
    tone_concern: str = Field(default="", max_length=300)


@dataclass(slots=True)
class OutreachOutcome:
    attempted: int = 0
    sent: int = 0
    suppressed: int = 0

    def summary(self) -> str:
        return f"{self.attempted} attempted, {self.sent} sent, {self.suppressed} suppressed"


def unsubscribe_url(token: str, ctx: FactoryContext) -> str | None:
    base = ctx.settings.unsubscribe_base_url
    return f"{base.rstrip('/')}/u/{token}" if base else None


async def compose(
    ctx: FactoryContext,
    session: AsyncSession,
    lead: LeadRecord,
    *,
    product_summary: str,
    task_id: str | None = None,
) -> tuple[OutreachDraft, str]:
    """Returns ``(draft, tunable_parameter_id)`` — the version id is always returned
    so ``send_to_lead`` can tag this send's eventual outcome to it."""
    system, version_id = await learning_prompts.get_prompt(
        session, key=_OUTREACH_PROMPT_KEY, default=SYSTEM_PROMPT
    )
    draft = await ctx.llm.parse(
        OutreachDraft,
        task_type=TaskType.MARKETING_COPY,
        system=system,
        prompt=(
            f"Recipient: {lead.first_name} {lead.last_name}, {lead.job_title} at "
            f"{lead.company}.\n\n"
            f"Tool:\n{product_summary}\n\n"
            "Write the email."
        ),
        engine=EngineName.MARKETING,
        task_id=task_id,
        tunable_parameter_id=version_id,
    )
    return draft, version_id


async def send_to_lead(
    ctx: FactoryContext,
    session: AsyncSession,
    lead_row: Lead,
    *,
    product_summary: str,
    product_id: str | None,
    task_id: str | None = None,
) -> bool:
    record = LeadRecord(
        first_name=lead_row.first_name,
        last_name=lead_row.last_name,
        job_title=lead_row.job_title,
        company=lead_row.company,
        work_email=lead_row.work_email,
        source_url=lead_row.source_url,
        scraped_at=lead_row.scraped_at,
    )
    draft, prompt_version = await compose(
        ctx, session, record, product_summary=product_summary, task_id=task_id
    )

    token = secrets.token_urlsafe(32)
    unsub = unsubscribe_url(token, ctx)
    postal = ctx.settings.outbound_postal_address or ""
    body = draft.body + render_compliance_footer(
        postal_address=postal,
        unsubscribe_url=unsub or "",
        source_url=record.source_url,
        provenance_kind=lead_row.provenance_kind,
        provider=lead_row.provider,
    )

    content_check = check_outreach_content(
        body,
        from_address=ctx.settings.outbound_from_address,
        postal_address=postal or None,
        unsubscribe_url=unsub,
    )
    violations = list(content_check.violations)
    if not draft.reads_like_a_real_person_wrote_it:
        concern = draft.tone_concern or "flagged, no detail given"
        violations.append(f"holistic tone check: {concern}")
    passed = content_check.passed and draft.reads_like_a_real_person_wrote_it
    # The primary gating metric for this prompt (see allowlist.DEFAULT_METRIC):
    # same-day, high-volume, and unlike conversion rate, powerful enough to actually
    # catch a regression within one trial window.
    await learning_prompts.record_outcome(
        session,
        tunable_parameter_id=prompt_version,
        success=passed,
        detail={"violations": violations} if not passed else None,
        task_id=task_id,
    )
    if not passed:
        log.info("outreach.content_blocked", violations=violations)
        await repository.record_outreach(
            session,
            lead_id=lead_row.id,
            subject=draft.subject,
            status="blocked",
            product_id=product_id,
            suppressed_reason="; ".join(violations)[:200],
            unsubscribe_token=token,
        )
        return False

    # The display name is a learned choice; the mailbox itself never varies, so a
    # real reply always lands in the one place anyone actually watches — see
    # allowlist.py's comment on this key for why its outcome can't be scored
    # synchronously the way the content prompt's is above.
    identity_name, identity_version = await learning_prompts.get_prompt(
        session, key=_SENDER_IDENTITY_KEY, default=_DEFAULT_SENDER_IDENTITY
    )
    from_address = f"{identity_name} <{ctx.settings.outbound_from_address}>"

    # Generated before the row exists: Mailgun's bounce/complaint webhook only ever
    # echoes back whatever tracking id it was given at send time, so the id has to
    # exist before the send, not after.
    outreach_id = new_id()
    result = await send_email(
        to=record.work_email,
        subject=draft.subject,
        body=body,
        settings=ctx.settings,
        from_address=from_address,
        unsubscribe_url=unsub,
        tracking_id=outreach_id,
    )
    await repository.record_outreach(
        session,
        id=outreach_id,
        lead_id=lead_row.id,
        subject=draft.subject,
        status="sent" if result.sent else "suppressed",
        product_id=product_id,
        suppressed_reason=None if result.sent else result.reason[:200],
        unsubscribe_token=token,
        # A blocked/suppressed send never had a chance to convert and must not be
        # scored against the identity that would have gone out.
        sender_identity_version_id=identity_version if result.sent else None,
    )
    return result.sent


async def paid_campaign_allowed(
    session: AsyncSession, *, product_id: str, amount_cents: int
) -> tuple[bool, str]:
    """Both gates for spending money on a product."""
    if not await concurrency.paid_spend_allowed(session, product_id):
        return False, "product is locked by Development (build or patch in flight)"
    if not await budget.can_afford(session, amount_cents):
        available = await budget.available_cents(session)
        return False, f"reinvestment pool holds {available} cents, need {amount_cents}"
    return True, "permitted"
