"""`GET /metrics` must never be readable without a token.

Found during a risk audit: this endpoint exposes the factory's entire financial and
operational state (spend, balance, reinvestment pool, task counts -- see
`observability/metrics.py::collect`) and was unauthenticated *and* publicly reachable
through Caddy. Anyone who guessed the path could read it.

Two independent layers now guard it. The `Caddyfile` refuses the path outright at the
edge, and this bearer check stands behind it so a proxy misconfiguration or a future
Caddyfile edit cannot silently re-expose the data. These tests cover the second layer;
the first is a proxy config with no Python surface to assert on.

The auth check deliberately runs *before* `session_scope`, so the refusal paths need
no database at all -- which is why this is a unit test rather than an integration one.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest

from factory.settings import get_settings


@pytest.fixture(autouse=True)
def _clear_settings_cache() -> AsyncIterator[None]:
    """`get_settings` is `lru_cache`d, so a token set by one test would leak into the
    next one's view of the environment."""
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def _client() -> httpx.AsyncClient:
    from factory.api.app import app

    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def test_refuses_when_no_token_is_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fails closed. An unset credential means "nobody may read this", never
    "everybody may" -- the opposite default would silently restore the original bug
    on any deployment that forgot the variable."""
    monkeypatch.delenv("METRICS_AUTH_TOKEN", raising=False)

    async with await _client() as client:
        response = await client.get("/metrics")

    assert response.status_code == 503
    assert "METRICS_AUTH_TOKEN" in response.json()["detail"]


async def test_rejects_a_missing_header(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("METRICS_AUTH_TOKEN", "correct-horse-battery-staple")

    async with await _client() as client:
        response = await client.get("/metrics")

    assert response.status_code == 401


async def test_rejects_a_wrong_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("METRICS_AUTH_TOKEN", "correct-horse-battery-staple")

    async with await _client() as client:
        response = await client.get("/metrics", headers={"Authorization": "Bearer wrong-token"})

    assert response.status_code == 401


async def test_rejects_the_right_token_under_the_wrong_scheme(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`Basic <token>` carrying the correct secret is still a refusal -- the scheme is
    part of the contract, not decoration."""
    monkeypatch.setenv("METRICS_AUTH_TOKEN", "correct-horse-battery-staple")

    async with await _client() as client:
        response = await client.get(
            "/metrics", headers={"Authorization": "Basic correct-horse-battery-staple"}
        )

    assert response.status_code == 401


async def test_serves_metrics_with_the_right_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """`metrics.collect` is stubbed so this stays a unit test: what is under test is
    the authorisation decision, not the snapshot's contents. `session_scope` is
    stubbed too, the same way `test_interest_cors.py` does it, so this test needs
    no database at all -- `session_scope()` itself calls `get_engine()`, which
    requires `DATABASE_URL` before `collect` ever runs."""
    monkeypatch.setenv("METRICS_AUTH_TOKEN", "correct-horse-battery-staple")

    from factory.api import app as app_module

    @asynccontextmanager
    async def fake_session_scope(*args: Any, **kwargs: Any) -> AsyncIterator[None]:
        yield None

    class _Snapshot:
        def as_dict(self) -> dict[str, Any]:
            return {"balance_cents": 4242}

    async def _fake_collect(session: Any) -> _Snapshot:
        return _Snapshot()

    monkeypatch.setattr(app_module, "session_scope", fake_session_scope)
    monkeypatch.setattr(app_module.metrics, "collect", _fake_collect)

    async with await _client() as client:
        response = await client.get(
            "/metrics", headers={"Authorization": "Bearer correct-horse-battery-staple"}
        )

    assert response.status_code == 200
    assert response.json() == {"balance_cents": 4242}


async def test_health_and_ready_are_not_behind_the_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression guard: /health is what the deploy pipeline and any uptime monitor
    poll. Putting it behind the same token would break both, and it exposes no
    financial state -- only liveness and which engines are enabled."""
    monkeypatch.delenv("METRICS_AUTH_TOKEN", raising=False)

    async with await _client() as client:
        response = await client.get("/health")

    assert response.status_code == 200
    assert "status" in response.json()
