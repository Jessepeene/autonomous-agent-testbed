"""Lead provider adapters.

The load-bearing assertion in this file: a realistic Apollo payload — which ships
phone numbers, personal emails, home city and employment history — must produce a
candidate carrying none of it. The adapter is where the fat payload dies.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from factory.engines.compliance.rules import render_compliance_footer
from factory.engines.marketing.providers import build_providers
from factory.engines.marketing.providers.apollo import ApolloProvider
from factory.engines.marketing.providers.base import (
    MIN_EMAIL_CONFIDENCE,
    LeadSearchPlan,
    ProvenanceKind,
    usable_person_email,
)
from factory.engines.marketing.providers.csv_drop import CsvDropProvider
from factory.engines.marketing.providers.hunter import HunterProvider
from factory.settings import Settings
from factory.state.schemas import ALLOWED_LEAD_FIELDS, LeadRecord

# A realistic Apollo person object, including everything we refuse to touch.
FAT_APOLLO_PERSON: dict[str, Any] = {
    "id": "5f3b2c1a",
    "first_name": "Ada",
    "last_name": "Lovelace",
    "title": "Head of Operations",
    "email": "ada@analytical-engines.test",
    "email_status": "verified",
    "organization": {"name": "Analytical Engines BV", "website_url": "https://engines.test"},
    # Everything below must never reach a candidate, the database, a log, or a prompt.
    "personal_emails": ["ada.private@gmail.com"],
    "phone_numbers": [{"raw_number": "+31612345678", "type": "mobile"}],
    "linkedin_url": "https://linkedin.com/in/ada",
    "twitter_url": "https://twitter.com/ada",
    "city": "Amsterdam",
    "state": "North Holland",
    "country": "Netherlands",
    "employment_history": [{"organization_name": "Babbage Ltd", "title": "Analyst"}],
    "photo_url": "https://example.test/ada.jpg",
    "departments": ["operations"],
}

BANNED = (
    "personal_emails",
    "phone_numbers",
    "linkedin_url",
    "twitter_url",
    "city",
    "state",
    "country",
    "employment_history",
    "photo_url",
)


def _plan(**overrides: Any) -> LeadSearchPlan:
    base: dict[str, Any] = {
        "job_titles": ["Head of Operations"],
        "rationale": "operations leaders own the reconciliation workflow",
    }
    base.update(overrides)
    return LeadSearchPlan(**base)


# --- the narrowing guarantee ------------------------------------------------


def test_apollo_adapter_discards_the_fat_payload(settings: Settings) -> None:
    candidate = ApolloProvider(settings)._to_candidate(FAT_APOLLO_PERSON)
    assert candidate is not None

    row = candidate.to_row()
    assert set(row) == ALLOWED_LEAD_FIELDS | {"source_url", "scraped_at"}

    serialised = repr(row) + repr(candidate)
    for banned in BANNED:
        assert banned not in serialised
    assert "+31612345678" not in serialised
    assert "ada.private@gmail.com" not in serialised
    assert "Amsterdam" not in serialised


def test_candidate_row_validates_as_a_lead_record(settings: Settings) -> None:
    """The adapter output must satisfy the same contract the database enforces."""
    candidate = ApolloProvider(settings)._to_candidate(FAT_APOLLO_PERSON)
    assert candidate is not None
    record = LeadRecord(**candidate.to_row())
    assert record.work_email == "ada@analytical-engines.test"
    assert record.source_url.startswith("https://")


def test_apollo_rejects_unverified_emails(settings: Settings) -> None:
    """Guessing at a real person's address is an accuracy failure, not a near miss."""
    guessed = {**FAT_APOLLO_PERSON, "email_status": "guessed"}
    assert ApolloProvider(settings)._to_candidate(guessed) is None


def test_apollo_rejects_records_without_a_role(settings: Settings) -> None:
    untitled = {**FAT_APOLLO_PERSON, "title": ""}
    assert ApolloProvider(settings)._to_candidate(untitled) is None


