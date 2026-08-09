"""Autonomous legal-entity resolution.

Distinguishes a contactable corporate entity from a protected natural person (or an
unregistered sole proprietorship indistinguishable from one) before a lead is ever
handed to the LIA. This is a *distinct* gate from the LIA: the LIA argues why
contacting a given role is legitimate; this gate answers a prior, narrower question —
is the organisation behind this domain actually a company at all.

Deliberately not a lookup against a hardcoded registry (a KVK client, an OpenCorporates
integration, or similar): an ephemeral RESEARCHER agent is given the company name and
domain and must work out *how* to check, from whatever public sources its `http`
tool can reach — a company website, an imprint/terms page, a registration number, a
legal-entity suffix. Which sources exist and are useful is exactly the kind of thing
that should not be hardcoded into this module; the agent decides, per the codebase's
existing stance that "roles are the containment, tool selection is not."

This is a heuristic, best-effort check, not a legally authoritative determination —
worth stating plainly rather than implying otherwise. A real KVK (or equivalent
national registry) lookup would be strictly more reliable than a web-search agent's
inference, and remains the honest recommendation for a production deployment that
wants this decision to hold up to regulatory scrutiny. What this module guarantees
instead is the fail-closed property the requirement actually asked for: whenever the
evidence is not clearly corporate, the lead is dropped, never contacted on an
optimistic guess.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from factory.agents.roles import RoleName
from factory.agents.spawner import SpawnRequest, spawn
from factory.llm.providers.base import LLMError
from factory.logging import get_logger
from factory.settings import EngineName, Settings, TaskType, get_settings
from factory.state import repository
from factory.state.schemas import EntityType, EntityVerdict
from factory.supervisor.context import FactoryContext

log = get_logger(__name__)

_EXTRACT_SYSTEM = """\
You extract a structured entity-type verdict from a researcher's free-text \
investigation of whether a company domain belongs to a registered corporate entity.

Set entity_type=unknown whenever the researcher's own account is inconclusive, \
contradictory, or based on a source that went unreached (a fetch failure, a parked \
page, no evidence either way). Do not resolve ambiguity in the researcher's favour —
an inconclusive investigation is evidence of nothing, not weak evidence of a company. \
Only set entity_type=corporate when the researcher actually found concrete evidence: \
a legal-entity suffix (BV, NV, GmbH, Ltd, Inc, LLC, Corp, AG, SA, and similar), a \
company registration number, an imprint/impressum/terms page naming a registered \
company, or equivalent.\
"""


def _objective(company: str, domain: str) -> str:
    return (
        f"Determine whether the organisation operating under the domain {domain!r} "
        f"(known to us as {company!r}) is a registered corporate legal entity, as "
        "opposed to an unregistered sole proprietorship or an individual operating "
        "under their own name. Investigate only the organisation — you have no "
        "information about, and must not speculate about, any individual person. "
        "Check the domain's public website if it is reachable: look for a legal-"
        "entity suffix in the company name, an imprint/impressum/terms/about page "
        "naming a registered company, or a company registration number. If you "
        "cannot find clear evidence either way, say so plainly rather than guessing "
        "— an inconclusive result is the correct and expected outcome for many small "
        "domains, not a failure."
    )


async def resolve_entity_type(
    ctx: FactoryContext,
    session: AsyncSession,
    *,
    company: str,
    email_domain: str,
    task_id: str | None = None,
) -> EntityVerdict:
    """Cached by ``email_domain`` — see `EntityVerdictRow` ORM docstring. Any failure to
    complete or extract resolves to `EntityType.UNKNOWN` at zero confidence, which
    `passes_entity_gate` below treats identically to an explicit negative: fail closed,
    never fail open on an agent or provider error.
    """
    cached = await repository.get_entity_verdict(session, email_domain)
    if cached is not None:
        return EntityVerdict(
            entity_type=EntityType(cached.entity_type),
            confidence=cached.confidence,
            evidence=cached.evidence,
        )

    result = await spawn(
        ctx,
        session,
        SpawnRequest(
            role=RoleName.RESEARCHER,
            objective=_objective(company, email_domain),
            engine=EngineName.COMPLIANCE,
            task_id=task_id,
        ),
    )

    if not result.succeeded or not result.answer.strip():
        verdict = _unknown(f"the investigation did not complete ({result.stopped_because})")
    else:
        verdict = await _extract_or_unknown(ctx, result.answer, task_id=task_id)

    stored = await repository.store_entity_verdict(
        session,
        email_domain=email_domain,
        entity_type=verdict.entity_type.value,
        confidence=verdict.confidence,
        evidence=verdict.evidence,
        task_id=task_id,
    )
    return EntityVerdict(
        entity_type=EntityType(stored.entity_type),
        confidence=stored.confidence,
        evidence=stored.evidence,
    )


async def _extract_or_unknown(
    ctx: FactoryContext, answer: str, *, task_id: str | None
) -> EntityVerdict:
    if not answer.strip():
        return _unknown("the investigation produced no answer")
    try:
        return await ctx.llm.parse(
            EntityVerdict,
            task_type=TaskType.ANALYSIS,
            system=_EXTRACT_SYSTEM,
            prompt=answer,
            engine=EngineName.COMPLIANCE,
            task_id=task_id,
        )
    except LLMError as exc:
        log.warning("entity.extraction_failed", detail=str(exc)[:200])
        return _unknown(f"verdict extraction failed: {exc}")


def _unknown(reason: str) -> EntityVerdict:
    return EntityVerdict(entity_type=EntityType.UNKNOWN, confidence=0.0, evidence=reason[:1500])


def passes_entity_gate(verdict: EntityVerdict, *, settings: Settings | None = None) -> bool:
    """Deterministic admission gate: an agent's judgement decides the inputs, code
    decides the outcome — same pattern as the LIA and the PLG constraint. Unverified
    (UNKNOWN or SOLE_PROPRIETOR_OR_INDIVIDUAL, or CORPORATE at too little confidence)
    is algorithmically dropped, exactly as required."""
    settings = settings or get_settings()
    return (
        verdict.entity_type is EntityType.CORPORATE
        and verdict.confidence >= settings.entity_resolution_min_confidence
    )


__all__ = ["passes_entity_gate", "resolve_entity_type"]
