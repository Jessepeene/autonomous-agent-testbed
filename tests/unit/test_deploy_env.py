"""Deploy-time environment injection.

Before this, `deploy()` sent no `env` at all — a generated product could not read
`STRIPE_API_KEY`, let alone a product identifier. Without `PRODUCT_ID` reaching the
running product, no Stripe event it creates can ever be attributed back to a specific
product row (see `finance/webhooks.py` and `engines/marketing/attribution.py`).
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


async def test_product_id_is_always_present_in_the_deploy_env(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, Any] = {}

    async def fake_post(self: httpx.AsyncClient, url: str, **kwargs: Any) -> httpx.Response:
        captured.update(kwargs.get("json") or {})
        return httpx.Response(200, json={"url": "example.vercel.app"}, request=_REQUEST)

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    live = settings.model_copy(update={"vercel_token": "vercel-test-token"})

    result = await deploy_module.deploy(
        council=_approved_council(),
        product_id="prod-123",
        product_name="Reconciliation Assistant",
        workspace="product-1",
        settings=live,
        dry_run=False,
    )

    assert result.ok is True
    assert captured["env"]["PRODUCT_ID"] == "prod-123"


async def test_stripe_key_is_included_only_when_configured(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, Any] = {}

    async def fake_post(self: httpx.AsyncClient, url: str, **kwargs: Any) -> httpx.Response:
        captured.update(kwargs.get("json") or {})
        return httpx.Response(200, json={"url": "example.vercel.app"}, request=_REQUEST)

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    live = settings.model_copy(
        update={"vercel_token": "vercel-test-token", "stripe_api_key": "sk_test_x"}
    )

    await deploy_module.deploy(
        council=_approved_council(),
        product_id="prod-123",
        product_name="Reconciliation Assistant",
        workspace="product-1",
        settings=live,
        dry_run=False,
    )

    assert captured["env"]["STRIPE_API_KEY"] == "sk_test_x"


async def test_stripe_key_is_omitted_not_sent_empty_when_unconfigured(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, Any] = {}

    async def fake_post(self: httpx.AsyncClient, url: str, **kwargs: Any) -> httpx.Response:
        captured.update(kwargs.get("json") or {})
        return httpx.Response(200, json={"url": "example.vercel.app"}, request=_REQUEST)

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    live = settings.model_copy(update={"vercel_token": "vercel-test-token", "stripe_api_key": None})

    await deploy_module.deploy(
        council=_approved_council(),
        product_id="prod-123",
        product_name="Reconciliation Assistant",
        workspace="product-1",
        settings=live,
        dry_run=False,
    )

    assert "STRIPE_API_KEY" not in captured["env"]
    assert captured["env"]["PRODUCT_ID"] == "prod-123"


async def test_dry_run_never_calls_the_deploy_api_at_all(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fail_post(self: httpx.AsyncClient, url: str, **kwargs: Any) -> httpx.Response:
        raise AssertionError("dry_run must never reach the network")

    monkeypatch.setattr(httpx.AsyncClient, "post", fail_post)

    result = await deploy_module.deploy(
        council=_approved_council(),
        product_id="prod-123",
        product_name="Reconciliation Assistant",
        workspace="product-1",
        settings=settings,
        dry_run=True,
    )

    assert result.ok is True
