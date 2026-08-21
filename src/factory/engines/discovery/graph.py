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
from factory.engines.discovery.scout import scout_signals
from factory.engines.discovery.sources.base import Signal, Source
from factory.engines.discovery.sources.github import GitHubIssuesSource
from factory.engines.discovery.sources.hackernews import HackerNewsSource
from factory.engines.discovery.sources.producthunt import ProductHuntSource
from factory.engines.discovery.sources.reddit import RedditSource
from factory.learning import prompts as learning_prompts
from factory.logging import get_logger
from factory.memory import store as memory_store
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


def _cluster_snapshot(clusters: list[Cluster]) -> list[dict[str, object]]:
    """Diagnostic detail for `discovery_verbose_logging` -- exactly the top-12
    clusters `generate_ideas` actually sees, not the full list `cluster_signals`
    returns."""
    return [
        {
            "rank": rank,
            "size": cluster.size,
            "engagement": cluster.engagement,
            "sources": sorted({signal.source for signal in cluster.signals}),
            "title": cluster.signals[0].title[:80],
        }
        for rank, cluster in enumerate(clusters[:12], start=1)
    ]


def _idea_snapshot(
    ideas: list[ScoredIdea], kept: list[ScoredIdea], weights: dict[str, float]
) -> list[dict[str, object]]:
    """Diagnostic detail for `discovery_verbose_logging` -- every idea the
    discriminator actually scored, kept or not, and why."""
    kept_ids = {id(idea) for idea in kept}
    return [
        {
            "niche": idea.target_niche,
            "score": idea.weighted_score(weights),
            "kept": id(idea) in kept_ids,
            "rejection_reason": idea.rejection_reason,
        }
        for idea in ideas
    ]


def _exclude_already_mined(clusters: list[Cluster], used_urls: set[str]) -> list[Cluster]:
    """Drop any cluster containing a signal whose URL already backs a stored idea.

    `upsert_idea`'s fingerprint dedup only catches a byte-for-byte repeat of the
    *generated prose*, which near-never happens -- the model rephrases the same
    evidence differently on every call. Real bug, found live: the same GitHub
    issue won the top cluster slot 5 cycles running and produced 5 differently
    -worded "new" ideas, all 5 becoming separate products. `source_url` is the
    one fact the generator must copy verbatim rather than paraphrase (never
    invent one), so it is the one reliable "have we already mined this" signal
    available before ever paying for a generation call. `used_urls` is expected
    pre-normalised (trailing slash stripped) by the caller, matching
    `_evidence_count_for`'s own convention below.
    """
    return [
        cluster
        for cluster in clusters
        if not any(signal.url.rstrip("/") in used_urls for signal in cluster.signals)
    ]


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
    fixed_signals = await gather_signals(sources) if sources else []
    scouted = await scout_signals(ctx, session, task)
    signals = fixed_signals + scouted
    if not signals:
        return "discovery: no signals returned this cycle"

    clusters = cluster_signals(signals)
    log.info(
        "discovery.clustered",
        signals=len(signals),
        scouted=len(scouted),
        clusters=len(clusters),
    )
    if ctx.settings.discovery_verbose_logging:
        log.info("discovery.cluster_snapshot", clusters=_cluster_snapshot(clusters))
    if not clusters:
        return f"discovery: {len(signals)} signals produced no clusters"

    used_urls = await repository.used_idea_source_urls(session)
    fresh_clusters = _exclude_already_mined(clusters, used_urls)
    stale_count = len(clusters) - len(fresh_clusters)
    if stale_count:
        log.info("discovery.clusters_already_mined", stale=stale_count, fresh=len(fresh_clusters))
    clusters = fresh_clusters
    if not clusters:
        return f"discovery: {len(signals)} signals produced only already-mined clusters"

    gen_text, gen_version = await learning_prompts.get_prompt(
        session, key=_GENERATOR_KEY, default=score.GENERATOR_SYSTEM
    )
    disc_text, disc_version = await learning_prompts.get_prompt(
        session, key=_DISCRIMINATOR_KEY, default=score.DISCRIMINATOR_SYSTEM
    )
    weights, weights_version = await learning_prompts.get_scoring_weights(
        session, key=_WEIGHTS_KEY, default=ScoredIdea.default_weights()
    )

    # Closing a loop this store's own module docstring names as the motivating
    # example ("the last three products in this niche all failed compliance for
    # the same reason, stop proposing them") but never actually closed until
    # now: what this cycle's own clusters are about is the query, and past
    # validation/compliance outcomes at engine:discovery scope are what comes
    # back. Injected into the *prompt*, not `system` -- see generate_ideas'
    # own comment for why appending to `system` would corrupt trial
    # attribution for a tunable prompt.
    cluster_query = " ".join(cluster.summary()[:200] for cluster in clusters[:12])
    memory_block = await memory_store.recall_block(
        session, query=cluster_query, scopes=memory_store.scopes_for(engine=EngineName.DISCOVERY)
    )

    batch = await score.generate_ideas(
        ctx,
        clusters,
        system=gen_text,
        tunable_parameter_id=gen_version,
        task_id=task.id,
        memory_block=memory_block,
    )
    scored = await score.discriminate(
        ctx,
        batch,
        system=disc_text,
        tunable_parameter_id=disc_version,
        task_id=task.id,
        memory_block=memory_block,
    )
    kept = score.survivors(scored, weights)
    if ctx.settings.discovery_verbose_logging:
        log.info("discovery.idea_snapshot", ideas=_idea_snapshot(scored.ideas, kept, weights))

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

    # One aggregate memory per cycle, not one per rejected idea: remember()
    # reinforces near-duplicates within a scope, so a genuinely recurring
    # pattern (the same handful of niches, the same rejection reasons)
    # strengthens across cycles instead of flooding engine:discovery with
    # near-identical rows -- MAX_MEMORIES_PER_SCOPE stays comfortable.
    rejected = [idea for idea in scored.ideas if id(idea) not in kept_ids]
    if rejected:
        niches = "; ".join(idea.target_niche for idea in rejected[:5])
        reasons = "; ".join(idea.rejection_reason for idea in rejected[:3] if idea.rejection_reason)
        content = (
            f"This cycle rejected {len(rejected)}/{len(scored.ideas)} scored ideas "
            f"(kept {len(kept)}). Niches: {niches}."
        )
        if reasons:
            content += f" Reasons given: {reasons}"
        await memory_store.remember(
            session,
            content=content,
            kind=memory_store.MemoryKind.LESSON,
            scope=memory_store.scope_for(engine=EngineName.DISCOVERY),
            origin="discovery:run",
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
        f"discovery: {len(signals)} signals ({len(scouted)} scouted) → "
        f"{len(clusters)} clusters → {len(batch.ideas)} candidates → "
        f"{len(kept)} survivors → {stored} new"
    )
