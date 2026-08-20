"""Lexical clustering of market signals.

Deliberately not embeddings. Clustering runs on every discovery cycle over hundreds
of signals, and an embedding call per signal would cost real money to answer a
question — "are these two complaints about the same thing?" — that Jaccard overlap on
content words answers well enough to feed the next stage.

It is also deterministic, so the same signals always produce the same clusters, which
makes discovery reproducible in tests.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from factory.engines.discovery.sources.base import Signal

_TOKEN = re.compile(r"[a-z][a-z0-9+.-]{2,}")
#: Adds a bare run of digits (any length, including a single digit) as its own
#: token -- used for memory dedup (see `tokenize`'s `include_numeric` parameter),
#: where a fact's number is frequently the entire point ("retry after 30 seconds"
#: vs "300 seconds", "cap raised to 40 percent" vs "60 percent",
#: "max_concurrent_builds is 2" vs "5"). The original letter-starting `_TOKEN` alone
#: never produces a purely-numeric token, so two facts differing only in a number
#: hashed to identical token sets and were treated as duplicates -- the more recent
#: one merged into the older, less-confident row instead of being stored, and the
#: stale number could end up with *higher* confidence than a correction would have.
#: Discovery clustering deliberately keeps the letter-only default: a bare number
#: scattered across unrelated public complaints is usually noise (a page count, a
#: year), not a real similarity signal, and that tokenizer's output composition has
#: never been re-tuned against including one.
_TOKEN_WITH_NUMERIC = re.compile(r"[a-z][a-z0-9+.-]{2,}|[0-9]+")

_STOPWORDS: frozenset[str] = frozenset(
    """
    the and for that with this you your are was were have has had not but they them
    then than there here what when where which who whom will would can could should
    about into from just like some more most other than only very via out our its
    it's i'm we've don't doesn't didn't isn't aren't been being does did done get got
    use used using make makes made need needs needed want wants work works working
    thing things something anything really actually basically probably maybe someone
    people time way lot bit new old good bad better best worse able
    """.split()
)

_MIN_TOKENS = 4

# A singleton cluster survives only with real, multi-party engagement -- "one loud
# complaint with forty replies" per this module's own original framing below. In
# practice this was never calibrated against either real source's actual scale:
# GitHub's `engagement` (raw comment count) was, until recently, unbounded and
# routinely reached into the thousands on perennial megathreads, so it cleared any
# floor trivially; HN's was a flat 0 (a since-fixed bug -- `points` is null on every
# comment-type Algolia hit, see `sources/hackernews.py`), so no HN singleton could
# ever clear *any* positive floor. With both sources now reporting real, comparable
# numbers (GitHub bounded to a 6-60 comment range, HN using real reply counts), 25
# is still too high for HN's scale -- confirmed live, a 120-comment real sample
# topped out at 15 replies, with only ~12% reaching even 3. 10 is chosen from that
# same sample: rare enough to preserve real selectivity (only the two loudest of
# 120 real comments cleared it) while remaining reachable, not a floor calibrated
# for a scale nothing produced.
_SINGLETON_ENGAGEMENT_FLOOR = 10


@dataclass(slots=True)
class Cluster:
    signals: list[Signal] = field(default_factory=list)
    keywords: frozenset[str] = frozenset()

    @property
    def size(self) -> int:
        return len(self.signals)

    @property
    def engagement(self) -> int:
        return sum(s.engagement for s in self.signals)

    def summary(self, max_chars: int = 2400) -> str:
        parts = [f"[{s.source}] {s.title}\n{s.body[:600]}\n{s.url}" for s in self.signals[:6]]
        return "\n\n".join(parts)[:max_chars]


def tokenize(text: str, *, include_numeric: bool = False) -> frozenset[str]:
    pattern = _TOKEN_WITH_NUMERIC if include_numeric else _TOKEN
    return frozenset(t for t in pattern.findall(text.lower()) if t not in _STOPWORDS)


def jaccard(left: frozenset[str], right: frozenset[str]) -> float:
    if not left or not right:
        return 0.0
    intersection = len(left & right)
    if intersection == 0:
        return 0.0
    return intersection / len(left | right)


def cluster_signals(
    signals: list[Signal],
    *,
    threshold: float = 0.18,
    min_cluster_size: int = 2,
) -> list[Cluster]:
    """Greedy single-pass clustering, ordered by evidence weight.

    Singleton clusters are kept only when highly engaged (see
    `_SINGLETON_ENGAGEMENT_FLOOR`): one loud complaint is a signal, one quiet
    complaint is noise.
    """
    tokenized = [(signal, tokenize(signal.text)) for signal in signals]
    tokenized = [(s, t) for s, t in tokenized if len(t) >= _MIN_TOKENS]
    tokenized.sort(key=lambda pair: pair[0].engagement, reverse=True)

    clusters: list[Cluster] = []
    for signal, tokens in tokenized:
        best: Cluster | None = None
        best_score = threshold
        for candidate in clusters:
            score = jaccard(tokens, candidate.keywords)
            if score > best_score:
                best, best_score = candidate, score
        if best is None:
            clusters.append(Cluster(signals=[signal], keywords=tokens))
        else:
            best.signals.append(signal)
            best.keywords = best.keywords & tokens or best.keywords

    kept = [
        c
        for c in clusters
        if c.size >= min_cluster_size or c.engagement >= _SINGLETON_ENGAGEMENT_FLOOR
    ]
    kept.sort(key=lambda c: (c.size, c.engagement), reverse=True)
    return kept
