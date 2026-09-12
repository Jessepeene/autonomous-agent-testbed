"""Tier 2 finding #7, system-wide verification plan: the discriminator used
to see only the generator's own paraphrase (problem_statement/target_niche),
never the raw signal evidence it's supposedly independently grading --
defeating this module's own "opposing incentives" design (see score.py's
module docstring). `discriminate()` now includes each idea's originating
cluster's raw text in its own prompt when a `clusters` mapping is given.
"""

from __future__ import annotations

from pathlib import Path

from factory.engines.discovery.cluster import Cluster
from factory.engines.discovery.score import discriminate
from factory.engines.discovery.sources.base import Signal
from factory.hitl.telegram import NullNotifier
from factory.llm.client import LLMClient
from factory.llm.providers.base import Provider
from factory.llm.providers.fake import FakeProvider
from factory.llm.router import ProviderRegistry
from factory.sandbox.disabled import DisabledSandbox
from factory.settings import EngineName, Settings
from factory.state.schemas import DiscriminatedIdeas, GeneratedIdea, IdeaBatch, ScoredIdea
from factory.supervisor.context import FactoryContext

_REAL_SIGNAL_URL = "https://real-source.test/thread/1"
_DISTINCTIVE_RAW_QUOTE = "I literally cried reconciling this spreadsheet at 2am on a Sunday."


def _ctx(provider: FakeProvider) -> FactoryContext:
    settings = Settings(gemini_api_key="test-key", workspace_root=Path("/tmp/unused"))
    llm = LLMClient(ProviderRegistry({Provider.GEMINI: provider}), settings=settings)
    return FactoryContext(
        settings=settings,
        llm=llm,
        notifier=NullNotifier(),
        sandbox=DisabledSandbox(),
        enabled_engines={EngineName.DISCOVERY},
        worker_id="discriminator-grounding-test",
    )


def _idea(source_url: str = _REAL_SIGNAL_URL) -> GeneratedIdea:
    return GeneratedIdea(
        problem_statement="Bookkeepers reconcile invoices by hand every single week.",
        target_niche="Bookkeepers at small Dutch accounting firms",
        source_url=source_url,
    )


def _scored_idea(source_url: str = _REAL_SIGNAL_URL) -> ScoredIdea:
    return ScoredIdea(
        problem_statement="Bookkeepers reconcile invoices by hand every single week.",
        target_niche="Bookkeepers at small Dutch accounting firms",
        source_url=source_url,
        monetization_hypothesis="EUR 49/month per seat.",
        pain_severity=0.8,
        willingness_to_pay=0.7,
        build_feasibility=0.6,
        adoption_ease=0.8,
    )


def _cluster() -> Cluster:
    return Cluster(
        signals=[
            Signal(
                title="Nobody has a good tool for this",
                body=_DISTINCTIVE_RAW_QUOTE,
                url=_REAL_SIGNAL_URL,
                source="hackernews",
                engagement=30,
            )
        ]
    )


async def test_the_raw_cluster_evidence_reaches_the_prompt_when_clusters_are_given() -> None:
    provider = FakeProvider(name=Provider.GEMINI)
    provider.register(DiscriminatedIdeas, DiscriminatedIdeas(ideas=[_scored_idea()]))
    ctx = _ctx(provider)
    batch = IdeaBatch(ideas=[_idea()])

    await discriminate(ctx, batch, clusters=[_cluster()])

    sent_prompt = provider.calls[-1]["prompt"]
    assert _DISTINCTIVE_RAW_QUOTE in sent_prompt


async def test_no_clusters_argument_preserves_the_original_ungrounded_behaviour() -> None:
    """Backward compatible: a caller that predates this (or has no cluster
    mapping handy) must not be forced to supply one."""
    provider = FakeProvider(name=Provider.GEMINI)
    provider.register(DiscriminatedIdeas, DiscriminatedIdeas(ideas=[_scored_idea()]))
    ctx = _ctx(provider)
    batch = IdeaBatch(ideas=[_idea()])

    await discriminate(ctx, batch)

    sent_prompt = provider.calls[-1]["prompt"]
    assert _DISTINCTIVE_RAW_QUOTE not in sent_prompt


async def test_an_idea_whose_source_url_matches_no_cluster_gets_no_evidence_block() -> None:
    """A fabricated or altered source_url must not crash the prompt-building
    step -- it just means no evidence block for that one idea, the same
    fail-soft behaviour `_evidence_count_for` already has for the identical
    situation."""
    provider = FakeProvider(name=Provider.GEMINI)
    provider.register(DiscriminatedIdeas, DiscriminatedIdeas(ideas=[_scored_idea()]))
    ctx = _ctx(provider)
    unmatched_url = "https://never-actually-a-real-signal.test/invented"
    batch = IdeaBatch(ideas=[_idea(unmatched_url)])

    await discriminate(ctx, batch, clusters=[_cluster()])

    sent_prompt = provider.calls[-1]["prompt"]
    assert _DISTINCTIVE_RAW_QUOTE not in sent_prompt
    assert unmatched_url in sent_prompt
