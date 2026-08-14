"""Rate limiting and IP hashing for the two unauthenticated public write
endpoints, `/interest/{product_id}` and `/analytics/{product_id}`.

A FastAPI dependency, not middleware -- middleware applies to every route
including `/webhooks/stripe` and `/webhooks/mailgun`, which are signature-
verified, trusted callers that must never be throttled by the same mechanism
built for anonymous traffic.

Stored in Postgres (`RateLimitBucket`), not in-process memory: this factory can
run more than one API worker, and an in-memory counter would let a caller
bypass the limit just by landing on a different one. The upsert mirrors
`repository.record_scrape_failure`'s idiom -- one atomic statement, no
SELECT-then-UPDATE race.
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import HTTPException, Request
from sqlalchemy import case
from sqlalchemy.dialects.postgresql import insert as pg_insert

from factory.settings import Settings, get_settings
from factory.state.db import session_scope
from factory.state.orm import RateLimitBucket


def hash_ip(request: Request, settings: Settings) -> str | None:
    """HMAC-SHA256 of the caller's IP, or `None`.

    Plain `sha256(ip)` is reversible in seconds for an IPv4 address (2**32
    preimages), so this refuses to produce anything at all unless a real
    secret is configured -- `None` here means "don't record an IP", not "use a
    weaker hash". Callers must fail toward collecting less data, never toward a
    raw IP baked into a bucket key instead (see `rate_limit` below).

    Reads the single `X-Forwarded-For` value the Caddyfile now guarantees
    (overwritten, not appended, specifically so a caller can't defeat this by
    sending an arbitrary header). Falls back to the ASGI-reported peer address
    when there is no proxy in front, e.g. under test.
    """
    secret = settings.secret("ABUSE_IP_HASH_SECRET")
    if not secret:
        return None
    forwarded = request.headers.get("x-forwarded-for", "")
    ip = forwarded.split(",")[0].strip() or (request.client.host if request.client else "")
    if not ip:
        return None
    return hmac.new(secret.encode("utf-8"), ip.encode("utf-8"), hashlib.sha256).hexdigest()


async def hit(session: Any, *, bucket_key: str, window_seconds: int) -> int:
    """Record one request against `bucket_key` and return the count *after*
    this one, for the current fixed window.

    A single atomic upsert, not read-then-write: the `CASE` inside `SET` reads
    the *existing* row's `window_start` to decide whether this request starts a
    fresh window (reset to 1) or continues the current one (increment).
    `ON CONFLICT` with no matching row inserts `hits=1` directly.
    """
    now = datetime.now(UTC)
    window_start = now - timedelta(seconds=window_seconds)
    stmt = (
        pg_insert(RateLimitBucket)
        .values(bucket_key=bucket_key, window_start=now, hits=1)
        .on_conflict_do_update(
            index_elements=[RateLimitBucket.bucket_key],
            set_={
                "hits": case(
                    (RateLimitBucket.window_start < window_start, 1),
                    else_=RateLimitBucket.hits + 1,
                ),
                "window_start": case(
                    (RateLimitBucket.window_start < window_start, now),
                    else_=RateLimitBucket.window_start,
                ),
            },
        )
        .returning(RateLimitBucket.hits)
    )
    return int((await session.execute(stmt)).scalar_one())


def rate_limit(
    *, scope: str, per_ip_setting: str | None = None, per_product_setting: str | None = None
) -> Callable[[Request, str | None], Coroutine[Any, Any, None]]:
    """Build a FastAPI dependency enforcing the ceilings named by
    `per_ip_setting`/`per_product_setting` -- `Settings` attribute names
    (e.g. `"interest_rate_limit_per_ip_hour"`), looked up fresh on every
    request via `get_settings()`, not captured once at import time. Baking in
    a plain int here would mean a test's `monkeypatch.setenv(...)` +
    `get_settings.cache_clear()` -- this codebase's established way to change
    a setting mid-suite -- silently had no effect on the limit actually
    enforced.

    `product_id` is read from the route's own path parameter when a
    per-product limit is requested -- FastAPI resolves it the same way it
    resolves any other path-parameter-typed dependency argument.

    The hit is recorded and *committed* before this ever decides to raise: if
    the increment happened inside the same transaction as a raised
    `HTTPException`, `session_scope`'s rollback-on-exception would silently
    undo the very hit that tripped the limit, and repeating the blocked
    request would never accumulate against the bucket at all.
    """

    async def _check(request: Request, product_id: str | None = None) -> None:
        settings = get_settings()
        per_ip_limit: int | None = getattr(settings, per_ip_setting) if per_ip_setting else None
        per_product_limit: int | None = (
            getattr(settings, per_product_setting) if per_product_setting else None
        )
        ip_hits = 0
        product_hits = 0
        async with session_scope(settings) as session:
            if per_ip_limit is not None:
                ip_hash = hash_ip(request, settings)
                if ip_hash is not None:
                    ip_hits = await hit(
                        session, bucket_key=f"{scope}:ip:{ip_hash}", window_seconds=3600
                    )
            if per_product_limit is not None and product_id:
                product_hits = await hit(
                    session, bucket_key=f"{scope}:product:{product_id}", window_seconds=3600
                )
        if per_ip_limit is not None and ip_hits > per_ip_limit:
            raise HTTPException(status_code=429, detail="too many requests")
        if per_product_limit is not None and product_id and product_hits > per_product_limit:
            raise HTTPException(status_code=429, detail="too many requests")

    return _check
