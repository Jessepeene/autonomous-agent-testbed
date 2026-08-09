"""Autonomous lead ingestion.

The pipeline, and the order is the whole design:

    draft plan (product text only, no PII)
      → autonomous refinement (LEAD_SCOUT agent tests & adjusts via real search tools)
      → real provider query (Hunter / Apollo — whichever hold credentials)
      → adapter narrows to 5 fields + provenance   ← fat payload dies here
      → deterministic rules                        ← no model consulted yet
      → entity resolution                          ← model sees company/domain only
      → LIA                                        ← model sees role/company/domain only
      → minimize() at the DB boundary              ← second enforcement
      → leads table (5 PII columns, NOT NULL provenance)

Three properties worth stating explicitly, because all three are easy to lose later:

**The planner never sees personal data.** Deciding *who to look for* is reasoning
about a product and a market, so the prompt that produces a `LeadSearchPlan` contains
the product description and nothing else. The LEAD_SCOUT agent that refines it is
handed the same draft plan and product text, and its tools (`find_leads_hunter`/
`find_leads_apollo`) return only role, company, and email domain — never a name or a
usable address — so refinement can iterate on real search results without a personal
record ever entering an LLM prompt. The manual CSV path (`providers.CsvDropProvider`)
is deprecated from this autonomous flow for the same reason `build_providers()` no
longer includes it: an operator-vetted file is a deliberate exception a human invokes,
not something the factory should reach for on its own initiative.

**The entity-resolution step never sees a person either.** It answers a question
about the *company* — is this domain a registered corporate entity — from the company
name and domain alone. See `compliance/entity.py`.

**The LIA never sees the full record.** `assess_lead` is handed job title, company,
email *domain*, and provenance — not the person's name and not their address. The
model can argue a role-to-utility link from that, and a third-party model provider
never receives an identifiable individual.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from factory.agents.roles import RoleName
from factory.agents.spawner import SpawnRequest, spawn
from factory.engines.compliance.entity import passes_entity_gate, resolve_entity_type
from factory.engines.compliance.lia import evaluate_leads
from factory.engines.compliance.minimize import ProvenanceMissing
from factory.engines.marketing.providers import (
    ApolloProvider,
    HunterProvider,
    LeadCandidate,
    LeadProvider,
    LeadSearchPlan,
)
from factory.learning import prompts as learning_prompts
from factory.llm.providers.base import LLMError
from factory.logging import get_logger
from factory.settings import EngineName, Settings, TaskType
from factory.state import repository
from factory.state.orm import Lead
from factory.state.schemas import LeadRecord
from factory.supervisor.context import FactoryContext

log = get_logger(__name__)

_PLANNER_KEY = "factory.engines.marketing.leads:PLANNER_SYSTEM"

PLANNER_SYSTEM = """\
You convert a B2B product description into search parameters for a contact database.

You are choosing which professional roles would plausibly buy this tool. You are not
choosing individuals, and you are not given any personal data.

Rules:
- job_titles: the exact titles a contact database would hold, not descriptions.
  "Financial Controller", "Head of Finance" — not "someone who does the books".
- seniorities: use only these values where relevant: junior, senior, manager,
  director, vp, executive, owner, founder.
- departments: broad functional areas, e.g. finance, engineering, operations, sales.
- company_keywords: the industry or company type, e.g. "accounting firm",
  "logistics", "managed service provider".
- company_domains: leave empty unless the product is genuinely specific to named
  companies. Never invent domains.
- locations: only when the product is geographically constrained (language,
  regulation, currency). Otherwise leave empty.

Be narrow. A tight list of titles that will actually buy beats a broad list that
generates volume — every additional wrong person is someone whose data we process
without a real basis for it.\
"""


@dataclass(slots=True)
class IngestReport:
    planned: int = 0
    returned: int = 0
    stored: int = 0
    rejected_rules: int = 0
    rejected_lia: int = 0
    rejected_provenance: int = 0
    rejected_unverified_entity: int = 0
    by_provider: dict[str, int] = field(default_factory=dict)
    quota_blocked: bool = False

    def summary(self) -> str:
        if self.quota_blocked:
            return "monthly lead-provider quota reached; no lookups performed"
        return (
            f"{self.returned} candidates from {len(self.by_provider)} provider(s) "
            f"→ {self.stored} stored "
            f"({self.rejected_provenance} no provenance, "
            f"{self.rejected_unverified_entity} unverified-entity, "
            f"{self.rejected_rules} rule-blocked, {self.rejected_lia} LIA-blocked)"
        )


def _real_adapters(settings: Settings) -> list[LeadProvider]:
    """The two credentialed data-provider adapters — never the CSV drop, which is a
    deliberate manual override an operator invokes directly, not part of what the
    factory reaches for on its own initiative (see the module docstring)."""
    candidates: list[LeadProvider] = [HunterProvider(settings), ApolloProvider(settings)]
    return [provider for provider in candidates if provider.available]


_SOURCING_EXTRACT_SYSTEM = """\
You extract a finalized lead-sourcing search plan from a lead scout's free-text \
account of testing and refining a search strategy against real contact-database tools.

