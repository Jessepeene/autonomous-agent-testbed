"""`_cluster_snapshot`/`_idea_snapshot`: the detail behind `discovery_verbose_logging`
(see settings.py), a temporary diagnostic aid added while investigating why
Discovery produced zero surviving ideas in its first days live. Gated behind a
settings flag rather than always-on -- these exist so a real cycle's behavior can
be read from `docker compose logs` directly instead of re-running live
reproduction scripts by hand.
"""

from __future__ import annotations

from factory.engines.discovery.cluster import Cluster
from factory.engines.discovery.graph import _cluster_snapshot, _idea_snapshot
from factory.engines.discovery.sources.base import Signal
from factory.state.schemas import ScoredIdea


def _idea(niche: str, *, rejection_reason: str | None = None) -> ScoredIdea:
    return ScoredIdea(
        problem_statement="Bookkeepers reconcile invoices by hand every week, wasting hours.",
        target_niche=niche,
        source_url="https://a.test/1",
        monetization_hypothesis="Bookkeepers pay EUR 49/month per seat for reconciliation.",
        pain_severity=0.8,
        willingness_to_pay=0.7,
        build_feasibility=0.6,
        adoption_ease=0.8,
        rejection_reason=rejection_reason,
    )


def _signal(title: str, *, source: str, engagement: int = 0) -> Signal:
    return Signal(
        title=title, body="b", url="https://a.test/1", source=source, engagement=engagement
    )


def test_cluster_snapshot_reports_only_the_top_twelve() -> None:
    clusters = [Cluster(signals=[_signal(f"t{i}", source="github")]) for i in range(20)]

    snapshot = _cluster_snapshot(clusters)

    assert len(snapshot) == 12
    assert snapshot[0]["rank"] == 1


def test_cluster_snapshot_reports_size_engagement_and_sources() -> None:
    clusters = [
        Cluster(
            signals=[
                _signal("build timeout", source="github", engagement=12),
                _signal("build timeout too short", source="hackernews", engagement=3),
            ]
        )
    ]

    snapshot = _cluster_snapshot(clusters)

    assert snapshot[0]["size"] == 2
    assert snapshot[0]["engagement"] == 15
    assert snapshot[0]["sources"] == ["github", "hackernews"]
    assert snapshot[0]["title"] == "build timeout"


def test_idea_snapshot_marks_kept_and_rejected_ideas_correctly() -> None:
    kept_idea = _idea("Bookkeepers at 5-20 person Dutch accounting firms")
    rejected_idea = _idea("consumer hobbyists", rejection_reason="single-person annoyance")
    weights = ScoredIdea.default_weights()

    snapshot = _idea_snapshot([kept_idea, rejected_idea], [kept_idea], weights)

    assert snapshot[0]["kept"] is True
    assert snapshot[0]["rejection_reason"] is None
    assert snapshot[1]["kept"] is False
    assert snapshot[1]["rejection_reason"] == "single-person annoyance"


def test_idea_snapshot_on_an_empty_batch_is_an_empty_list() -> None:
    assert _idea_snapshot([], [], ScoredIdea.default_weights()) == []
