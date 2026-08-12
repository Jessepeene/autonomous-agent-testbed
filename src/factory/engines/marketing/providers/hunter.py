"""Hunter.io domain search.

Hunter is the stronger provenance source of the two adapters: each result carries a
``sources[]`` array of the actual URLs where the address was found published. When
one of those is usable we record it as a `PRIMARY_SOURCE`, which lets the outreach
footer name a real page rather than a broker.
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

_ENDPOINT = "https://api.hunter.io/v2/domain-search"
_LIMITER = RateLimiter(1.0)
_TIMEOUT = httpx.Timeout(30.0, connect=5.0)


class HunterProvider:
    name = "hunter.io"

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()

    @property
    def available(self) -> bool:
        return bool(self._settings.secret("HUNTER_API_KEY"))

    async def search(self, plan: LeadSearchPlan, *, limit: int = 25) -> list[LeadCandidate]:
        """Hunter searches by domain, so a plan with no domains yields nothing."""
        if not self.available:
            log.info("leads.hunter_skipped", reason="HUNTER_API_KEY is not configured")
            return []
        if not plan.company_domains:
            log.info("leads.hunter_skipped", reason="plan carried no company_domains")
            return []

        key = self._settings.secret("HUNTER_API_KEY")
        per_domain = max(1, limit // len(plan.company_domains))
        candidates: list[LeadCandidate] = []

        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            for domain in plan.company_domains[:25]:
                if len(candidates) >= limit:
                    break
                await _LIMITER.wait()
                params: dict[str, str] = {
                    "domain": domain,
                    "api_key": key or "",
                    "limit": str(per_domain),
                    "type": "personal",  # exclude generic role mailboxes at the API
                }
                if plan.seniorities:
                    params["seniority"] = ",".join(plan.seniorities[:3])
                if plan.departments:
                    params["department"] = ",".join(plan.departments[:3])

                try:
                    response = await client.get(_ENDPOINT, params=params)
                except httpx.HTTPError as exc:
                    log.warning("leads.hunter_failed", domain=domain, detail=str(exc)[:200])
                    continue
                if response.status_code != 200:
                    detail = response.text
                    log.warning(
                        "leads.hunter_rejected", status=response.status_code, detail=detail[:200]
                    )
                    # The key applies uniformly across every domain in this loop, so
                    # one 401/403 already proves the account itself is the problem --
                    # no reason to keep hammering the remaining domains with the same
                    # rejected key. Same reasoning as apollo.py for the status split.
                    if response.status_code in (401, 403):
                        raise ProviderAuthError(self.name, response.status_code, detail)
                    if response.status_code in (400, 422):
                        raise ProviderRequestError(self.name, response.status_code, detail)
                    continue

                try:
                    payload = response.json().get("data", {})
                except ValueError as exc:
                    # A malformed body for one domain must not abort every domain
                    # after it in this loop and discard whatever was already found.
                    log.warning("leads.hunter_bad_json", domain=domain, detail=str(exc)[:200])
                    continue
                organisation = str(payload.get("organization") or domain)
                for entry in payload.get("emails", []):
                    candidate = self._to_candidate(entry, organisation)
                    if candidate is not None:
                        candidates.append(candidate)

        log.info("leads.hunter_results", count=len(candidates))
        return candidates[:limit]

    def _to_candidate(self, entry: dict[str, Any], organisation: str) -> LeadCandidate | None:
        """Narrow one Hunter result. Everything not named here is discarded."""
        email = str(entry.get("value") or "").strip().lower()
        confidence = float(entry.get("confidence") or 0) / 100.0
        if entry.get("type") == "generic" or not usable_person_email(email, confidence):
            return None

        first = str(entry.get("first_name") or "").strip()
        last = str(entry.get("last_name") or "").strip()
        title = str(entry.get("position") or "").strip()
        if not (first and last and title):
            # Without a role we cannot argue a role-to-utility link, so the record is
            # useless to us and we do not keep it.
            return None

        return LeadCandidate(
            first_name=first,
            last_name=last,
            job_title=title,
            company=organisation,
            work_email=email,
            provenance=self._provenance(entry, email, confidence),
        )

    def _provenance(self, entry: dict[str, Any], email: str, confidence: float) -> Provenance:
        for source in entry.get("sources") or []:
            uri = str(source.get("uri") or "")
            if uri.startswith(("http://", "https://")) and source.get("still_on_page", True):
                return Provenance(
                    kind=ProvenanceKind.PRIMARY_SOURCE,
                    url=uri,
                    provider=self.name,
                    retrieved_at=datetime.now(UTC),
                    confidence=confidence,
                )
        # Hunter knows the address but can no longer point at a live page for it.
        return Provenance(
            kind=ProvenanceKind.BROKER_ATTESTED,
            url=f"https://hunter.io/search/{email.partition('@')[2]}",
            provider=self.name,
            retrieved_at=datetime.now(UTC),
            confidence=confidence,
        )