def test_apollo_records_are_broker_attested(settings: Settings) -> None:
    """Apollo cannot point at a page publishing the role, so it must not claim to."""
    candidate = ApolloProvider(settings)._to_candidate(FAT_APOLLO_PERSON)
    assert candidate is not None
    assert candidate.provenance.kind is ProvenanceKind.BROKER_ATTESTED
    assert candidate.provenance.provider == "apollo.io"


# --- Hunter provenance ------------------------------------------------------

HUNTER_EMAIL: dict[str, Any] = {
    "value": "ada@analytical-engines.test",
    "type": "personal",
    "confidence": 94,
    "first_name": "Ada",
    "last_name": "Lovelace",
    "position": "Head of Operations",
    "sources": [
        {
            "uri": "https://analytical-engines.test/about/team",
            "still_on_page": True,
            "extracted_on": "2026-07-01",
        }
    ],
}


def test_hunter_uses_a_primary_url_when_one_exists(settings: Settings) -> None:
    candidate = HunterProvider(settings)._to_candidate(HUNTER_EMAIL, "Analytical Engines BV")
    assert candidate is not None
    assert candidate.provenance.kind is ProvenanceKind.PRIMARY_SOURCE
    assert candidate.provenance.url == "https://analytical-engines.test/about/team"


def test_hunter_falls_back_to_broker_attested_without_a_live_page(settings: Settings) -> None:
    stale = {**HUNTER_EMAIL, "sources": [{"uri": "https://x.test/old", "still_on_page": False}]}
    candidate = HunterProvider(settings)._to_candidate(stale, "Analytical Engines BV")
    assert candidate is not None
    assert candidate.provenance.kind is ProvenanceKind.BROKER_ATTESTED


def test_hunter_drops_generic_role_mailboxes(settings: Settings) -> None:
    generic = {**HUNTER_EMAIL, "type": "generic", "value": "info@analytical-engines.test"}
    assert HunterProvider(settings)._to_candidate(generic, "Engines") is None


def test_hunter_drops_low_confidence_records(settings: Settings) -> None:
    weak = {**HUNTER_EMAIL, "confidence": 40}
    assert HunterProvider(settings)._to_candidate(weak, "Engines") is None


@pytest.mark.parametrize(
    ("email", "confidence", "expected"),
    [
        ("ada@engines.test", 0.95, True),
        ("ada@engines.test", 0.10, False),
        ("info@engines.test", 0.99, False),
        ("", 0.99, False),
        (None, 0.99, False),
        ("ada@engines.test", MIN_EMAIL_CONFIDENCE, True),
    ],
)
def test_usable_person_email(email: str | None, confidence: float, expected: bool) -> None:
    assert usable_person_email(email, confidence) is expected


# --- provider availability gating -------------------------------------------


async def test_unconfigured_providers_return_nothing(settings: Settings) -> None:
    assert HunterProvider(settings).available is False
    assert ApolloProvider(settings).available is False
    assert await HunterProvider(settings).search(_plan()) == []
    assert await ApolloProvider(settings).search(_plan()) == []


def test_build_providers_skips_unconfigured(settings: Settings) -> None:
    assert build_providers(settings) == []

    configured = settings.model_copy(update={"apollo_api_key": "key"})
    names = [p.name for p in build_providers(configured)]
    assert names == ["apollo.io"]


async def test_hunter_without_domains_makes_no_request(settings: Settings) -> None:
    """Hunter is domain-scoped; a plan with no domains must not burn a credit."""
    configured = settings.model_copy(update={"hunter_api_key": "key"})
    provider = HunterProvider(configured)
    assert provider.available is True
    assert await provider.search(_plan(company_domains=[])) == []


