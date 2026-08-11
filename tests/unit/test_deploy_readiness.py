"""Deploy must wait for the build to actually finish before reporting success,
and must tell an infrastructure failure apart from a genuine content rejection.

Before this, `deploy()` returned `ok=True` the moment Vercel *accepted* the
upload (any 200/201 with a `url`) -- `readyState` was never checked, so a product
could go LIVE (and marketing start harvesting leads against it) before the build
had even started, let alone succeeded. Separately, a transport error, missing
token, or 5xx/429/401/403 from Vercel is never the fault of the deployed code and
can never be fixed by regenerating it -- `is_infra=True` lets `graph.py` route
those to a `DevOpsError` instead of a heal cycle.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from factory.engines.development import deploy as deploy_module
from factory.settings import Settings
from factory.state.schemas import CouncilResult

_REQUEST = httpx.Request("POST", "https://api.vercel.com/v13/deployments")


def _approved_council() -> CouncilResult:
    return CouncilResult(approved=True, verdicts=[])


@pytest.fixture(autouse=True)
def _fixed_files(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        deploy_module,
        "_collect_files",
        lambda workspace, settings: [{"file": "app.py", "data": "", "encoding": "base64"}],
    )


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _instant(_seconds: float) -> None:
        return None

    monkeypatch.setattr(deploy_module.asyncio, "sleep", _instant)


def _live(settings: Settings) -> Settings:
    return settings.model_copy(update={"vercel_token": "vercel-test-token"})


async def _deploy(settings: Settings) -> deploy_module.DeployResult:
    return await deploy_module.deploy(
        council=_approved_council(),
        product_id="prod-123",
        product_name="Reconciliation Assistant",
        workspace="product-1",
        settings=settings,
        dry_run=False,
    )


async def test_immediate_ready_state_reports_success_with_no_polling(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    async def fake_post(self: httpx.AsyncClient, url: str, **kwargs: Any) -> httpx.Response:
        calls.append("post")
        return httpx.Response(
            200, json={"url": "example.vercel.app", "readyState": "READY"}, request=_REQUEST
        )

    async def fail_get(self: httpx.AsyncClient, url: str, **kwargs: Any) -> httpx.Response:
        raise AssertionError("must not poll when the initial response is already READY")

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    monkeypatch.setattr(httpx.AsyncClient, "get", fail_get)

    result = await _deploy(_live(settings))

    assert result.ok is True
    assert calls == ["post"]


async def test_polls_until_the_build_becomes_ready(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    states = iter(["BUILDING", "BUILDING", "READY"])

    async def fake_post(self: httpx.AsyncClient, url: str, **kwargs: Any) -> httpx.Response:
        return httpx.Response(
            200,
            json={"url": "example.vercel.app", "readyState": "QUEUED", "id": "dpl_1"},
            request=_REQUEST,
        )

    async def fake_get(self: httpx.AsyncClient, url: str, **kwargs: Any) -> httpx.Response:
        assert url.endswith("/dpl_1")
        return httpx.Response(200, json={"readyState": next(states)}, request=_REQUEST)

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)

    result = await _deploy(_live(settings))

    assert result.ok is True
    assert result.is_infra is False


async def test_error_ready_state_is_a_real_failure_not_infra(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_post(self: httpx.AsyncClient, url: str, **kwargs: Any) -> httpx.Response:
        return httpx.Response(
            200,
            json={"url": "example.vercel.app", "readyState": "BUILDING", "id": "dpl_1"},
            request=_REQUEST,
        )

    async def fake_get(self: httpx.AsyncClient, url: str, **kwargs: Any) -> httpx.Response:
        return httpx.Response(200, json={"readyState": "ERROR"}, request=_REQUEST)

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)

    result = await _deploy(_live(settings))

    assert result.ok is False
    assert result.is_infra is False


async def test_poll_budget_exhausted_is_infra_not_a_code_problem(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_post(self: httpx.AsyncClient, url: str, **kwargs: Any) -> httpx.Response:
        return httpx.Response(
            200,
            json={"url": "example.vercel.app", "readyState": "BUILDING", "id": "dpl_1"},
            request=_REQUEST,
        )

    async def fake_get(self: httpx.AsyncClient, url: str, **kwargs: Any) -> httpx.Response:
        return httpx.Response(200, json={"readyState": "BUILDING"}, request=_REQUEST)

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)

    result = await _deploy(_live(settings))

    assert result.ok is False
    assert result.is_infra is True


async def test_missing_vercel_token_is_infra(settings: Settings) -> None:
    result = await _deploy(settings.model_copy(update={"vercel_token": None}))

    assert result.ok is False
    assert result.is_infra is True


@pytest.mark.parametrize("status", [401, 403, 429, 500, 502, 503, 504])
async def test_infra_shaped_status_codes_are_flagged_infra(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    async def fake_post(self: httpx.AsyncClient, url: str, **kwargs: Any) -> httpx.Response:
        return httpx.Response(status, text="upstream trouble", request=_REQUEST)

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    result = await _deploy(_live(settings))

    assert result.ok is False
    assert result.is_infra is True


async def test_a_content_rejection_status_is_not_infra(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_post(self: httpx.AsyncClient, url: str, **kwargs: Any) -> httpx.Response:
        return httpx.Response(400, text="malformed deployment payload", request=_REQUEST)

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    result = await _deploy(_live(settings))

    assert result.ok is False
    assert result.is_infra is False


async def test_transport_error_is_infra(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    async def fail_post(self: httpx.AsyncClient, url: str, **kwargs: Any) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=_REQUEST)

    monkeypatch.setattr(httpx.AsyncClient, "post", fail_post)

    result = await _deploy(_live(settings))

    assert result.ok is False
    assert result.is_infra is True
