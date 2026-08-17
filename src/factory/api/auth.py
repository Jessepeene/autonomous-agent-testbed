"""Shared bearer-token authentication for read-only observation endpoints
(``GET /metrics``, ``GET /dashboard/state``) -- see ``api/abuse.py`` for the
equivalent factory-dependency pattern this mirrors, just for rate limiting
instead of authentication.
"""

from __future__ import annotations

import hmac
from collections.abc import Callable, Coroutine
from typing import Any

from fastapi import Header, HTTPException

from factory.settings import get_settings


def require_bearer_token(setting_name: str) -> Callable[..., Coroutine[Any, Any, None]]:
    """Build a FastAPI dependency requiring ``Authorization: Bearer <token>`` to
    match the configured ``setting_name`` (a ``Settings`` secret attribute, e.g.
    ``"metrics_auth_token"``), looked up fresh on every request via
    ``get_settings()`` -- same reasoning as ``abuse.rate_limit``'s own dependency
    factory: a test's ``monkeypatch.setenv`` + ``get_settings.cache_clear()``
    must actually change what's enforced, not just what a captured value says.

    Fails closed when unset: an unset credential means "nobody may read this",
    never "everybody may".
    """

    async def _check(authorization: str = Header(default="", alias="Authorization")) -> None:
        expected = get_settings().secret(setting_name)
        if not expected:
            raise HTTPException(status_code=503, detail=f"{setting_name.upper()} is not configured")
        scheme, _, presented = authorization.partition(" ")
        # compare_digest on both halves: a plain `!=` leaks length/prefix through
        # timing, and this is reachable from the container network even with
        # Caddy refusing the path at the edge.
        if scheme.lower() != "bearer" or not hmac.compare_digest(presented.strip(), expected):
            raise HTTPException(status_code=401, detail="invalid or missing bearer token")

    return _check