Use the scout's final, settled parameters, not an earlier draft it abandoned partway \
through. If the scout concluded the original plan was already right, return it \
unchanged. Never invent a job title, domain, or keyword the scout did not actually \
mention having used or decided on.\
"""


async def refine_sourcing_strategy(
    ctx: FactoryContext,
    session: AsyncSession,
    draft: LeadSearchPlan,
    *,
    product_summary: str,
    task_id: str | None = None,
) -> LeadSearchPlan:
    """Hands the draft plan to an autonomous LEAD_SCOUT agent, which tests it against
    the real (PII-redacted) `find_leads_hunter`/`find_leads_apollo` tools and may
    broaden or narrow it before anything is executed for real against a paying data
    provider. This — not the single non-agentic planning call above — is where "design
    its own sourcing strategy" actually happens: the agent decides which of its
    available tools to call, with what parameters, and whether to iterate, entirely
    from what its own tool registry currently offers rather than a hardcoded sequence.

    Falls back to the draft, unchanged, on any failure to complete or extract —
    refinement is a genuine enhancement over the draft, never a dependency the
    pipeline can be blocked by.
    """
    result = await spawn(
        ctx,
        session,
        SpawnRequest(
            role=RoleName.LEAD_SCOUT,
            objective=(
                "Validate and refine this draft lead-sourcing search plan by testing "
                "it against the contact-database tools available to you. Broaden or "
                "narrow the parameters based on what the tools actually return. When "
                "you are done, state the final search parameters plainly."
            ),
            context=(
                f"Product:\n{product_summary}\n\n"
                f"Draft plan to validate:\n{draft.model_dump_json(indent=2)}"
            ),
            engine=EngineName.MARKETING,
            task_id=task_id,
        ),
    )
    if not result.succeeded or not result.answer.strip():
        return draft

    try:
        refined = await ctx.llm.parse(
            LeadSearchPlan,
            task_type=TaskType.ANALYSIS,
            system=_SOURCING_EXTRACT_SYSTEM,
            prompt=result.answer,
            engine=EngineName.MARKETING,
            task_id=task_id,
        )
    except LLMError as exc:
        log.warning("leads.refinement_extraction_failed", detail=str(exc)[:200])
        return draft
    return refined if refined.is_actionable else draft


async def plan_search(
    ctx: FactoryContext,
    session: AsyncSession,
    *,
    product_summary: str,
    task_id: str | None = None,
) -> tuple[LeadSearchPlan | None, str]:
    """Turn a product description into search parameters. No PII in this prompt.

    Always returns the planner's ``tunable_parameter_id`` alongside the plan (even
    when the plan itself is ``None``), so ``ingest()`` can tag every candidate's
    eventual outcome to the version that produced this search.
    """
    system, version_id = await learning_prompts.get_prompt(
        session, key=_PLANNER_KEY, default=PLANNER_SYSTEM
    )
    try:
        plan = await ctx.llm.parse(
            LeadSearchPlan,
            task_type=TaskType.ANALYSIS,
            system=system,
            prompt=f"Product:\n{product_summary}\n\nProduce the search plan.",
            engine=EngineName.MARKETING,
            task_id=task_id,
            tunable_parameter_id=version_id,
        )
    except LLMError as exc:
        log.warning("leads.planning_failed", detail=str(exc)[:300])
        return None, version_id

    if not plan.is_actionable:
        log.info("leads.plan_not_actionable", rationale=plan.rationale[:200])
        return None, version_id
    log.info(
        "leads.planned",
        titles=len(plan.job_titles),
        domains=len(plan.company_domains),
        keywords=len(plan.company_keywords),
    )
    return plan, version_id


async def leads_created_this_month(session: AsyncSession) -> int:
    since = datetime.now(UTC) - timedelta(days=30)
    stmt = select(func.count()).select_from(Lead).where(Lead.created_at >= since)
    return int((await session.execute(stmt)).scalar_one())


