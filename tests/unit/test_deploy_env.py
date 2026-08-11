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
from factory.engines.development.deploy import _collect_files
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
        return httpx.Response(
            200, json={"url": "example.vercel.app", "readyState": "READY"}, request=_REQUEST
        )

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
        return httpx.Response(
            200, json={"url": "example.vercel.app", "readyState": "READY"}, request=_REQUEST
        )

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
        return httpx.Response(
            200, json={"url": "example.vercel.app", "readyState": "READY"}, request=_REQUEST
        )

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


def test_collect_files_excludes_installed_dependencies(settings: Settings) -> None:
    """.deps is where _verify_and_ship's install step puts dependencies for the
    sandbox test run (development/graph.py). Vercel installs from requirements.txt
    itself at deploy time, so shipping this tree too is both pointless and, for
    anything non-trivial, likely to blow the file-count/size limits of a
    no-linked-repo file-upload deployment."""
    root = settings.workspace_root / "product-1"
    (root / "app").mkdir(parents=True)
    (root / "app" / "main.py").write_bytes(b"print('hi')")
    deps_dir = root / ".deps" / "fastapi"
    deps_dir.mkdir(parents=True)
    (deps_dir / "__init__.py").write_bytes(b"# fastapi")

    files = _collect_files("product-1", settings)

    paths = {entry["file"] for entry in files}
    assert "app/main.py" in paths
    assert not any(path.startswith(".deps") for path in paths)


def test_collect_files_excludes_exactly_what_bug_council_never_reviewed(
    settings: Settings,
) -> None:
    """A secret or malicious code baked into dist/build output must never ship
    without ever having been seen by Bug Council -- deploy's skip set must match
    read_workspace's exactly (both now read from the same WORKSPACE_SKIP_DIRS)."""
    from factory.engines.development.scaffold.generator import (
        WORKSPACE_SKIP_DIRS,
        read_workspace,
    )

    root = settings.workspace_root / "product-1"
    (root / "app").mkdir(parents=True)
    (root / "app" / "main.py").write_bytes(b"print('hi')")
    for skip_dir in ("dist", "build", ".pytest_cache"):
        nested = root / skip_dir / "artifact.js"
        nested.parent.mkdir(parents=True, exist_ok=True)
        nested.write_bytes(b"console.log('built output, never reviewed')")

    deployed_paths = {entry["file"] for entry in _collect_files("product-1", settings)}
    reviewed_paths = set(read_workspace("product-1", settings))

    for skip_dir in ("dist", "build", ".pytest_cache"):
        assert skip_dir in WORKSPACE_SKIP_DIRS
        assert not any(p.startswith(skip_dir) for p in deployed_paths)
        assert not any(p.startswith(skip_dir) for p in reviewed_paths)
    assert "app/main.py" in deployed_paths
    assert "app/main.py" in reviewed_paths


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
