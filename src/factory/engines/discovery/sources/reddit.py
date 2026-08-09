"""Reddit via the official OAuth API.

Credential-gated by design. The unauthenticated JSON endpoints exist but using them
at scale breaks Reddit's terms, and a factory that quietly violates a platform's ToS
is one complaint away from losing the source entirely.
"""

from __future__ import annotations

import httpx

from factory.engines.discovery.sources.base import (
    COMPLAINT_QUERIES,
    USER_AGENT,
    RateLimiter,
    Signal,
    client,
)
from factory.logging import get_logger
from factory.settings import Settings, get_settings

log = get_logger(__name__)

_TOKEN_URL = "https://www.reddit.com/api/v1/access_token"  # noqa: S105 - endpoint, not a secret
_SEARCH_URL = "https://oauth.reddit.com/search"
_LIMITER = RateLimiter(1.5)

_SUBREDDITS = ("smallbusiness", "sysadmin", "devops", "SaaS", "msp", "accounting")


class RedditSource:
    name = "reddit"

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._token: str | None = None

    @property
    def available(self) -> bool:
        return bool(
            self._settings.secret("REDDIT_CLIENT_ID")
            and self._settings.secret("REDDIT_CLIENT_SECRET")
        )

    async def _authenticate(self, http: httpx.AsyncClient) -> str | None:
        if self._token:
            return self._token
        client_id = self._settings.secret("REDDIT_CLIENT_ID")
        client_secret = self._settings.secret("REDDIT_CLIENT_SECRET")
        if not client_id or not client_secret:
            return None
        try:
            response = await http.post(
                _TOKEN_URL,
                data={"grant_type": "client_credentials"},
                auth=(client_id, client_secret),
                headers={"User-Agent": USER_AGENT},
            )
        except httpx.HTTPError as exc:
            log.warning("discovery.reddit_auth_failed", detail=str(exc)[:200])
            return None
        if response.status_code != 200:
            log.warning("discovery.reddit_auth_rejected", status=response.status_code)
            return None
        self._token = str(response.json().get("access_token") or "") or None
        return self._token

    async def fetch(self, *, limit: int = 40) -> list[Signal]:
        if not self.available:
            return []
        signals: list[Signal] = []
        async with client() as http:
            token = await self._authenticate(http)
            if token is None:
                return []
            headers = {"Authorization": f"Bearer {token}", "User-Agent": USER_AGENT}
            per_query = max(1, limit // len(COMPLAINT_QUERIES))
            for query in COMPLAINT_QUERIES:
                await _LIMITER.wait()
                try:
                    response = await http.get(
                        _SEARCH_URL,
                        params={
                            "q": f"{query} ({' OR '.join(f'subreddit:{s}' for s in _SUBREDDITS)})",
                            "limit": str(per_query),
                            "sort": "relevance",
                            "t": "year",
                        },
                        headers=headers,
                    )
                except httpx.HTTPError as exc:
                    log.warning("discovery.reddit_failed", detail=str(exc)[:200])
                    continue
                if response.status_code != 200:
                    continue
                for child in response.json().get("data", {}).get("children", []):
                    data = child.get("data", {})
                    body = (data.get("selftext") or "").strip()
                    if len(body) < 80:
                        continue
                    signals.append(
                        Signal(
                            title=data.get("title", ""),
                            body=body[:4000],
                            url=f"https://www.reddit.com{data.get('permalink', '')}",
                            source=self.name,
                            engagement=int(data.get("num_comments") or 0),
                        )
                    )
        log.info("discovery.fetched", source=self.name, count=len(signals))
        return signals
