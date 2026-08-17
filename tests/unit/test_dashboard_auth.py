"""`GET /dashboard/state` must never be readable without a token.

Same shape as `test_metrics_auth.py`: the bearer check (`api/auth.py::
require_bearer_token`) runs before `session_scope`, so every refusal path
needs no database at all, which is why this is a unit test rather than an
integration one.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
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
    monkeypatch.delenv("DASHBOARD_AUTH_TOKEN", raising=False)

    async with await _client() as client:
        response = await client.get("/dashboard/state")

    assert response.status_code == 503
    assert "DASHBOARD_AUTH_TOKEN" in response.json()["detail"]


async def test_rejects_a_missing_header(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DASHBOARD_AUTH_TOKEN", "correct-horse-battery-staple")

    async with await _client() as client:
        response = await client.get("/dashboard/state")

    assert response.status_code == 401


async def test_rejects_a_wrong_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DASHBOARD_AUTH_TOKEN", "correct-horse-battery-staple")

    async with await _client() as client:
        response = await client.get(
            "/dashboard/state", headers={"Authorization": "Bearer wrong-token"}
        )

    assert response.status_code == 401


async def test_rejects_the_right_token_under_the_wrong_scheme(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DASHBOARD_AUTH_TOKEN", "correct-horse-battery-staple")

    async with await _client() as client:
        response = await client.get(
            "/dashboard/state", headers={"Authorization": "Basic correct-horse-battery-staple"}
        )

    assert response.status_code == 401


async def test_a_dashboard_token_does_not_also_authorize_metrics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The two endpoints use independent secrets on purpose -- confirms
    `require_bearer_token`'s `setting_name` parameter is actually threaded
    through per-call, not captured once and shared between the two routes."""
    monkeypatch.setenv("DASHBOARD_AUTH_TOKEN", "dashboard-secret")
    monkeypatch.setenv("METRICS_AUTH_TOKEN", "metrics-secret")

    async with await _client() as client:
        cross = await client.get("/metrics", headers={"Authorization": "Bearer dashboard-secret"})

    assert cross.status_code == 401


async def test_serves_state_with_the_right_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """`observability.dashboard.collect` is stubbed so this stays a unit test:
    what is under test is the authorisation decision, not the snapshot's
    contents."""
    monkeypatch.setenv("DASHBOARD_AUTH_TOKEN", "correct-horse-battery-staple")

    from factory.api import dashboard as dashboard_module

    class _Snapshot:
        def as_dict(self) -> dict[str, Any]:
            return {"balance_cents": 4242}

    async def _fake_collect(session: Any, *, settings: Any, report: Any) -> _Snapshot:
        return _Snapshot()

    monkeypatch.setattr(dashboard_module.dashboard_metrics, "collect", _fake_collect)

    async with await _client() as client:
        response = await client.get(
            "/dashboard/state", headers={"Authorization": "Bearer correct-horse-battery-staple"}
        )

    assert response.status_code == 200
    assert response.json() == {"balance_cents": 4242}
