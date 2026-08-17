"""Egress policy: which IP a request to a third-party site leaves from.

This exists because a real, diagnosed problem -- `marketing/providers/website.py`'s
page fetches routinely `ConnectTimeout`/`ReadTimeout`/403 against real sites,
confirmed live to be specifically these targets resisting a request from this
server's own datacenter IP, not a general networking problem -- has a real fix and
a fix this factory refuses to build.

**The real fix, refused on purpose**: a residential/rotating proxy would make target
sites see an ordinary consumer address instead of a datacenter one, which would
straightforwardly work. `compliance/assessor.py::IDEA_SYSTEM_PROMPT` -- permanently
immutable, safety-critical -- rejects any idea that would require "circumventing
authentication or rate limits". Read fairly, a rotating residential proxy whose
purpose is evading a deliberate datacenter-IP block *is* that. This factory does not
get to do to a third party what it refuses to let a generated product do to anyone
else.

**What this module actually provides instead**: a second, honestly-identified
datacenter-class egress point -- a different IP, not a disguise. This is a plausible
partial mitigation for simple IP-reputation blocking, not a fix for bot-defense
systems (Akamai/Cloudflare-style) that key off datacenter ASN ranges as a class
rather than one specific address -- stated honestly, not oversold.

Three hard constraints, all enforced by callers, not by this module alone:
- Datacenter-class proxy only. Never a residential/rotating proxy provider.
- Never used to spoof identity -- `USER_AGENT` is unchanged on the proxy path.
- Never used to evade a rate limit -- a 429 must never trigger a proxy retry
  anywhere in this codebase; see `website.py`'s own retry-trigger set.

The canonical owner of `USER_AGENT`/`TIMEOUT`/the plain client constructor --
`sources/base.py::client()` delegates to `direct_client()` below so every one of
its existing callers (the fixed HN/GitHub/Reddit/Product Hunt sources) keeps
behaving exactly as before, with zero call-site changes.
"""

from __future__ import annotations

import httpx

from factory.settings import Settings

USER_AGENT = "superagent-factory/0.1 (+market research; contact via source site)"
TIMEOUT = httpx.Timeout(20.0, connect=5.0)


def direct_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=TIMEOUT, headers={"User-Agent": USER_AGENT})


def proxy_client(settings: Settings) -> httpx.AsyncClient | None:
    """``None`` when no proxy is configured -- callers must handle this
    gracefully, matching every other optional-credential pattern in this
    codebase (a missing proxy degrades to "direct attempt only", never an
    error). Same `USER_AGENT`, same timeout -- the only thing that changes on
    this path is which IP the request leaves from.
    """
    proxy_url = settings.secret("SCRAPE_PROXY_URL")
    if not proxy_url:
        return None
    return httpx.AsyncClient(timeout=TIMEOUT, headers={"User-Agent": USER_AGENT}, proxy=proxy_url)
