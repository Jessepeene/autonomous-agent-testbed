"""Autonomous lead-sourcing strategy refinement.

`refine_sourcing_strategy` is where "the Marketing Engine designs its own sourcing
strategy" actually happens: a LEAD_SCOUT agent tests the planner's draft against its
real (PII-redacted) search tools and may adjust it before anything is executed for
real against a paying data provider. Refinement is a strict enhancement over the
draft — every failure mode here must fall back to the draft unchanged, never block
ingestion outright.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from factory.engines.marketing.leads import refine_sourcing_strategy
from factory.engines.marketing.providers import LeadSearchPlan
from factory.llm.providers.base import AgentTurn, LLMError, Provider, Usage
from factory.llm.providers.fake import FakeProvider
from factory.supervisor.context import FactoryContext

_DRAFT = LeadSearchPlan(
    job_titles=["Head of Finance"],
    rationale="finance leads own the reconciliation workflow",
)


def _agent_turn(text: str) -> AgentTurn:
    return AgentTurn(
        text=text,
        tool_calls=[],
        stop_reason="end_turn",
        provider=Provider.GEMINI,
        model="gemini-2.5-flash",
        usage=Usage(input_tokens=10, output_tokens=10),
    )


async def test_a_settled_refinement_replaces_the_draft(
    ctx: FactoryContext, gemini_fake: FakeProvider, memory_session: AsyncSession
) -> None:
    gemini_fake.register_turns(
        _agent_turn("Tested Head of Finance; broadened to include Financial Controller too.")
    )
    refined = LeadSearchPlan(
        job_titles=["Head of Finance", "Financial Controller"],
        rationale="both titles turned up real results when tested",
    )
    gemini_fake.register(LeadSearchPlan, refined)

    result = await refine_sourcing_strategy(
        ctx, memory_session, _DRAFT, product_summary="A reconciliation tool for bookkeepers."
    )

    assert result.job_titles == ["Head of Finance", "Financial Controller"]


async def test_an_agent_that_does_not_complete_falls_back_to_the_draft(
    ctx: FactoryContext,
    gemini_fake: FakeProvider,
    anthropic_fake: FakeProvider,
    memory_session: AsyncSession,
) -> None:
    """No `LeadSearchPlan` response is queued — if extraction were attempted anyway,
    `FakeProvider` would raise, failing the test."""
    from factory.llm.providers.base import ProviderUnavailable

    # ANALYST/ANALYSIS-typed calls may fail over to Anthropic; taking it out of
    # service means this is a clean outage rather than needing queued failures on
    # both providers.
    anthropic_fake.available_flag = False
    for _ in range(3):  # LLMClient retries a transport failure 3x before giving up.
        gemini_fake.register_turns(ProviderUnavailable(Provider.GEMINI, "503", status=503))

    result = await refine_sourcing_strategy(
        ctx, memory_session, _DRAFT, product_summary="A reconciliation tool for bookkeepers."
    )

    assert result is _DRAFT


async def test_extraction_failure_falls_back_to_the_draft(
    ctx: FactoryContext, gemini_fake: FakeProvider, memory_session: AsyncSession
) -> None:
    gemini_fake.register_turns(_agent_turn("Tested it; the original plan already works well."))
    gemini_fake.register(LeadSearchPlan, LLMError(Provider.GEMINI, "schema violation"))

    result = await refine_sourcing_strategy(
        ctx, memory_session, _DRAFT, product_summary="A reconciliation tool for bookkeepers."
    )

    assert result is _DRAFT


async def test_a_non_actionable_refinement_falls_back_to_the_draft(
    ctx: FactoryContext, gemini_fake: FakeProvider, memory_session: AsyncSession
) -> None:
    gemini_fake.register_turns(_agent_turn("Nothing turned up anything usable."))
    gemini_fake.register(LeadSearchPlan, LeadSearchPlan(rationale="no usable parameters found"))

    result = await refine_sourcing_strategy(
        ctx, memory_session, _DRAFT, product_summary="A reconciliation tool for bookkeepers."
    )

    assert result is _DRAFT


async def test_refinement_only_offers_the_leads_tool_category(
    ctx: FactoryContext, gemini_fake: FakeProvider, memory_session: AsyncSession
) -> None:
    """LEAD_SCOUT's fixed tool_categories is the containment — refinement must not
    widen it, even implicitly."""
    from factory.tools import builtin  # noqa: F401 - import populates REGISTRY

    gemini_fake.register_turns(_agent_turn("The plan already works."))
    gemini_fake.register(LeadSearchPlan, _DRAFT)

    await refine_sourcing_strategy(
        ctx, memory_session, _DRAFT, product_summary="A reconciliation tool for bookkeepers."
    )

    tools_sent = gemini_fake.conversations[0]["tools"]
    tool_names = {t["name"] for t in tools_sent}
    assert tool_names <= {"find_leads_hunter", "find_leads_apollo"}