def test_lead_tools_are_withheld_without_credentials(settings: Settings) -> None:
    from factory.tools import builtin  # noqa: F401 - import populates REGISTRY
    from factory.tools.registry import REGISTRY

    assert REGISTRY.get_mcp_tools(category=["leads"], settings=settings) == []

    with_apollo = settings.model_copy(update={"apollo_api_key": "key"})
    offered = REGISTRY.get_mcp_tools(category=["leads"], settings=with_apollo)
    assert [t.name for t in offered] == ["find_leads_apollo"]


# --- CSV drop still works as the manual override ----------------------------


async def test_csv_drop_reads_operator_files(settings: Settings) -> None:
    from factory.engines.marketing.providers.csv_drop import drop_directory

    directory = drop_directory(settings)
    (directory / "vetted.csv").write_text(
        "first_name,last_name,job_title,company,work_email,source_url\n"
        "Ada,Lovelace,Head of Ops,Engines BV,ada@engines.test,https://engines.test/team\n",
        encoding="utf-8",
    )
    provider = CsvDropProvider(settings)
    assert provider.available is True

    candidates = await provider.search(_plan())
    assert len(candidates) == 1
    assert candidates[0].provenance.kind is ProvenanceKind.OPERATOR_SUPPLIED


async def test_csv_rows_without_provenance_are_dropped(settings: Settings) -> None:
    from factory.engines.marketing.providers.csv_drop import drop_directory

    directory = drop_directory(settings)
    (directory / "bad.csv").write_text(
        "first_name,last_name,job_title,company,work_email,source_url\n"
        "Ada,Lovelace,Head of Ops,Engines BV,ada@engines.test,\n",
        encoding="utf-8",
    )
    assert await CsvDropProvider(settings).search(_plan()) == []


# --- Art. 14 disclosure -----------------------------------------------------


def test_broker_footer_does_not_claim_a_published_page() -> None:
    """Saying "published at" about a broker record would be a false disclosure."""
    footer = render_compliance_footer(
        postal_address="Testlaan 1",
        unsubscribe_url="https://example.com/u/tok",
        source_url="https://app.apollo.io/#/people/5f3b2c1a",
        provenance_kind="broker_attested",
        provider="apollo.io",
    )
    assert "apollo.io" in footer
    assert "contact database" in footer
    assert "is published at" not in footer


def test_primary_source_footer_names_the_page() -> None:
    footer = render_compliance_footer(
        postal_address="Testlaan 1",
        unsubscribe_url="https://example.com/u/tok",
        source_url="https://engines.test/team",
        provenance_kind="primary_source",
    )
    assert "https://engines.test/team" in footer
    assert "is published at" in footer


@pytest.mark.parametrize("kind", ["primary_source", "broker_attested", "operator_supplied"])
def test_every_footer_states_the_lawful_basis_and_an_opt_out(kind: str) -> None:
    footer = render_compliance_footer(
        postal_address="Testlaan 1, Amsterdam",
        unsubscribe_url="https://example.com/u/tok",
        source_url="https://engines.test/team",
        provenance_kind=kind,
        provider="apollo.io",
    )
    assert "legitimate interest" in footer
    assert "https://example.com/u/tok" in footer
    assert "Testlaan 1, Amsterdam" in footer


def test_search_plan_requires_something_to_search_on() -> None:
    empty = LeadSearchPlan(rationale="nothing specific enough to act on")
    assert empty.is_actionable is False
    assert _plan().is_actionable is True


def test_planner_prompt_contains_no_personal_data() -> None:
    """Deciding who to look for is reasoning about a market, not about a person."""
    from factory.engines.marketing.leads import PLANNER_SYSTEM

    lowered = PLANNER_SYSTEM.lower()
    for term in ("email", "phone", "first_name", "last_name", "individual's"):
        assert term not in lowered


def test_scraped_at_is_timezone_aware(settings: Settings) -> None:
    candidate = ApolloProvider(settings)._to_candidate(FAT_APOLLO_PERSON)
    assert candidate is not None
    assert candidate.provenance.retrieved_at.tzinfo is not None
    assert candidate.provenance.retrieved_at <= datetime.now(UTC)
