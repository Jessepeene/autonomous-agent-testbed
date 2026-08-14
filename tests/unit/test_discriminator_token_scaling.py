"""`discriminate()`'s `max_tokens` scaling.

Found live: a 20-idea sample run against the real, deployed discriminator hit
`SchemaViolation` ("gemini hit max_output_tokens before completing the JSON
object") at the client's un-scaled 8192-token default. `ScoredIdea` is a
heavier schema than `LeadAssessment` -- 15 fields per idea, including two
~800-char free-text fields -- so the batch-size-aware pattern already
established in `compliance/assessor.py::_scaled_batch_tokens` was missing here
entirely; `discriminate()` never passed `max_tokens` at all before this fix.
"""

from __future__ import annotations

from factory.engines.discovery.score import _scaled_discriminator_tokens, discriminate
from factory.llm.providers.base import Provider
from factory.llm.providers.fake import FakeProvider
from factory.state.schemas import DiscriminatedIdeas, GeneratedIdea, IdeaBatch, ScoredIdea


def test_scaling_never_exceeds_the_16384_ceiling() -> None:
    assert _scaled_discriminator_tokens(25) <= 16_384


def test_scaling_grows_with_batch_size() -> None:
    assert _scaled_discriminator_tokens(1) < _scaled_discriminator_tokens(10)
    assert _scaled_discriminator_tokens(10) < _scaled_discriminator_tokens(20)


def test_a_batch_of_twenty_gets_more_than_the_old_unscaled_default() -> None:
    """The exact scenario that failed live: 20 ideas, the client's own 8192
    default was insufficient."""
    assert _scaled_discriminator_tokens(20) > 8192


def _idea() -> GeneratedIdea:
    return GeneratedIdea(
        problem_statement="Bookkeepers reconcile invoices by hand every single week.",
        target_niche="Bookkeepers at small Dutch accounting firms",
        source_url="https://example.test/thread",
    )


def _scored_idea() -> ScoredIdea:
    return ScoredIdea(
        problem_statement="Bookkeepers reconcile invoices by hand every single week.",
        target_niche="Bookkeepers at small Dutch accounting firms",
        source_url="https://example.test/thread",
        monetization_hypothesis="EUR 49/month per seat.",
        pain_severity=0.8,
        willingness_to_pay=0.7,
        build_feasibility=0.6,
        adoption_ease=0.8,
    )


async def _ctx_with(provider: FakeProvider):  # type: ignore[no-untyped-def]
    from pathlib import Path

    from factory.hitl.telegram import NullNotifier
    from factory.llm.client import LLMClient
    from factory.llm.router import ProviderRegistry
    from factory.sandbox.disabled import DisabledSandbox
    from factory.settings import EngineName, Settings
    from factory.supervisor.context import FactoryContext

    settings = Settings(gemini_api_key="test-key", workspace_root=Path("/tmp/unused"))
    llm = LLMClient(ProviderRegistry({Provider.GEMINI: provider}), settings=settings)
    return FactoryContext(
        settings=settings,
        llm=llm,
        notifier=NullNotifier(),
        sandbox=DisabledSandbox(),
        enabled_engines={EngineName.DISCOVERY},
        worker_id="token-scaling-test",
    )


async def test_discriminate_passes_the_scaled_max_tokens_to_the_provider() -> None:
    provider = FakeProvider(name=Provider.GEMINI)
    provider.register(DiscriminatedIdeas, DiscriminatedIdeas(ideas=[_scored_idea()] * 5))
    ctx = await _ctx_with(provider)
    batch = IdeaBatch(ideas=[_idea() for _ in range(5)])

    await discriminate(ctx, batch)

    call = provider.calls[-1]
    assert call["schema"] == "DiscriminatedIdeas"
    assert call["max_tokens"] == _scaled_discriminator_tokens(5)


async def test_discriminate_with_an_empty_batch_never_calls_the_provider() -> None:
    provider = FakeProvider(name=Provider.GEMINI)
    ctx = await _ctx_with(provider)

    result = await discriminate(ctx, IdeaBatch(ideas=[]))

    assert result.ideas == []
    assert provider.calls == []
