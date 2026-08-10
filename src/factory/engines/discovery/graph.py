"""Discovery Engine runner.

fetch signals → cluster → generate → discriminate → persist → queue for compliance.

Every surviving idea is queued for compliance assessment rather than straight to
Development: nothing gets built before someone has established it is lawful to build.
"""

from __future__ import annotations

import asyncio

from sqlalchemy.ext.asyncio import AsyncSession

from factory.engines.discovery import score
from factory.engines.discovery.cluster import Cluster, cluster_signals
from factory.engines.discovery.sources.base import Signal, Source
from factory.engines.discovery.sources.github import GitHubIssuesSource
from factory.engines.discovery.sources.hackernews import HackerNewsSource
from factory.engines.discovery.sources.producthunt import ProductHuntSource
from factory.engines.discovery.sources.reddit import RedditSource
from factory.learning import prompts as learning_prompts
from factory.logging import get_logger
from factory.settings import EngineName, Settings
from factory.state import repository
from factory.state.orm import Task
from factory.state.schemas import ScoredIdea, TaskKind
from factory.supervisor.context import FactoryContext

log = get_logger(__name__)

_SIGNALS_PER_SOURCE = 40
_GENERATOR_KEY = "factory.engines.discovery.score:GENERATOR_SYSTEM"
_DISCRIMINATOR_KEY = "factory.engines.discovery.score:DISCRIMINATOR_SYSTEM"
_WEIGHTS_KEY = "factory.engines.discovery.score:SCORING_WEIGHTS"


def _evidence_count_for(idea: ScoredIdea, clusters: list[Cluster]) -> int:
    """How many raw signals corroborate this idea's cluster.

    The generator is told to copy ``source_url`` verbatim from one of the signals
    in the cluster it drew the idea from ("Never invent one"), so that URL is the
    only link back to a cluster an emitted idea still carries -- there is no
    cluster index or id in the returned schema. Deterministic, not asked of the
    model: the model already has enough to fabricate a plausible-sounding count,
    and this is a fact derivable from data already in hand.

    Falls back to 0 (matching every idea's behaviour before this existed) if the
    URL was altered in transit (whitespace, a trailing slash) rather than raising
    -- one idea's evidence count being wrong is not worth failing the whole cycle.
    """
    target = str(idea.source_url).rstrip("/")
    for cluster in clusters:
        if any(signal.url.rstrip("/") == target for signal in cluster.signals):
            return cluster.size
    return 0


def build_sources(settings: Settings) -> list[Source]:
    """Every source that is currently usable. Unconfigured ones drop out silently."""
    candidates: list[Source] = [
        HackerNewsSource(),
        GitHubIssuesSource(settings),
        RedditSource(settings),
        ProductHuntSource(settings),
    ]
    return [source for source in candidates if source.available]


async def gather_signals(sources: list[Source]) -> list[Signal]:
    """Fetch every source concurrently; one failing source must not lose the others."""
    results = await asyncio.gather(
        *(source.fetch(limit=_SIGNALS_PER_SOURCE) for source in sources),
        return_exceptions=True,
    )
    signals: list[Signal] = []
    for source, result in zip(sources, results, strict=True):
        if isinstance(result, BaseException):
            log.warning("discovery.source_failed", source=source.name, detail=str(result)[:200])
            continue
        signals.extend(result)
    return signals


async def run(ctx: FactoryContext, session: AsyncSession, task: Task) -> str:
    sources = build_sources(ctx.settings)
    if not sources:
        return "discovery: no sources are configured"

    signals = await gather_signals(sources)
    if not signals:
        return "discovery: no signals returned this cycle"

    clusters = cluster_signals(signals)
    log.info("discovery.clustered", signals=len(signals), clusters=len(clusters))
    if not clusters:
        return f"discovery: {len(signals)} signals produced no clusters"

    gen_text, gen_version = await learning_prompts.get_prompt(
        session, key=_GENERATOR_KEY, default=score.GENERATOR_SYSTEM
    )
    disc_text, disc_version = await learning_prompts.get_prompt(
        session, key=_DISCRIMINATOR_KEY, default=score.DISCRIMINATOR_SYSTEM
    )
    weights, weights_version = await learning_prompts.get_scoring_weights(
        session, key=_WEIGHTS_KEY, default=ScoredIdea.default_weights()
    )

    batch = await score.generate_ideas(
        ctx, clusters, system=gen_text, tunable_parameter_id=gen_version, task_id=task.id
    )
    scored = await score.discriminate(
        ctx, batch, system=disc_text, tunable_parameter_id=disc_version, task_id=task.id
    )
    kept = score.survivors(scored, weights)

    # Every discriminated idea is an outcome for all three keys that shaped it — a
    # trial on any one of them (generation, discrimination, or the weights) needs its
    # own complete episode history to be evaluable, independent of which of the three
    # currently happens to be on trial.
    kept_ids = {id(idea) for idea in kept}
    for idea in scored.ideas:
        success = id(idea) in kept_ids
        detail = {"weighted_score": idea.weighted_score(weights)}
        for version_id in (gen_version, disc_version, weights_version):
            await learning_prompts.record_outcome(
                session,
                tunable_parameter_id=version_id,
                success=success,
                detail=detail,
                task_id=task.id,
            )

    stored = 0
    for idea in kept:
        idea_id = await repository.upsert_idea(
            session,
            idea,
            score=idea.weighted_score(weights),
            evidence_count=_evidence_count_for(idea, clusters),
        )
        if idea_id is None:
            continue  # already known — de-duplicated by fingerprint
        stored += 1
        await repository.enqueue_task(
            session,
            kind=TaskKind.ASSESS_COMPLIANCE,
            engine=EngineName.COMPLIANCE,
            idempotency_key=f"compliance:idea:{idea_id}",
            payload={"idea_id": idea_id},
            idea_id=idea_id,
            priority=80,
        )

    return (
        f"discovery: {len(signals)} signals → {len(clusters)} clusters → "
        f"{len(batch.ideas)} candidates → {len(kept)} survivors → {stored} new"
    )
