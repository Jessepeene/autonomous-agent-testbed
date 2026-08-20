"""`cluster_signals`'s singleton-survival floor.

Regression coverage for the calibration fix in `cluster.py`: the floor was a
flat 25, implicitly assuming a comment-count-like scale. HackerNews's real
per-comment engagement (reply count, see `sources/hackernews.py`) rarely
reaches double digits -- confirmed live, a 120-comment real sample topped
out at 15 -- so the old floor made an HN singleton's survival structurally
impossible even after its `engagement` field itself was fixed to report real
numbers. `_SINGLETON_ENGAGEMENT_FLOOR` is calibrated against that real
sample instead of an uncalibrated guess.
"""

from __future__ import annotations

from factory.engines.discovery.cluster import _SINGLETON_ENGAGEMENT_FLOOR, cluster_signals
from factory.engines.discovery.sources.base import Signal


def _signal(text: str, *, engagement: int, source: str = "hackernews") -> Signal:
    return Signal(
        title=text, body=text, url="https://example.com", source=source, engagement=engagement
    )


def test_a_singleton_below_the_floor_is_dropped() -> None:
    signals = [_signal("teams struggle tracking renewals across departments", engagement=1)]
    assert cluster_signals(signals) == []


def test_a_singleton_at_the_floor_survives() -> None:
    signals = [
        _signal(
            "teams struggle tracking renewals across departments",
            engagement=_SINGLETON_ENGAGEMENT_FLOOR,
        )
    ]
    kept = cluster_signals(signals)
    assert len(kept) == 1
    assert kept[0].size == 1


def test_a_pair_of_similar_signals_survives_regardless_of_low_engagement() -> None:
    signals = [
        _signal("teams struggle tracking renewals across departments manually", engagement=0),
        _signal("teams struggle tracking renewals across every department manually", engagement=0),
    ]
    kept = cluster_signals(signals)
    assert len(kept) == 1
    assert kept[0].size == 2
