"""GDPR controls: minimisation, provenance, and the deterministic lead rules."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from hypothesis import given
from hypothesis import settings as hyp_settings
from hypothesis import strategies as st
from pydantic import ValidationError

from factory.engines.compliance.minimize import (
    ProvenanceMissing,
    dropped_fields,
    minimize,
)
from factory.engines.compliance.rules import (
    MAX_CONTACTS_PER_DOMAIN_PER_MONTH,
    check_lead_eligibility,
    check_outreach_content,
    render_compliance_footer,
)
from factory.state.schemas import ALLOWED_LEAD_FIELDS, LeadRecord

BASE_ROW = {
    "first_name": "Ada",
    "last_name": "Lovelace",
    "job_title": "Head of Operations",
    "company": "Analytical Engines BV",
    "work_email": "ada@analytical-engines.test",
    "source_url": "https://analytical-engines.test/team",
    "scraped_at": "2026-08-01T00:00:00+00:00",
}


def test_minimize_keeps_only_permitted_fields() -> None:
    record = minimize(BASE_ROW)
    assert set(record.model_dump()) == ALLOWED_LEAD_FIELDS | {"source_url", "scraped_at"}


def test_minimize_drops_extraneous_scraped_data() -> None:
    noisy = {
        **BASE_ROW,
        "phone": "+31 6 12345678",
        "home_address": "Somestraat 4",
        "linkedin_id": "ada-lovelace-1815",
        "photo_url": "https://example.test/ada.jpg",
        "date_of_birth": "1815-12-10",
    }
    assert dropped_fields(noisy) == {
        "phone",
        "home_address",
        "linkedin_id",
        "photo_url",
        "date_of_birth",
    }
    record = minimize(noisy)
    dumped = record.model_dump()
    for banned in ("phone", "home_address", "linkedin_id", "photo_url", "date_of_birth"):
        assert banned not in dumped


@hyp_settings(max_examples=200)
@given(
    extra=st.dictionaries(
        keys=st.text(min_size=1, max_size=30).filter(
            lambda k: k not in ALLOWED_LEAD_FIELDS and k not in {"source_url", "scraped_at"}
        ),
        values=st.text(max_size=50),
        max_size=12,
    )
)
def test_minimize_never_leaks_arbitrary_keys(extra: dict[str, str]) -> None:
    """Whatever a scraper invents, nothing beyond the permitted set survives."""
    record = minimize({**BASE_ROW, **extra})
    assert set(record.model_dump()) == ALLOWED_LEAD_FIELDS | {"source_url", "scraped_at"}


def test_minimize_requires_provenance() -> None:
    row = {k: v for k, v in BASE_ROW.items() if k != "source_url"}
    with pytest.raises(ProvenanceMissing):
        minimize(row)


def test_minimize_rejects_blank_provenance() -> None:
    with pytest.raises(ProvenanceMissing):
        minimize({**BASE_ROW, "source_url": "   "})


def test_minimize_rejects_missing_required_field() -> None:
    row = {k: v for k, v in BASE_ROW.items() if k != "job_title"}
    with pytest.raises(ValueError, match="job_title"):
        minimize(row)


def test_lead_record_rejects_relative_source_url() -> None:
    with pytest.raises(ValidationError):
        LeadRecord(
            first_name="A",
            last_name="B",
            job_title="C",
            company="D",
            work_email="a@b.test",
            source_url="/team",
            scraped_at=datetime.now(UTC),
        )


def test_lead_record_rejects_extra_fields() -> None:
    with pytest.raises(ValidationError):
        LeadRecord(
            **{
                "first_name": "A",
                "last_name": "B",
                "job_title": "C",
                "company": "D",
                "work_email": "a@b.test",
                "source_url": "https://b.test/team",
                "scraped_at": datetime.now(UTC),
                "phone": "+31000000000",
            }
        )


def test_scraped_at_defaults_to_now_when_absent() -> None:
    row = {k: v for k, v in BASE_ROW.items() if k != "scraped_at"}
    record = minimize(row)
    assert record.scraped_at.tzinfo is not None


# --- deterministic eligibility rules ---------------------------------------


def test_personal_mailbox_is_refused(lead: LeadRecord) -> None:
    personal = lead.model_copy(update={"work_email": "ada@gmail.com"})
    result = check_lead_eligibility(personal)
    assert result.passed is False
    assert any("personal mailbox" in v for v in result.violations)


def test_role_address_is_refused(lead: LeadRecord) -> None:
    role = lead.model_copy(update={"work_email": "info@analytical-engines.test"})
    result = check_lead_eligibility(role)
    assert result.passed is False
    assert any("role address" in v for v in result.violations)


def test_domain_frequency_cap_is_enforced(lead: LeadRecord) -> None:
    result = check_lead_eligibility(lead, contacts_this_month=MAX_CONTACTS_PER_DOMAIN_PER_MONTH)
    assert result.passed is False
    assert any("cap" in v for v in result.violations)


def test_ordinary_business_lead_passes(lead: LeadRecord) -> None:
    assert check_lead_eligibility(lead, contacts_this_month=0).passed is True


# --- outbound content rules -------------------------------------------------


def test_outreach_requires_unsubscribe_and_postal_address() -> None:
    result = check_outreach_content(
        "Hi there, thought this might help.",
        from_address="factory@example.com",
        postal_address="Testlaan 1, Amsterdam",
        unsubscribe_url="https://example.com/u/tok",
    )
    assert result.passed is False
    assert any("unsubscribe link" in v for v in result.violations)


def test_compliant_message_passes() -> None:
    postal = "Testlaan 1, 1000 AA Amsterdam, NL"
    unsubscribe = "https://example.com/u/tok"
    body = "Hi there, thought this might help." + render_compliance_footer(
        postal_address=postal,
        unsubscribe_url=unsubscribe,
        source_url="https://analytical-engines.test/team",
    )
    result = check_outreach_content(
        body,
        from_address="factory@example.com",
        postal_address=postal,
        unsubscribe_url=unsubscribe,
    )
    assert result.passed is True, result.violations


@pytest.mark.parametrize(
    "phrase",
    ["Act now", "buy NOW", "Click here", "limited time", "risk-free", "100% free"],
)
def test_spam_trigger_phrases_fail_the_content_check(phrase: str) -> None:
    postal = "Testlaan 1, 1000 AA Amsterdam, NL"
    unsubscribe = "https://example.com/u/tok"
    body = f"Hi there, {phrase} to see the tool." + render_compliance_footer(
        postal_address=postal,
        unsubscribe_url=unsubscribe,
        source_url="https://analytical-engines.test/team",
    )
    result = check_outreach_content(
        body,
        from_address="factory@example.com",
        postal_address=postal,
        unsubscribe_url=unsubscribe,
    )
    assert result.passed is False
    assert any("spam-trigger" in v for v in result.violations)


def test_a_common_word_that_is_not_a_multiword_trigger_does_not_fail() -> None:
    """Single common words ("free", "cash") are deliberately not on the list — too
    many false positives in legitimate B2B copy."""
    postal = "Testlaan 1, 1000 AA Amsterdam, NL"
    unsubscribe = "https://example.com/u/tok"
    body = "This is free to try and takes five minutes to set up." + render_compliance_footer(
        postal_address=postal,
        unsubscribe_url=unsubscribe,
        source_url="https://analytical-engines.test/team",
    )
    result = check_outreach_content(
        body,
        from_address="factory@example.com",
        postal_address=postal,
        unsubscribe_url=unsubscribe,
    )
    assert result.passed is True, result.violations


def test_footer_names_the_data_source() -> None:
    """GDPR Art. 14: a person we never met is entitled to know where we got them."""
    footer = render_compliance_footer(
        postal_address="Testlaan 1",
        unsubscribe_url="https://example.com/u/tok",
        source_url="https://analytical-engines.test/team",
    )
    assert "https://analytical-engines.test/team" in footer
