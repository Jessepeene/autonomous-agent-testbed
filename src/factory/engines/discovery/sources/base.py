"""Market-signal sources.

A signal is one piece of public evidence that someone has a problem. Sources are
read-only, rate-limited, and identify themselves honestly in the User-Agent — the
factory's first contact with the internet should not be an ToS violation.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Protocol

import httpx

from factory.integrations.outbound import TIMEOUT, USER_AGENT
from factory.integrations.outbound import direct_client as _direct_client

__all__ = [
    "COMPLAINT_QUERIES",
    "TIMEOUT",
    "USER_AGENT",
    "RateLimiter",
    "Signal",
    "Source",
    "client",
]

#: Phrases that reliably precede an unmet need in developer and operator writing.
COMPLAINT_QUERIES: tuple[str, ...] = (
    "spent hours manually",
    "there is no good tool for",
    "we still do this in a spreadsheet",
    "wish there was a way to automate",
    "our workflow is completely manual",
    "had to write a script because",
)


@dataclass(frozen=True, slots=True)
class Signal:
    """One public complaint or unmet need."""

    title: str
    body: str
    url: str
    source: str
    engagement: int = 0

    @property
    def text(self) -> str:
        return f"{self.title}\n{self.body}".strip()


class Source(Protocol):
    name: str

    @property
    def available(self) -> bool: ...

    async def fetch(self, *, limit: int = 40) -> list[Signal]: ...


class RateLimiter:
    """Minimum spacing between requests to one host."""

    def __init__(self, min_interval_seconds: float) -> None:
        self._interval = min_interval_seconds
        self._lock = asyncio.Lock()
        self._last = 0.0

    async def wait(self) -> None:
        async with self._lock:
            loop = asyncio.get_running_loop()
            elapsed = loop.time() - self._last
            if elapsed < self._interval:
                await asyncio.sleep(self._interval - elapsed)
            self._last = loop.time()


def client() -> httpx.AsyncClient:
    """Thin delegating alias -- `integrations/outbound.py` now owns egress
    policy (the plain client, and the datacenter-proxy fallback
    `website.py` uses); this keeps every one of this function's existing
    callers unchanged."""
    return _direct_client()
