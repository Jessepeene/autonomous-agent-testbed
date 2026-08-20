"""Two real bugs in Discovery's raw signal sources, both found the same way:
live introspection of what the real APIs actually return, not a code read.

HackerNews: `points` is a story-level field in Algolia's index and is `null`
on every `tags=comment` hit -- confirmed live, 100% of real fetches -- so
every HN signal silently scored `engagement=0` and could never survive
`cluster_signals()`'s singleton-keep threshold. Reply count (`children`) is
the one real per-comment engagement signal this endpoint returns.

GitHub: `sort=comments` with no upper bound on the query always surfaces the
same handful of all-time-record megathreads (perennial gaming/compatibility
trackers, ML framework op-coverage issues) -- confirmed live, an entire real
cycle's top-12 clusters were 100% consumer/gaming/OSS threads, zero
resembling a B2B pain point. Capping the range replaced them with issues
like a CI build-timeout limit and a billing-panel integration request.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

import httpx
import pytest

from factory.engines.discovery.sources import github as github_module
from factory.engines.discovery.sources import hackernews as hackernews_module
from factory.engines.discovery.sources.base import Signal
from factory.engines.discovery.sources.github import _QUERIES, GitHubIssuesSource
from factory.engines.discovery.sources.hackernews import HackerNewsSource
from factory.settings import Settings


def _client_for(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.fixture(autouse=True)
def _no_rate_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _no_wait() -> None:
        return None

    monkeypatch.setattr(hackernews_module._LIMITER, "wait", _no_wait)
    monkeypatch.setattr(github_module._LIMITER, "wait", _no_wait)


# --- HackerNews: engagement must come from reply count, never `points` --------


def test_hackernews_engagement_uses_reply_count_not_the_always_null_points_field(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hits = [
        {
            "objectID": "1",
            "comment_text": "x" * 100,
            "story_title": "s",
            "points": None,  # real shape: Algolia never populates this on a comment hit
            "children": [111, 222, 333],
        }
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"hits": hits})

    monkeypatch.setattr(hackernews_module, "client", lambda: _client_for(handler))

    signals = asyncio.run(HackerNewsSource().fetch(limit=6))

    assert signals
    assert all(s.engagement == 3 for s in signals)


def test_hackernews_a_comment_with_no_replies_scores_zero_not_a_crash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hits = [
        {"objectID": "1", "comment_text": "x" * 100, "story_title": "s", "points": None}
    ]  # no "children" key at all -- the real shape for a reply-less comment

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"hits": hits})

    monkeypatch.setattr(hackernews_module, "client", lambda: _client_for(handler))

    signals = asyncio.run(HackerNewsSource().fetch(limit=6))

    assert signals
    assert signals[0].engagement == 0


# --- GitHub: the comment-count query must be bounded above, not just below ----


def test_github_queries_bound_the_comment_count_above_and_below() -> None:
    for query in _QUERIES:
        assert ".." in query, f"query has no upper bound on comments: {query!r}"
        assert ">1000" not in query


async def _fetch_with(monkeypatch: pytest.MonkeyPatch, item: dict[str, object]) -> list[Signal]:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"items": [item]})

    monkeypatch.setattr(github_module, "client", lambda: _client_for(handler))
    return await GitHubIssuesSource(Settings()).fetch(limit=3)


def test_github_source_still_fetches_normally_under_the_new_bounded_queries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    item = {
        "title": "Increase build timeout",
        "body": "x" * 100,
        "html_url": "https://github.com/example/repo/issues/1",
        "comments": 12,
    }
    signals = asyncio.run(_fetch_with(monkeypatch, item))

    assert len(signals) == 3  # one per query, same handler
    assert all(s.engagement == 12 for s in signals)
