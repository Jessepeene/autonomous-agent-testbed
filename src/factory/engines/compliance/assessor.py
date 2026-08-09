"""LLM compliance assessment — the second gate, never the only one.

Deterministic rules in ``rules.py`` decide what is *permitted*. This module decides
whether something permitted is also *advisable*, and produces the written
justification a regulator would ask for.

Low confidence is treated as a refusal, not a pass: an assessor that is unsure has
not established a lawful basis.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

from factory.llm.providers.base import LLMError
from factory.logging import get_logger
from factory.settings import EngineName, Settings, TaskType, get_settings
from factory.state.schemas import (
    ComplianceAssessment,
    ComplianceStatus,
    LeadAssessment,
    LeadAssessmentBatch,
    LeadRecord,
    LIAVerdict,
    ValidatedIdea,
)
from factory.supervisor.context import FactoryContext

log = get_logger(__name__)

#: A batch that returns fewer than this fraction of its leads has lost track of the
#: mapping, not merely skipped a couple of prospects — refuse the whole chunk rather
#: than trust the entries it did return.
_MIN_BATCH_SURVIVAL = 0.75

LIA_SYSTEM_PROMPT = """\
You are the compliance assessor for a B2B software company established in the \
Netherlands. You perform Legitimate Interest Assessments under GDPR Article 6(1)(f) \
before any cold outreach.

Argue all three limbs explicitly and separately:

1. role_utility_link — the direct, logical connection between this person's stated \
professional role and the utility of this specific tool. A generic "they work in \
tech" is not a link. If the tool would not plausibly help someone in this exact \
role, say so and fail the assessment.
2. necessity — why contacting this individual is necessary to pursue that interest, \
and why no less intrusive means would achieve it.
3. balancing_test — weigh the recipient's reasonable expectations and rights against \
the commercial interest. Consider whether a person in this role at this kind of \
company would reasonably expect professional contact about this category of tool.

Set passed=false whenever any limb fails. Failing is the correct, safe outcome — a \
weak justification that lets an email go out is worse than a missed prospect.

Then set status: APPROVED only if all three limbs hold and the data is clearly \
professional; FLAG_FOR_REVIEW when the case is arguable but genuinely uncertain; \
REJECTED when a limb fails. Report confidence honestly — do not inflate it.\
"""

LIA_BATCH_SYSTEM_PROMPT = (
    LIA_SYSTEM_PROMPT
    + """

You will be given several prospects at once, each carrying a short `ref` token (for \
example "L1", "L2"). Perform an independent Legitimate Interest Assessment for every \
prospect and echo its exact `ref` in your answer — one entry per prospect, in any \
order. Never invent a `ref` you were not given, never combine two prospects into one \
entry, and never return the same `ref` twice. Give each prospect the same full, \
independent three-limb analysis you would give if it were the only one — do not let \
an early prospect's reasoning anchor the others.\
"""
)

IDEA_SYSTEM_PROMPT = """\
You are the compliance assessor for an autonomous software company established in \
the Netherlands. You review proposed product ideas before any engineering effort is \
spent on them.

Reject an idea when building it would require: processing special-category personal \
data (GDPR Art. 9), scraping a platform in breach of its terms of service, \
circumventing authentication or rate limits, handling payment card data outside a \
PCI-compliant processor, or operating in a licensed sector (finance, healthcare, \
legal advice) without the licence.

Flag for review — do not silently approve — when the idea is lawful but carries \
liability that a human should accept knowingly: processing employee data, storing \
customer PII beyond contact details, or automating decisions with legal effect.

Report confidence honestly.\
"""


async def assess_lead(
    ctx: FactoryContext,
    lead: LeadRecord,
    *,
    product_summary: str,
    settings: Settings | None = None,
    task_id: str | None = None,
) -> ComplianceAssessment:
    """Run the LIA for one prospect.

    Any failure — model outage, refusal, low confidence — resolves to "do not
    contact". The default answer about a stranger's personal data is no.
    """
    settings = settings or get_settings()
    prompt = (
        "Prospect (all data shown is everything we hold):\n"
        f"- job_title: {lead.job_title}\n"
        f"- company: {lead.company}\n"
        f"- email domain: {lead.email_domain}\n"
        f"- source_url: {lead.source_url}\n"
        f"- scraped_at: {lead.scraped_at.isoformat()}\n\n"
        f"Product we would contact them about:\n{product_summary}\n\n"
        "Perform the Legitimate Interest Assessment."
    )

    try:
        assessment = await ctx.llm.parse(
            ComplianceAssessment,
            task_type=TaskType.COMPLIANCE,
            system=LIA_SYSTEM_PROMPT,
            prompt=prompt,
            engine=EngineName.COMPLIANCE,
            task_id=task_id,
        )
    except LLMError as exc:
        log.warning("compliance.assessment_failed", detail=str(exc)[:300])
        return _refuse(f"assessment could not be completed: {exc}")

    return finalise_lead_assessment(assessment, settings=settings)


def finalise_lead_assessment(
    assessment: ComplianceAssessment, *, settings: Settings
) -> ComplianceAssessment:
    """The post-parse thresholding shared by the per-lead and batched LIA paths.

    Extracted so "does confidence thresholding still work identically for a batched
    verdict" is answerable by construction — both paths call this exact function —
    rather than by re-implementing and hoping the two stay in sync.
    """
    if assessment.lia_verdict is None:
        return _refuse("assessor returned no LIA verdict")
    if not assessment.lia_verdict.passed:
        return assessment.model_copy(update={"status": ComplianceStatus.REJECTED})
    if assessment.confidence < settings.compliance_min_confidence:
        log.info(
            "compliance.low_confidence",
            confidence=assessment.confidence,
            threshold=settings.compliance_min_confidence,
        )
        return assessment.model_copy(update={"status": ComplianceStatus.FLAG_FOR_REVIEW})
    return assessment


