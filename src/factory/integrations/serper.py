"""Serper.dev — Google search results over a simple REST API.

Kept dependency-free of the marketing package on purpose: this client backs both
`marketing/providers/website.py` (lead sourcing) and the general-purpose
`web_search` agent tool (`tools/builtin.py`) — a lower-level module than either,
not a marketing-specific one.

Endpoint contract not independently verified against a live account in this
environment (the same caveat this codebase already carries for Mailgun/Vercel) —
confirmed via serper.dev's own published response shape (an ``organic`` array of
``title``/``link``/``snippet``/``position``), consistent with what
`reference files/lead_scout.py` already expects from its own production
``serper.search()`` call.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from factory.engines.discovery.sources.base import RateLimiter
from factory.settings import Settings, get_settings

_ENDPOINT = "https://google.serper.dev/search"
_LIMITER = RateLimiter(1.0)
_TIMEOUT = httpx.Timeout(20.0, connect=5.0)
_USER_AGENT = "superagent-factory/0.1 (+market research; contact via source site)"


class SerperError(Exception):
    """Serper rejected the request. ``status_code`` lets callers classify it the
    same way every other provider in this codebase does (401/403 credential,
    400/422 malformed request) — this module stays generic on purpose so both a
    `LeadProvider` and a plain tool can interpret it their own way."""

    def __init__(self, status_code: int, detail: str) -> None:
        self.status_code = status_code
        self.detail = detail[:300]
        super().__init__(f"serper.dev rejected the request (HTTP {status_code}): {self.detail}")


@dataclass(frozen=True, slots=True)
class SerperResult:
    title: str
    link: str
    snippet: str


def available(settings: Settings | None = None) -> bool:
    settings = settings or get_settings()
    return bool(settings.secret("SERPER_API_KEY"))


async def search(
    query: str, *, num: int = 10, settings: Settings | None = None
) -> list[SerperResult]:
    """Raises `SerperError` on a non-200 response. Returns ``[]`` when no key is
    configured, matching every `LeadProvider.available` convention (an
    unconfigured optional credential is never a hard failure)."""
    settings = settings or get_settings()
    key = settings.secret("SERPER_API_KEY")
    if not key:
        return []

    await _LIMITER.wait()
    async with httpx.AsyncClient(timeout=_TIMEOUT, headers={"User-Agent": _USER_AGENT}) as client:
        response = await client.post(
            _ENDPOINT,
            json={"q": query, "num": max(1, min(num, 20))},
            headers={"X-API-KEY": key, "Content-Type": "application/json"},
        )
    if response.status_code != 200:
        raise SerperError(response.status_code, response.text)

    payload = response.json()
    results: list[SerperResult] = []
    for entry in payload.get("organic", []):
        link = str(entry.get("link") or "").strip()
        if not link:
            continue
        results.append(
            SerperResult(
                title=str(entry.get("title") or "").strip(),
                link=link,
                snippet=str(entry.get("snippet") or "").strip(),
            )
        )
    return results
