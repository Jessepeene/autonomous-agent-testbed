"""GitHub issues as a market signal.

Open issues with many comments and no maintainer response are unmet needs with a
demonstrated audience. Works unauthenticated at a low rate limit; a token raises it.
"""

from __future__ import annotations

import httpx

from factory.engines.discovery.sources.base import RateLimiter, Signal, client
from factory.logging import get_logger
from factory.settings import Settings, get_settings

log = get_logger(__name__)

_ENDPOINT = "https://api.github.com/search/issues"
_LIMITER = RateLimiter(2.5)

_QUERIES: tuple[str, ...] = (
    'is:issue is:open label:"feature request" comments:>10',
    'is:issue is:open "would be great if" comments:>5',
    'is:issue is:open "any workaround" comments:>5',
)


class GitHubIssuesSource:
    name = "github"

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()

    @property
    def available(self) -> bool:
        return True  # unauthenticated search works, just more slowly

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/vnd.github+json"}
        token = self._settings.secret("GITHUB_TOKEN")
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    async def fetch(self, *, limit: int = 40) -> list[Signal]:
        per_query = max(1, limit // len(_QUERIES))
        signals: list[Signal] = []
        async with client() as http:
            for query in _QUERIES:
                await _LIMITER.wait()
                try:
                    response = await http.get(
                        _ENDPOINT,
                        params={"q": query, "sort": "comments", "per_page": str(per_query)},
                        headers=self._headers(),
                    )
                except httpx.HTTPError as exc:
                    log.warning("discovery.github_failed", detail=str(exc)[:200])
                    continue
                if response.status_code == 403:
                    log.warning("discovery.github_rate_limited")
                    break
                if response.status_code != 200:
                    continue
                for item in response.json().get("items", []):
                    body = (item.get("body") or "").strip()
                    if len(body) < 80:
                        continue
                    signals.append(
                        Signal(
                            title=item.get("title", ""),
                            body=body[:4000],
                            url=item.get("html_url", ""),
                            source=self.name,
                            engagement=int(item.get("comments") or 0),
                        )
                    )
        log.info("discovery.fetched", source=self.name, count=len(signals))
        return signals
