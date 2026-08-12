"""Apollo.io people search.

Apollo returns by far the fattest payload of any source in the factory: alongside the
work email it ships personal email addresses, direct dial and mobile numbers, home
city and country, LinkedIn and Twitter handles, and full employment history.

None of that is necessary to email someone about a B2B tool, so none of it is read.
`_to_candidate` names the six fields it wants and ignores the rest of the response
object; there is no code path in this module that can move `phone_numbers` or
`personal_emails` toward storage, a log line, or a model prompt.

Apollo supplies no primary URL for a contact, so every Apollo record is
`BROKER_ATTESTED` and the outreach footer names Apollo explicitly.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx

from factory.engines.discovery.sources.base import RateLimiter
from factory.engines.marketing.providers.base import (
    LeadCandidate,
    LeadSearchPlan,
    Provenance,
    ProvenanceKind,
    ProviderAuthError,
    ProviderRequestError,
    usable_person_email,
)
from factory.logging import get_logger
from factory.settings import Settings, get_settings

log = get_logger(__name__)

_ENDPOINT = "https://api.apollo.io/api/v1/mixed_people/search"
_LIMITER = RateLimiter(1.5)
_TIMEOUT = httpx.Timeout(30.0, connect=5.0)
_PAGE_SIZE = 25

#: Apollo's own verification states. Anything weaker is a guess about a real person.
_ACCEPTED_EMAIL_STATUS = frozenset({"verified"})


class ApolloProvider:
    name = "apollo.io"

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()

    @property
    def available(self) -> bool:
        return bool(self._settings.secret("APOLLO_API_KEY"))

    async def search(self, plan: LeadSearchPlan, *, limit: int = 25) -> list[LeadCandidate]:
        if not self.available or not plan.is_actionable:
            return []

        body: dict[str, Any] = {
            "page": 1,
            "per_page": min(limit, _PAGE_SIZE),
            "contact_email_status": ["verified"],
        }
        if plan.job_titles:
            body["person_titles"] = plan.job_titles[:12]
        if plan.seniorities:
            body["person_seniorities"] = plan.seniorities[:8]
        if plan.departments:
            body["person_departments"] = plan.departments[:8]
        if plan.company_domains:
            body["q_organization_domains"] = "\n".join(plan.company_domains[:25])
        if plan.company_keywords:
            body["q_organization_keyword_tags"] = plan.company_keywords[:12]
        if plan.locations:
            body["person_locations"] = plan.locations[:8]

        await _LIMITER.wait()
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                response = await client.post(
                    _ENDPOINT,
                    json=body,
                    headers={
                        "Content-Type": "application/json",
                        "Cache-Control": "no-cache",
                        "x-api-key": self._settings.secret("APOLLO_API_KEY") or "",
                    },
                )
        except httpx.HTTPError as exc:
            log.warning("leads.apollo_failed", detail=str(exc)[:200])
            return []

        if response.status_code != 200:
            detail = response.text
            log.warning("leads.apollo_rejected", status=response.status_code, detail=detail[:200])
            # 401/403: the key is present (`available` already checked that) but
            # being refused -- invalid, expired, or the plan/credits do not cover
            # this call. 400/422: the request itself is malformed -- a bug in how
            # this module builds it, not an account problem. Anything else (429
            # rate limit, 5xx) is left as today's graceful degrade: log and return
            # empty, since it may well resolve on its own before anyone needs to
            # act on it.
            if response.status_code in (401, 403):
                raise ProviderAuthError(self.name, response.status_code, detail)
            if response.status_code in (400, 422):
                raise ProviderRequestError(self.name, response.status_code, detail)
            return []

        body_json = response.json()
        people = body_json.get("people", [])
        candidates: list[LeadCandidate] = []
        for person in people:
            candidate = self._to_candidate(person)
            if candidate is not None:
                candidates.append(candidate)

        # Only ever requests page 1 -- there is no pagination loop, so a search
        # matching more people than fit in one _PAGE_SIZE/limit-capped page
        # silently leaves the rest unfetched. Not fixed here (a real pagination
        # loop is a larger change), but no longer silent: logged whenever Apollo
        # itself reports more results existed than this call actually retrieved.
        total_entries = (body_json.get("pagination") or {}).get("total_entries")
        if isinstance(total_entries, int) and total_entries > len(people):
            log.info(
                "leads.apollo_truncated",
                total_entries=total_entries,
                fetched=len(people),
            )

        log.info("leads.apollo_results", returned=len(people), usable=len(candidates))
        return candidates[:limit]

    def _to_candidate(self, person: dict[str, Any]) -> LeadCandidate | None:
        """Read six fields. Everything else Apollo sent is left where it is."""
        email = str(person.get("email") or "").strip().lower()
        status = str(person.get("email_status") or "").strip().lower()
        confidence = 1.0 if status in _ACCEPTED_EMAIL_STATUS else 0.0
        if not usable_person_email(email, confidence):
            return None

        first = str(person.get("first_name") or "").strip()
        last = str(person.get("last_name") or "").strip()
        title = str(person.get("title") or "").strip()
        organisation = person.get("organization") or {}
        company = str(organisation.get("name") or "").strip()
        if not (first and last and title and company):
            return None

        return LeadCandidate(
            first_name=first,
            last_name=last,
            job_title=title,
            company=company,
            work_email=email,
            provenance=Provenance(
                # Apollo aggregates; it cannot point at a page publishing this role,
                # so the recipient is told the data came from a broker.
                kind=ProvenanceKind.BROKER_ATTESTED,
                url=f"https://app.apollo.io/#/people/{person.get('id', '')}",
                provider=self.name,
                retrieved_at=datetime.now(UTC),
                confidence=confidence,
            ),
        )
