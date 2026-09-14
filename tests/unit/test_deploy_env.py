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
from factory.engines.development.deploy import _collect_files, _has_fastapi_entrypoint
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


# --- PRODUCT_PRICE_CENTS (Phase 5) ------------------------------------------------


@pytest.mark.parametrize(
    ("price_usd", "expected_cents"),
    [
        (4.99, 499),
        (9.99, 999),
        # The exact case a naive int(price * 100) gets wrong: 19.99 * 100 is
        # 1998.9999999999998 in IEEE-754 double precision, which truncates to
        # 1998, not 1999 -- round() first is what makes this exact.
        (19.99, 1999),
        (29.99, 2999),
        (0.0, 0),
        (100.0, 10000),
    ],
)
def test_price_cents_rounds_rather_than_truncates(price_usd: float, expected_cents: int) -> None:
    from factory.engines.development.deploy import _price_cents

    assert _price_cents(price_usd) == expected_cents


async def test_product_price_cents_is_present_when_a_price_is_given(
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

    await deploy_module.deploy(
        council=_approved_council(),
        product_id="prod-123",
        product_name="Reconciliation Assistant",
        workspace="product-1",
        settings=live,
        dry_run=False,
        self_serve_price_usd=19.99,
    )

    assert captured["env"]["PRODUCT_PRICE_CENTS"] == "1999"


async def test_product_price_cents_is_absent_for_a_validation_page(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`self_serve_price_usd=None` -- exactly what `_verify_and_ship` passes for a
    validation landing page, which has no Stripe wiring to control at all."""
    captured: dict[str, Any] = {}

    async def fake_post(self: httpx.AsyncClient, url: str, **kwargs: Any) -> httpx.Response:
        captured.update(kwargs.get("json") or {})
        return httpx.Response(
            200, json={"url": "example.vercel.app", "readyState": "READY"}, request=_REQUEST
        )

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    live = settings.model_copy(update={"vercel_token": "vercel-test-token"})

    await deploy_module.deploy(
        council=_approved_council(),
        product_id="prod-123",
        product_name="Reconciliation Assistant",
        workspace="product-1",
        settings=live,
        dry_run=False,
        self_serve_price_usd=None,
    )

    assert "PRODUCT_PRICE_CENTS" not in captured["env"]


async def test_a_zero_price_is_still_a_real_price_not_omitted(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """0.0 is a real, if degenerate, price (an idea scored before this field
    existed) -- distinct from None, which means "no Stripe wiring at all"."""
    captured: dict[str, Any] = {}

    async def fake_post(self: httpx.AsyncClient, url: str, **kwargs: Any) -> httpx.Response:
        captured.update(kwargs.get("json") or {})
        return httpx.Response(
            200, json={"url": "example.vercel.app", "readyState": "READY"}, request=_REQUEST
        )

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    live = settings.model_copy(update={"vercel_token": "vercel-test-token"})

    await deploy_module.deploy(
        council=_approved_council(),
        product_id="prod-123",
        product_name="Reconciliation Assistant",
        workspace="product-1",
        settings=live,
        dry_run=False,
        self_serve_price_usd=0.0,
    )

    assert captured["env"]["PRODUCT_PRICE_CENTS"] == "0"


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


# --- FastAPI entrypoint detection ---------------------------------------------
#
# Real bug, found live on the first product this factory ever deployed:
# `deploy()` used to send `projectSettings.framework: None` unconditionally,
# which tells Vercel not to detect a framework at all. Confirmed against the
# real Deployments API: `"functions": null, "builds": []` -- the generated
# FastAPI backend (app/main.py) never actually ran, so every `/api/*` call
# 404'd. See `_has_fastapi_entrypoint`'s own docstring for the full story.


@pytest.mark.parametrize(
    "path",
    ["app.py", "index.py", "server.py", "main.py", "wsgi.py", "asgi.py"],
)
def test_recognizes_every_documented_root_level_entrypoint_name(path: str) -> None:
    assert _has_fastapi_entrypoint([{"file": path, "data": "", "encoding": "base64"}])


@pytest.mark.parametrize("directory", ["src", "app", "api"])
def test_recognizes_main_py_one_level_under_the_documented_directories(directory: str) -> None:
    files = [{"file": f"{directory}/main.py", "data": "", "encoding": "base64"}]
    assert _has_fastapi_entrypoint(files)


def test_a_pure_static_landing_page_has_no_entrypoint() -> None:
    """LANDING_PAGE_SYSTEM builds are explicitly "no backend business logic of
    any kind" -- must not declare framework: "fastapi" for a page with no `app`
    instance, or Vercel's build fails looking for one that was never generated."""
    files = [
        {"file": "public/index.html", "data": "", "encoding": "base64"},
        {"file": "public/privacy.html", "data": "", "encoding": "base64"},
        {"file": "requirements.txt", "data": "", "encoding": "base64"},
    ]
    assert not _has_fastapi_entrypoint(files)


def test_a_nested_main_py_two_levels_deep_does_not_count() -> None:
    """Only a fixed, shallow set of locations counts -- matches Vercel's own
    documented detection rule, not "any main.py anywhere in the tree" (which
    would false-positive on, say, a vendored dependency's own main.py)."""
    files = [{"file": "app/nested/main.py", "data": "", "encoding": "base64"}]
    assert not _has_fastapi_entrypoint(files)


async def test_deploy_declares_fastapi_when_a_real_backend_is_present(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        deploy_module,
        "_collect_files",
        lambda workspace, settings: [{"file": "app/main.py", "data": "", "encoding": "base64"}],
    )
    captured: dict[str, Any] = {}

    async def fake_post(self: httpx.AsyncClient, url: str, **kwargs: Any) -> httpx.Response:
        captured.update(kwargs.get("json") or {})
        return httpx.Response(
            200, json={"url": "example.vercel.app", "readyState": "READY"}, request=_REQUEST
        )

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    live = settings.model_copy(update={"vercel_token": "vercel-test-token"})

    await deploy_module.deploy(
        council=_approved_council(),
        product_id="prod-123",
        product_name="Reconciliation Assistant",
        workspace="product-1",
        settings=live,
        dry_run=False,
    )

    assert captured["projectSettings"]["framework"] == "fastapi"


async def test_deploy_declares_no_framework_for_a_pure_static_build(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Overrides the module's autouse `_fixed_files` fixture (which returns
    # app.py, a real entrypoint name) with a pure-static file list.
    monkeypatch.setattr(
        deploy_module,
        "_collect_files",
        lambda workspace, settings: [
            {"file": "public/index.html", "data": "", "encoding": "base64"}
        ],
    )
    captured: dict[str, Any] = {}

    async def fake_post(self: httpx.AsyncClient, url: str, **kwargs: Any) -> httpx.Response:
        captured.update(kwargs.get("json") or {})
        return httpx.Response(
            200, json={"url": "example.vercel.app", "readyState": "READY"}, request=_REQUEST
        )

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    live = settings.model_copy(update={"vercel_token": "vercel-test-token"})

    await deploy_module.deploy(
        council=_approved_council(),
        product_id="prod-123",
        product_name="Reconciliation Assistant",
        workspace="product-1",
        settings=live,
        dry_run=False,
    )

    assert captured["projectSettings"]["framework"] is None


def test_a_static_page_does_not_ship_python_build_triggers(settings: Settings) -> None:
    """Live root cause of a shippable landing page that could not deploy.

    The page passed every gate, recorded zero requirement gaps, and the release
    judgement said ship. Vercel then failed the build:

        Error: No python entrypoint found. Set "tool.vercel.entrypoint" in
        pyproject.toml or define an entrypoint in one of: app.py, index.py, ...

    It carried a 15-byte `requirements.txt` (`pytest`) for the
    `test_validation_page.py` beside it. Both exist so the build can be tested
    locally; neither has anything to do with what gets served. Vercel's zero-config
    detection saw `requirements.txt`, decided "Python project", and demanded an
    entrypoint a static landing page will never have.
    """
    root = settings.workspace_root / "product-static"
    root.mkdir(parents=True)
    (root / "index.html").write_bytes(b"<html><body>hi</body></html>")
    (root / "privacy.html").write_bytes(b"<html><body>privacy</body></html>")
    (root / "requirements.txt").write_bytes(b"pytest>=8\n")
    (root / "test_validation_page.py").write_bytes(b"def test_ok(): pass\n")

    paths = {entry["file"] for entry in _collect_files("product-static", settings)}

    assert paths == {"index.html", "privacy.html"}, (
        "a deployment with no Python entrypoint is static, and a file no browser can "
        f"request can only mislead Vercel's builder -- got {sorted(paths)}"
    )


def test_a_real_backend_still_ships_its_requirements(settings: Settings) -> None:
    """The exclusion must not fire when there IS a backend.

    `requirements.txt` is exactly how Vercel learns what to install for a real MVP.
    Stripping it there would break every product that actually has a backend, which
    is a far worse failure than the one being fixed.
    """
    root = settings.workspace_root / "product-mvp"
    (root / "app").mkdir(parents=True)
    (root / "app" / "main.py").write_bytes(b"from fastapi import FastAPI\napp = FastAPI()\n")
    (root / "requirements.txt").write_bytes(b"fastapi\n")
    (root / "index.html").write_bytes(b"<html></html>")

    paths = {entry["file"] for entry in _collect_files("product-mvp", settings)}

    assert "requirements.txt" in paths
    assert "app/main.py" in paths


def test_the_static_filter_is_an_allowlist_not_a_list_of_known_offenders(
    settings: Settings,
) -> None:
    """The first version named four files to exclude and fixed exactly those four.

    `requirements.txt`, `pyproject.toml`, `Pipfile`, `setup.py` -- so `poetry.lock`,
    `setup.cfg`, `Dockerfile` and `.python-version` would each have been a separate
    outage with an identical cause and an identical one-line fix. Stated as "a
    static deployment serves what a browser can request", they are all handled at
    once, including whatever Vercel's detection learns to recognise next.
    """
    root = settings.workspace_root / "product-allowlist"
    root.mkdir(parents=True)
    (root / "index.html").write_bytes(b"<html></html>")
    (root / "styles.css").write_bytes(b"body{}")
    (root / "logo.svg").write_bytes(b"<svg/>")
    (root / "robots.txt").write_bytes(b"User-agent: *\n")
    # None of these were in the original denylist.
    for offender in ("poetry.lock", "setup.cfg", "Dockerfile", ".python-version", "tox.ini"):
        (root / offender).write_bytes(b"x")

    paths = {entry["file"] for entry in _collect_files("product-allowlist", settings)}

    assert paths == {"index.html", "styles.css", "logo.svg", "robots.txt"}, (
        f"only browser-requestable files may ship; got {sorted(paths)}"
    )


def test_the_deploy_digest_is_a_fact_about_the_bundle(settings: Settings) -> None:
    """"Is this workspace shipped?" has to be answerable, not remembered.

    `workspace_undeployed` (a boolean set by one code path and cleared by another)
    was the first answer and it drifted immediately: every edit made before the
    column existed was invisible to it, so a workspace genuinely holding an
    unshipped fix reported `false` and five repair attempts could not close the gap.

    A digest computed from the files cannot drift, because both sides are computed
    from reality at the moment the question is asked.

    Calls `_collect_files` directly rather than `workspace_digest`, because this
    module's autouse fixture stubs the collector to a fixed payload -- which would
    make every digest identical and the assertion vacuous.
    """
    from factory.engines.development.deploy import content_digest

    root = settings.workspace_root / "product-digest"
    root.mkdir(parents=True)
    (root / "index.html").write_bytes(b"<html>original</html>")

    before = content_digest(_collect_files("product-digest", settings))
    assert before == content_digest(_collect_files("product-digest", settings)), "must be stable"

    (root / "index.html").write_bytes(b"<html>repaired</html>")
    after = content_digest(_collect_files("product-digest", settings))

    assert after != before, (
        "a changed workspace must produce a different digest, or a repair that "
        "never shipped is indistinguishable from one that did"
    )


def test_the_digest_does_not_depend_on_file_order(settings: Settings) -> None:
    """Two bundles with the same content are the same deployment."""
    from factory.engines.development.deploy import content_digest

    a = [
        {"file": "index.html", "data": "aGk=", "encoding": "base64"},
        {"file": "styles.css", "data": "Ym9keQ==", "encoding": "base64"},
    ]
    assert content_digest(a) == content_digest(list(reversed(a)))


def test_two_different_bundles_cannot_collide_on_a_delimiter(settings: Settings) -> None:
    """Length-prefixed, not delimiter-separated.

    A separator that can occur inside the data lets two different bundles hash to
    one value -- and base64 payloads are long enough that picking a "safe" byte is
    a guess rather than a guarantee.
    """
    from factory.engines.development.deploy import content_digest

    left = [{"file": "a", "data": "bc", "encoding": "base64"}]
    right = [{"file": "ab", "data": "c", "encoding": "base64"}]

    assert content_digest(left) != content_digest(right)