async def ingest(
    ctx: FactoryContext,
    session: AsyncSession,
    *,
    product_summary: str,
    product_id: str | None = None,
    limit: int = 50,
    task_id: str | None = None,
) -> IngestReport:
    report = IngestReport()

    # Provider credits are a metered resource. Exceeding the plan quota costs real
    # money without any decision being made about it, so the cap is enforced before
    # a single lookup is issued.
    quota = ctx.settings.lead_provider_monthly_quota
    used = await leads_created_this_month(session)
    if used >= quota:
        log.warning("leads.quota_reached", used=used, quota=quota)
        report.quota_blocked = True
        return report
    limit = min(limit, quota - used)

    draft_plan, planner_version = await plan_search(
        ctx, session, product_summary=product_summary, task_id=task_id
    )
    if draft_plan is None:
        return report

    plan = await refine_sourcing_strategy(
        ctx, session, draft_plan, product_summary=product_summary, task_id=task_id
    )
    report.planned = len(plan.job_titles) + len(plan.company_domains)

    providers = _real_adapters(ctx.settings)
    if not providers:
        log.info("leads.no_providers")
        return report

    candidates: list[LeadCandidate] = []
    for provider in providers:
        if len(candidates) >= limit:
            break
        try:
            found = await provider.search(plan, limit=limit - len(candidates))
        except Exception as exc:
            log.warning("leads.provider_failed", provider=provider.name, detail=str(exc)[:200])
            continue
        report.by_provider[provider.name] = len(found)
        candidates.extend(found)

    report.returned = len(candidates)

    # The provenance/shape check is per-candidate and pure (no model, no DB), so it
    # runs first regardless of batching. Only candidates that produce a valid
    # `LeadRecord` ever reach entity resolution or the LIA.
    valid: list[tuple[LeadCandidate, LeadRecord]] = []
    for candidate in candidates:
        row = candidate.to_row()
        try:
            valid.append((candidate, LeadRecord(**row)))
        except (ValueError, ProvenanceMissing) as exc:
            log.info(
                "leads.candidate_invalid",
                provider=candidate.provenance.provider,
                detail=str(exc)[:200],
            )
            report.rejected_provenance += 1

    # Entity resolution, cached per unique domain within this batch: several leads
    # from the same company share one verdict rather than spending an agent call
    # each. Unverified — UNKNOWN, an individual, or too little confidence — is
    # algorithmically dropped before the LIA ever sees it.
    verified: list[tuple[LeadCandidate, LeadRecord]] = []
    entity_ok_by_domain: dict[str, bool] = {}
    for candidate, record in valid:
        domain = record.email_domain
        if domain not in entity_ok_by_domain:
            verdict = await resolve_entity_type(
                ctx, session, company=record.company, email_domain=domain, task_id=task_id
            )
            entity_ok_by_domain[domain] = passes_entity_gate(verdict, settings=ctx.settings)
        if entity_ok_by_domain[domain]:
            verified.append((candidate, record))
        else:
            report.rejected_unverified_entity += 1
            log.info("leads.unverified_entity", domain=domain)

    # One call over every survivor — batched internally per
    # `settings.compliance_batch_size` — rather than one call per candidate. At the
    # default size of 1 this issues exactly the same number of LIA calls as before.
    decisions = await evaluate_leads(
        ctx,
        session,
        [record for _, record in verified],
        product_summary=product_summary,
        task_id=task_id,
    )

    for (candidate, record), decision in zip(verified, decisions, strict=True):
        # The planner's outcome, per candidate it produced: did this search parameter
        # set turn up a prospect that actually passed the rules and the LIA? A rules
        # block is still counted against the plan — a plan that names the wrong
        # audience is exactly what "planner_effective_rate" should catch.
        await learning_prompts.record_outcome(
            session,
            tunable_parameter_id=planner_version,
            success=decision.allowed,
            detail={"domain": record.email_domain},
            task_id=task_id,
        )

        if not decision.allowed:
            log.info(
                "leads.blocked",
                provider=candidate.provenance.provider,
                domain=record.email_domain,
                reason=decision.reason[:200],
            )
            # Rule violations are recorded separately from LIA outcomes: a rules
            # block means the audience is wrong, an LIA block means the argument is
            # weak, and they call for different corrections to the search plan.
            if decision.rule_violations:
                report.rejected_rules += 1
            else:
                report.rejected_lia += 1
            continue

        await repository.insert_lead(
            session,
            candidate.to_row(),
            lia_passed=True,
            lia_justification=decision.justification[:4000],
            product_id=product_id,
            provenance_kind=candidate.provenance.kind.value,
            provider=candidate.provenance.provider,
        )
        report.stored += 1

    log.info("leads.ingested", summary=report.summary())
    return report
