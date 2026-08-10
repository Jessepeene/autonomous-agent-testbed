"""Evidence count: how many raw signals corroborate a surviving idea.

Before this, `evidence_count` was defined end-to-end (schema, ORM column, read
path) but nothing ever wrote a real value -- `upsert_idea`'s INSERT never
mentioned the column at all, so every idea silently got the database default of
0 regardless of whether it came from a cluster of one throwaway post or twenty
corroborating ones. `_evidence_count_for` recovers the count deterministically
from the cluster the idea's `source_url` came from, rather than asking the model
to report it (a fact it could just as easily fabricate).
"""

from __future__ import annotations

from factory.engines.discovery.cluster import Cluster
from factory.engines.discovery.graph import _evidence_count_for
from factory.engines.discovery.sources.base import Signal
from factory.state.schemas import ScoredIdea


def _idea(source_url: str) -> ScoredIdea:
    return ScoredIdea(
        problem_statement="Bookkeepers reconcile invoices by hand every week, wasting hours.",
        target_niche="Bookkeepers at 5-20 person Dutch accounting firms",
        source_url=source_url,
        monetization_hypothesis="Bookkeepers pay EUR 49/month per seat for reconciliation.",
        pain_severity=0.8,
        willingness_to_pay=0.7,
        build_feasibility=0.6,
        adoption_ease=0.8,
    )


def _signal(url: str) -> Signal:
    return Signal(title="t", body="b", url=url, source="hackernews")


def test_matches_the_cluster_whose_signal_url_was_copied() -> None:
    matching = Cluster(signals=[_signal("https://a.test/1"), _signal("https://a.test/2")])
    other = Cluster(signals=[_signal("https://b.test/1")])
    idea = _idea("https://a.test/1")

    assert _evidence_count_for(idea, [other, matching]) == 2


def test_tolerates_a_trailing_slash_difference() -> None:
    """pydantic's HttpUrl normalisation can add a trailing slash the raw signal
    URL never had -- an exact-string match alone would silently miss this."""
    cluster = Cluster(signals=[_signal("https://a.test/1")])
    idea = _idea("https://a.test/1/")

    assert _evidence_count_for(idea, [cluster]) == 1


def test_no_matching_cluster_falls_back_to_zero_without_raising() -> None:
    cluster = Cluster(signals=[_signal("https://a.test/1")])
    idea = _idea("https://nowhere.test/gone")

    assert _evidence_count_for(idea, [cluster]) == 0


def test_empty_cluster_list_falls_back_to_zero() -> None:
    idea = _idea("https://a.test/1")

    assert _evidence_count_for(idea, []) == 0
