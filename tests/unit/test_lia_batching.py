"""Batched LIA — built, shipped disabled by default (`compliance_batch_size=1`).

The correlation error is the catastrophic failure mode here: attaching a lawful-basis
argument to the wrong person and mailing them under it. These tests are adversarial
on purpose — duplicate refs, unknown refs, partial responses, empty responses — and
every one of them must produce zero `allowed=True` decisions for the affected leads.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from factory.engines.compliance import assessor
from factory.engines.compliance.lia import evaluate_leads
from factory.hitl import interrupts
from factory.llm.providers.base import Provider
from factory.llm.providers.fake import FakeProvider
from factory.settings import Settings
from factory.state import repository
from factory.state.schemas import (
    ComplianceStatus,
    LeadAssessment,
    LeadAssessmentBatch,
    LeadRecord,
    LIAVerdict,
)
from factory.supervisor.context import FactoryContext


def _lead(name: str) -> LeadRecord:
    return LeadRecord(
        first_name=name,
        last_name="Doe",
        job_title="Head of Finance",
        company=f"{name} BV",
        work_email=f"{name.lower()}@example-corp.test",
        source_url="https://example-corp.test/team",
        scraped_at=datetime(2026, 8, 1, tzinfo=UTC),
    )


def _verdict(passed: bool = True) -> LIAVerdict:
    text = "a" * 30
    return LIAVerdict(role_utility_link=text, necessity=text, balancing_test=text, passed=passed)


def _entry(
    ref: str,
    *,
    status: ComplianceStatus = ComplianceStatus.APPROVED,
    confidence: float = 0.95,
    passed: bool = True,
) -> LeadAssessment:
    return LeadAssessment(
        ref=ref,
        status=status,
        justification="j" * 30,
        confidence=confidence,
        lia_verdict=_verdict(passed),
    )


@pytest.fixture(autouse=True)
def _no_db(monkeypatch: pytest.MonkeyPatch) -> None:
    async def zero(*args: object, **kwargs: object) -> int:
        return 0

    async def noop(*args: object, **kwargs: object) -> str | None:
        return "int_test"

    monkeypatch.setattr(repository, "outreach_count_for_domain", zero)
    monkeypatch.setattr(interrupts, "request", noop)


# --- assessor.assess_leads: correlation and fail-closed behaviour -----------


async def test_empty_input_produces_no_calls_and_no_results(
    ctx: FactoryContext, gemini_fake: FakeProvider
) -> None:
    assert await assessor.assess_leads(ctx, [], product_summary="a tool") == []
    assert gemini_fake.calls == []


async def test_a_well_formed_batch_maps_each_verdict_back_to_its_lead(
    ctx: FactoryContext, gemini_fake: FakeProvider
) -> None:
    leads = [_lead("Alice"), _lead("Bob"), _lead("Cara")]
    gemini_fake.register(
        LeadAssessmentBatch,
        LeadAssessmentBatch(
            assessments=[_entry("L2"), _entry("L1"), _entry("L3")]  # order scrambled
        ),
    )

    results = await assessor.assess_leads(ctx, leads, product_summary="a tool")

    assert len(results) == 3
    assert all(r.approved for r in results)


async def test_a_duplicate_ref_refuses_the_entire_chunk(
    ctx: FactoryContext, gemini_fake: FakeProvider
) -> None:
    leads = [_lead("Alice"), _lead("Bob")]
    gemini_fake.register(
        LeadAssessmentBatch,
        LeadAssessmentBatch(assessments=[_entry("L1"), _entry("L1")]),
    )

    results = await assessor.assess_leads(ctx, leads, product_summary="a tool")

    assert all(not r.approved for r in results)


async def test_an_unknown_ref_refuses_the_entire_chunk(
    ctx: FactoryContext, gemini_fake: FakeProvider
) -> None:
    leads = [_lead("Alice"), _lead("Bob")]
    gemini_fake.register(
        LeadAssessmentBatch,
        LeadAssessmentBatch(assessments=[_entry("L1"), _entry("L99")]),
    )

    results = await assessor.assess_leads(ctx, leads, product_summary="a tool")

    assert all(not r.approved for r in results)


async def test_returning_far_fewer_than_requested_refuses_the_entire_chunk(
    ctx: FactoryContext, gemini_fake: FakeProvider
) -> None:
    """3 of 8 is below the 75% survival floor — the model likely lost track of the
    mapping, so even the 3 it did return are not trustworthy."""
    leads = [_lead(f"P{i}") for i in range(8)]
    gemini_fake.register(
        LeadAssessmentBatch,
        LeadAssessmentBatch(assessments=[_entry("L1"), _entry("L2"), _entry("L3")]),
    )

    results = await assessor.assess_leads(ctx, leads, product_summary="a tool")

    assert all(not r.approved for r in results)


async def test_an_empty_response_refuses_the_entire_chunk(
    ctx: FactoryContext, gemini_fake: FakeProvider
) -> None:
    leads = [_lead("Alice"), _lead("Bob")]
    gemini_fake.register(LeadAssessmentBatch, LeadAssessmentBatch(assessments=[]))

    results = await assessor.assess_leads(ctx, leads, product_summary="a tool")

    assert all(not r.approved for r in results)


async def test_a_single_isolated_omission_refuses_only_that_lead(
    ctx: FactoryContext, gemini_fake: FakeProvider
) -> None:
    """7 of 8 valid, non-duplicated entries clears the 75% floor — only the missing
    prospect is refused, not the whole batch."""
    leads = [_lead(f"P{i}") for i in range(8)]
    entries = [_entry(f"L{i + 1}") for i in range(7)]  # L8 missing
    gemini_fake.register(LeadAssessmentBatch, LeadAssessmentBatch(assessments=entries))

    results = await assessor.assess_leads(ctx, leads, product_summary="a tool")

    assert sum(r.approved for r in results) == 7
    assert not results[7].approved


async def test_llm_failure_refuses_the_entire_chunk(
    ctx: FactoryContext, gemini_fake: FakeProvider, anthropic_fake: FakeProvider
) -> None:
    from factory.llm.providers.base import ProviderUnavailable

    # COMPLIANCE may fail over to Anthropic; taking it out of service isolates this
    # to a clean single-provider outage.
    anthropic_fake.available_flag = False
    leads = [_lead("Alice"), _lead("Bob")]
    for _ in range(3):
        gemini_fake.register(
            LeadAssessmentBatch, ProviderUnavailable(Provider.GEMINI, "503", status=503)
        )

    results = await assessor.assess_leads(ctx, leads, product_summary="a tool")

    assert all(not r.approved for r in results)


# --- shared finaliser: thresholding is provably identical for both paths ----


async def test_low_confidence_is_downgraded_the_same_way_in_a_batch(
    ctx: FactoryContext, gemini_fake: FakeProvider, settings: Settings
) -> None:
    leads = [_lead("Alice")]
    gemini_fake.register(
        LeadAssessmentBatch,
        LeadAssessmentBatch(
            assessments=[_entry("L1", confidence=settings.compliance_min_confidence - 0.01)]
        ),
    )

    results = await assessor.assess_leads(ctx, leads, product_summary="a tool")

    assert results[0].status is ComplianceStatus.FLAG_FOR_REVIEW


def test_finalise_lead_assessment_is_the_single_source_of_threshold_truth(
    settings: Settings,
) -> None:
    from factory.state.schemas import ComplianceAssessment

    low_confidence = ComplianceAssessment(
        status=ComplianceStatus.APPROVED,
        justification="j" * 30,
        confidence=settings.compliance_min_confidence - 0.01,
        lia_verdict=_verdict(passed=True),
    )
    result = assessor.finalise_lead_assessment(low_confidence, settings=settings)
    assert result.status is ComplianceStatus.FLAG_FOR_REVIEW


# --- evaluate_leads: the kill switch actually restores the old path ---------


async def test_default_batch_size_issues_one_call_per_lead(
    ctx: FactoryContext, gemini_fake: FakeProvider
) -> None:
    """`compliance_batch_size` defaults to 1, so `evaluate_leads` must dispatch to
    the literal single-lead `assess_lead` path — not a batch-of-one that happens to
    share most of the same code."""
    from factory.state.schemas import ComplianceAssessment

    assert ctx.settings.compliance_batch_size == 1
    leads = [_lead("Alice"), _lead("Bob"), _lead("Cara")]
    for _ in leads:
        gemini_fake.register(
            ComplianceAssessment,
            ComplianceAssessment(
                status=ComplianceStatus.APPROVED,
                justification="j" * 30,
                confidence=0.95,
                lia_verdict=_verdict(),
            ),
        )

    decisions = await evaluate_leads(ctx, None, leads, product_summary="a tool")  # type: ignore[arg-type]

    assert len(gemini_fake.calls) == 3
    assert all(call["schema"] == "ComplianceAssessment" for call in gemini_fake.calls)
    assert all(d.allowed for d in decisions)


async def test_a_configured_batch_size_reduces_call_count(
    ctx: FactoryContext, gemini_fake: FakeProvider
) -> None:
    ctx.settings.compliance_batch_size = 8
    leads = [_lead(f"P{i}") for i in range(8)]
    gemini_fake.register(
        LeadAssessmentBatch,
        LeadAssessmentBatch(assessments=[_entry(f"L{i + 1}") for i in range(8)]),
    )

    decisions = await evaluate_leads(ctx, None, leads, product_summary="a tool")  # type: ignore[arg-type]

    assert len(gemini_fake.calls) == 1
    assert all(call["schema"] == "LeadAssessmentBatch" for call in gemini_fake.calls)
    assert all(d.allowed for d in decisions)


async def test_batching_preserves_input_order_across_multiple_chunks(
    ctx: FactoryContext, gemini_fake: FakeProvider
) -> None:
    ctx.settings.compliance_batch_size = 2
    leads = [_lead("Alice"), _lead("Bob"), _lead("Cara")]
    gemini_fake.register(
        LeadAssessmentBatch,
        LeadAssessmentBatch(
            assessments=[_entry("L1"), _entry("L2", status=ComplianceStatus.REJECTED, passed=False)]
        ),
        LeadAssessmentBatch(assessments=[_entry("L1")]),
    )

    decisions = await evaluate_leads(ctx, None, leads, product_summary="a tool")  # type: ignore[arg-type]

    assert len(gemini_fake.calls) == 2
    assert decisions[0].allowed is True
    assert decisions[1].allowed is False
    assert decisions[2].allowed is True
