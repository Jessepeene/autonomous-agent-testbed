"""Product Hunt via the GraphQL API.

Credential-gated. Used to read what a launched product's audience says is *missing*,
which is a sharper signal than the launch itself.
"""

from __future__ import annotations

import httpx

from factory.engines.discovery.sources.base import RateLimiter, Signal, client
from factory.logging import get_logger
from factory.settings import Settings, get_settings

log = get_logger(__name__)

_ENDPOINT = "https://api.producthunt.com/v2/api/graphql"
_LIMITER = RateLimiter(2.0)

_QUERY = """
query RecentPosts($first: Int!) {
  posts(order: VOTES, first: $first) {
    edges {
      node {
        name
        tagline
        description
        url
        votesCount
        commentsCount
      }
    }
  }
}
"""


class ProductHuntSource:
    name = "producthunt"

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()

    @property
    def available(self) -> bool:
        return bool(self._settings.secret("PRODUCTHUNT_TOKEN"))

    async def fetch(self, *, limit: int = 40) -> list[Signal]:
        if not self.available:
            return []
        token = self._settings.secret("PRODUCTHUNT_TOKEN")
        await _LIMITER.wait()
        async with client() as http:
            try:
                response = await http.post(
                    _ENDPOINT,
                    json={"query": _QUERY, "variables": {"first": min(limit, 50)}},
                    headers={"Authorization": f"Bearer {token}"},
                )
            except httpx.HTTPError as exc:
                log.warning("discovery.producthunt_failed", detail=str(exc)[:200])
                return []
        if response.status_code != 200:
            return []

        edges = response.json().get("data", {}).get("posts", {}).get("edges", [])
        signals = [
            Signal(
                title=str(node.get("name", "")),
                body=f"{node.get('tagline', '')}\n{node.get('description', '')}"[:4000],
                url=str(node.get("url", "")),
                source=self.name,
                engagement=int(node.get("commentsCount") or 0),
            )
            for node in (edge.get("node", {}) for edge in edges)
            if node.get("url")
        ]
        log.info("discovery.fetched", source=self.name, count=len(signals))
        return signals
