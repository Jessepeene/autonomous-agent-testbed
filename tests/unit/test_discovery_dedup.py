"""`_exclude_already_mined`: real-URL dedup for Discovery's cluster feed.

Real bug, found live: `upsert_idea`'s fingerprint dedup only catches a
byte-for-byte repeat of the *generated prose*, which near-never happens --
the model rephrases the same evidence differently on every call. The same
real GitHub issue won the top-ranked cluster slot 5 discovery cycles running
(nothing about a GitHub issue's comment count changes hour to hour), and the
generator produced 5 differently-worded ideas from the identical evidence,
every one passing the fingerprint check and becoming a separate product.
`source_url` is the one fact the generator must copy verbatim rather than
paraphrase ("Never invent one"), which makes it the one reliable signal for
"have we already mined this" -- checked before ever paying for a generation
call, not after.
"""

from __future__ import annotations

from factory.engines.discovery.cluster import Cluster
from factory.engines.discovery.graph import _exclude_already_mined
from factory.engines.discovery.sources.base import Signal


def _signal(url: str) -> Signal:
    return Signal(title="t", body="b", url=url, source="github")


def test_a_cluster_whose_signal_url_was_already_used_is_dropped() -> None:
    stale = Cluster(signals=[_signal("https://github.com/x/y/issues/427")])
    fresh = Cluster(signals=[_signal("https://github.com/x/y/issues/999")])
    used = {"https://github.com/x/y/issues/427"}

    kept = _exclude_already_mined([stale, fresh], used)

    assert kept == [fresh]


def test_a_cluster_survives_if_none_of_its_signals_were_used() -> None:
    cluster = Cluster(signals=[_signal("https://a.test/1"), _signal("https://a.test/2")])

    assert _exclude_already_mined([cluster], {"https://b.test/unrelated"}) == [cluster]


def test_a_multi_signal_cluster_is_dropped_if_any_one_signal_was_used() -> None:
    """A cluster groups several related posts -- if even one of them already
    became an idea, the whole cluster is substantially the same evidence, not
    a genuinely new discussion that happens to share a neighbour."""
    cluster = Cluster(
        signals=[_signal("https://a.test/new"), _signal("https://a.test/already-used")]
    )

    kept = _exclude_already_mined([cluster], {"https://a.test/already-used"})

    assert kept == []


def test_trailing_slash_differences_still_match() -> None:
    """Matches `_evidence_count_for`'s own normalisation convention -- the caller
    is expected to pre-strip `used_urls`, and this strips the signal's own URL
    the same way before comparing."""
    cluster = Cluster(signals=[_signal("https://a.test/1/")])

    assert _exclude_already_mined([cluster], {"https://a.test/1"}) == []


def test_an_empty_used_set_drops_nothing() -> None:
    cluster = Cluster(signals=[_signal("https://a.test/1")])

    assert _exclude_already_mined([cluster], set()) == [cluster]


def test_an_empty_cluster_list_returns_empty() -> None:
    assert _exclude_already_mined([], {"https://a.test/1"}) == []
