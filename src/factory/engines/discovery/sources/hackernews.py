"""Hacker News via the public Algolia search API.

Needs no credentials, which makes it the source that keeps Discovery working on a
freshly-provisioned machine with nothing configured.
"""

from __future__ import annotations

import httpx

from factory.engines.discovery.sources.base import (
    COMPLAINT_QUERIES,
    RateLimiter,
    Signal,
    client,
)
from factory.logging import get_logger

log = get_logger(__name__)

_ENDPOINT = "https://hn.algolia.com/api/v1/search"
_LIMITER = RateLimiter(1.0)


class HackerNewsSource:
    name = "hackernews"

    @property
    def available(self) -> bool:
        return True

    async def fetch(self, *, limit: int = 40) -> list[Signal]:
        per_query = max(1, limit // len(COMPLAINT_QUERIES))
        signals: list[Signal] = []
        async with client() as http:
            for query in COMPLAINT_QUERIES:
                await _LIMITER.wait()
                try:
                    response = await http.get(
                        _ENDPOINT,
                        params={
                            "query": query,
                            "tags": "comment",
                            "hitsPerPage": str(per_query),
                        },
                    )
                except httpx.HTTPError as exc:
                    log.warning("discovery.hn_failed", query=query, detail=str(exc)[:200])
                    continue
                if response.status_code != 200:
                    continue
                for hit in response.json().get("hits", []):
                    text = (hit.get("comment_text") or "").strip()
                    if len(text) < 80:
                        continue
                    object_id = hit.get("objectID", "")
                    signals.append(
                        Signal(
                            title=hit.get("story_title") or query,
                            body=text[:4000],
                            url=f"https://news.ycombinator.com/item?id={object_id}",
                            source=self.name,
                            engagement=int(hit.get("points") or 0),
                        )
                    )
        log.info("discovery.fetched", source=self.name, count=len(signals))
        return signals
