"""B2B lead provider interface.

The design principle here is **narrow at the edge**: each adapter converts a
provider's response into a `LeadCandidate` — five PII fields plus provenance — inside
the adapter itself. The fat payload (phone numbers, personal emails, home cities,
employment history, LinkedIn IDs) is dropped at the HTTP boundary and never travels
further into the system.

`minimize()` still runs again at the database boundary. That is deliberate
redundancy: the adapter narrows, the repository enforces. A buggy adapter cannot
widen the `leads` table, and a new adapter written later inherits the guarantee
without having to remember it.

**A compliant provider does not make the processing compliant.** Buying data from a
licensed aggregator removes the scraping question and nothing else: the provider's
lawful basis does not transfer to us, so we still run our own LIA, and because the
data did not come from the data subject, GDPR Art. 14 obliges us to tell them at
first contact *where we got it*. That is what `ProvenanceKind` exists to make
possible — a footer that says "we bought your details from a broker" is a different
sentence from "your role is published on your employer's site", and only one of them
is true for a broker record.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol

from pydantic import Field

from factory.state.schemas import Contract


class ProvenanceKind(StrEnum):
    """Where a record actually came from, which drives the Art. 14 disclosure."""

    #: A public URL where this person's role is published (their employer's team
    #: page, a conference speaker list). The strongest disclosure available.
    PRIMARY_SOURCE = "primary_source"

    #: A B2B data provider asserts the record; no primary URL was supplied. Lawful,
    #: but the recipient must be told it came from a broker, and named.
    BROKER_ATTESTED = "broker_attested"

    #: A human operator vouched for the record (the CSV path).
    OPERATOR_SUPPLIED = "operator_supplied"


@dataclass(frozen=True, slots=True)
class Provenance:
    kind: ProvenanceKind
    url: str
    provider: str
    retrieved_at: datetime
    confidence: float = 1.0


@dataclass(frozen=True, slots=True)
class LeadCandidate:
    """A prospect, already reduced to the permitted surface.

    There is intentionally no field for anything else. An adapter that wants to keep
    a phone number has nowhere to put it.
    """

    first_name: str
    last_name: str
    job_title: str
    company: str
    work_email: str
    provenance: Provenance

    def to_row(self) -> dict[str, Any]:
        """The only shape handed to the repository."""
        return {
            "first_name": self.first_name,
            "last_name": self.last_name,
            "job_title": self.job_title,
            "company": self.company,
            "work_email": self.work_email,
            "source_url": self.provenance.url,
            "scraped_at": self.provenance.retrieved_at,
        }


class LeadSearchPlan(Contract):
    """Search parameters derived from a product's target niche.

    Produced by a model from the *product description only* — no personal data is in
    the prompt that generates it, so planning who to look for never involves
    processing anyone's details.
    """

    job_titles: list[str] = Field(default_factory=list, max_length=12)
    seniorities: list[str] = Field(default_factory=list, max_length=8)
    departments: list[str] = Field(default_factory=list, max_length=8)
    company_domains: list[str] = Field(default_factory=list, max_length=25)
    company_keywords: list[str] = Field(default_factory=list, max_length=12)
    locations: list[str] = Field(default_factory=list, max_length=8)
    rationale: str = Field(min_length=10, max_length=800)

    @property
    def is_actionable(self) -> bool:
        return bool(self.job_titles or self.company_domains or self.company_keywords)


class LeadProvider(Protocol):
    name: str

    @property
    def available(self) -> bool:
        """False when the API key is absent; the registry then withholds the tool."""
        ...

    async def search(self, plan: LeadSearchPlan, *, limit: int = 25) -> list[LeadCandidate]: ...


class ProviderAuthError(Exception):
    """The provider rejected the request over the credential itself -- invalid,
    expired, or lacking the plan/credits/permission for this call (HTTP 401/403).
    `available` was true (a key is configured), so this is not
    MISSING_CREDENTIAL's "absent entirely" -- it is present and being refused.

    Retrying with the same key will fail identically; only a human fixing the
    provider account can resolve this. See marketing/graph.py's immediate
    escalation on this exception -- there is no reason to wait a day to ask
    for something a human could fix in two minutes, the same way this factory
    already asks immediately for a genuinely missing credential.
    """

    def __init__(self, provider: str, status_code: int, detail: str) -> None:
        self.provider = provider
        self.status_code = status_code
        self.detail = detail[:300]
        super().__init__(f"{provider} rejected the request (HTTP {status_code}): {self.detail}")


class ProviderRequestError(Exception):
    """The provider rejected the request itself as malformed (HTTP 400/422) --
    a bug in how this factory calls the provider, not an account problem.
    Retrying the identical request will fail identically; this needs a code
    fix, not anything a human can do in the provider's own dashboard."""

    def __init__(self, provider: str, status_code: int, detail: str) -> None:
        self.provider = provider
        self.status_code = status_code
        self.detail = detail[:300]
        super().__init__(f"{provider} rejected the request (HTTP {status_code}): {self.detail}")


#: Below this the record is treated as unverified and discarded. This is an accuracy
#: obligation (GDPR Art. 5(1)(d)) before it is a deliverability preference: emailing
#: a guessed address means processing a real person's data on a wrong assumption.
MIN_EMAIL_CONFIDENCE = 0.80


def is_role_localpart(email: str) -> bool:
    """Role mailboxes reach a function, not a person who can be shown a basis."""
    from factory.engines.compliance.rules import ROLE_ADDRESS_LOCALPARTS

    return email.partition("@")[0].strip().lower() in ROLE_ADDRESS_LOCALPARTS


def usable_person_email(email: str | None, confidence: float) -> bool:
    if not email or "@" not in email:
        return False
    if confidence < MIN_EMAIL_CONFIDENCE:
        return False
    return not is_role_localpart(email)