def _batch_refs(n: int) -> list[str]:
    return [f"L{i + 1}" for i in range(n)]


def _scaled_batch_tokens(n: int) -> int:
    """Output is the binding constraint, not input: worst case is three ~1000-char
    limbs plus a ~2000-char justification per lead, well over a thousand tokens each
    with JSON overhead. The default 8,192-token ceiling truncates at well under 5
    leads, which would turn a token saving into a loss — truncation means
    `SchemaViolation`, a full-price repair pass, and likely fail-closed for the whole
    chunk anyway. `max_tokens` is a ceiling, not a charge, so widening it here costs
    nothing on a chunk whose real output is shorter."""
    return min(1_024 + 1_800 * n, 16_384)


async def assess_leads(
    ctx: FactoryContext,
    leads: Sequence[LeadRecord],
    *,
    product_summary: str,
    settings: Settings | None = None,
    task_id: str | None = None,
) -> list[ComplianceAssessment]:
    """One LIA call covering every lead in ``leads``. Order-preserving: element ``i``
    of the result corresponds to ``leads[i]``. Fails closed on any sign the model
    lost track of which answer belongs to which prospect — see the module docstring
    in `lia.py` for why a wrong correlation here is the catastrophic failure mode.
    """
    settings = settings or get_settings()
    if not leads:
        return []

    refs = _batch_refs(len(leads))
    blocks = "\n\n".join(
        f"### {ref}\n"
        f"- job_title: {lead.job_title}\n"
        f"- company: {lead.company}\n"
        f"- email domain: {lead.email_domain}\n"
        f"- source_url: {lead.source_url}\n"
        f"- scraped_at: {lead.scraped_at.isoformat()}"
        for ref, lead in zip(refs, leads, strict=True)
    )
    prompt = (
        "Prospects (all data shown is everything we hold about each):\n\n"
        f"{blocks}\n\n"
        f"Product we would contact them about:\n{product_summary}\n\n"
        "Perform the Legitimate Interest Assessment for every prospect."
    )

    try:
        batch = await ctx.llm.parse(
            LeadAssessmentBatch,
            task_type=TaskType.COMPLIANCE,
            system=LIA_BATCH_SYSTEM_PROMPT,
            prompt=prompt,
            engine=EngineName.COMPLIANCE,
            task_id=task_id,
            max_tokens=_scaled_batch_tokens(len(leads)),
        )
    except LLMError as exc:
        log.warning("compliance.batch_assessment_failed", detail=str(exc)[:300], size=len(leads))
        return [_refuse(f"batch assessment could not be completed: {exc}") for _ in leads]

    by_ref: dict[str, LeadAssessment] = {}
    corrupt = False
    for entry in batch.assessments:
        if entry.ref not in refs or entry.ref in by_ref:
            corrupt = True
            break
        by_ref[entry.ref] = entry

    if not corrupt and len(by_ref) < math.ceil(_MIN_BATCH_SURVIVAL * len(leads)):
        corrupt = True

    if corrupt:
        # Losing the mapping — a duplicate, an unknown ref, or too few entries to
        # trust the rest — means every verdict in the chunk is suspect, not just the
        # visibly wrong one. Refuse all of it.
        log.warning(
            "compliance.batch_corrupt",
            size=len(leads),
            returned=len(batch.assessments),
        )
        return [
            _refuse("the batched assessment lost track of which answer belongs to whom")
            for _ in leads
        ]

    results: list[ComplianceAssessment] = []
    for ref in refs:
        matched = by_ref.get(ref)
        if matched is None:
            # A simple, isolated omission: everything else in the batch checked out,
            # so only this prospect — not the whole chunk — is refused.
            results.append(_refuse("no assessment was returned for this prospect"))
            continue
        results.append(finalise_lead_assessment(matched.to_assessment(), settings=settings))
    return results


async def assess_idea(
    ctx: FactoryContext,
    idea: ValidatedIdea,
    *,
    settings: Settings | None = None,
    task_id: str | None = None,
) -> ComplianceAssessment:
    settings = settings or get_settings()
    prompt = (
        "Proposed product:\n"
        f"- problem: {idea.problem_statement}\n"
        f"- target niche: {idea.target_niche}\n"
        f"- monetization: {idea.monetization_hypothesis}\n"
        f"- evidence source: {idea.source_url}\n\n"
        "Assess whether this may be built and sold."
    )
    try:
        assessment = await ctx.llm.parse(
            ComplianceAssessment,
            task_type=TaskType.COMPLIANCE,
            system=IDEA_SYSTEM_PROMPT,
            prompt=prompt,
            engine=EngineName.COMPLIANCE,
            task_id=task_id,
        )
    except LLMError as exc:
        log.warning("compliance.idea_assessment_failed", detail=str(exc)[:300])
        return _refuse(f"assessment could not be completed: {exc}")

    if assessment.confidence < settings.compliance_min_confidence:
        return assessment.model_copy(update={"status": ComplianceStatus.FLAG_FOR_REVIEW})
    return assessment


def _refuse(reason: str) -> ComplianceAssessment:
    return ComplianceAssessment(
        status=ComplianceStatus.FLAG_FOR_REVIEW,
        justification=(
            f"Automatic hold: {reason}. No lawful basis has been established, so the "
            "safe default is to take no action pending human review."
        ),
        confidence=0.0,
        lia_verdict=LIAVerdict(
            role_utility_link="not established — assessment did not complete",
            necessity="not established — assessment did not complete",
            balancing_test="not established — assessment did not complete",
            passed=False,
        ),
    )
