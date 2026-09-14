"""A browser on the deployed page must actually be able to submit the form.

This is the test that would have caught the bug nothing else could. Every landing
page is served from a `*.vercel.app` origin and posts to `/interest/{product_id}`
on the factory's own domain, which makes every submission cross-origin. There was
no CORS configuration at all, so:

- the browser's mandatory `OPTIONS` preflight got a bare **405 Method Not Allowed**,
  and the `fetch` was never sent;
- and the `POST` response carried no `Access-Control-Allow-Origin`, so even a
  request that got through could not be read.

Either one alone breaks the form. The result: the form failed for every visitor of
every page this factory has ever deployed, and the interest signal the entire
validation stage is built on could never arrive from a browser.

**Why nothing caught it.** Every reviewer in the pipeline reads source: the gap
check compares files to criteria, Bug Council reviews code categories, the
generated `test_validate.py` asserts against the HTML on disk. `curl` against the
endpoint succeeds, because `curl` sends no `Origin` and needs no preflight. Nothing
ever opened a deployed page in a browser -- the one agent that can (AUDITOR, which
holds the Playwright tools) only ever runs on products in `LIVE`, and a validation
page sits in `VALIDATING`.

So these tests do what no source review can: they send the request a browser sends,
headers and all, and assert on what a browser requires back.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
from fastapi.testclient import TestClient

from factory.api import abuse
from factory.api import app as app_module
from factory.api.app import app

#: A realistic deploy origin. The subdomain is random per product, which is exactly
#: why the allowed origin cannot be an allowlist.
DEPLOY_ORIGIN = "https://shardrunner-jtnvmy9vj-saasagentfactory.vercel.app"

PRODUCT = "06829d037ff64e0a9b4e94f2ed780b02"


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """A client whose handlers reach a real response without a database.

    The handler has to *complete* for this to test anything. Starlette's CORS
    middleware does not decorate an unhandled exception, so a handler that dies on
    a missing database produces a bare 500 with no headers -- which looks exactly
    like the bug being fixed and would make the test pass or fail for the wrong
    reason. Stubbing the session lets the endpoint return its real 404-for-unknown
    -product, which is a handled response and carries the CORS envelope.
    """

    @asynccontextmanager
    async def fake_session_scope(*args: Any, **kwargs: Any) -> AsyncIterator[None]:
        yield None

    async def no_such_product(session: Any, product_id: str) -> None:
        return None

    async def no_hits(session: Any, *, bucket_key: str, window_seconds: int) -> int:
        return 0

    # `abuse` holds its own `session_scope` reference and runs as a dependency,
    # i.e. before the handler body -- patching only the app module leaves the rate
    # limiter reaching for a real database and failing first.
    monkeypatch.setattr(app_module, "session_scope", fake_session_scope)
    monkeypatch.setattr(abuse, "session_scope", fake_session_scope)
    monkeypatch.setattr(abuse, "hit", no_hits)
    monkeypatch.setattr(app_module.repository, "get_product", no_such_product)
    yield TestClient(app, raise_server_exceptions=False)


@pytest.mark.parametrize("path", [f"/interest/{PRODUCT}", f"/analytics/{PRODUCT}"])
def test_the_browser_preflight_is_answered(client: TestClient, path: str) -> None:
    """The `OPTIONS` a browser sends before any cross-origin JSON POST.

    It is not optional and not skippable: `Content-Type: application/json` makes the
    request non-simple, so the browser always asks first. A 405 here means the form
    never fires at all.
    """
    response = client.options(
        path,
        headers={
            "Origin": DEPLOY_ORIGIN,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
    )

    assert response.status_code == 200, (
        f"preflight got {response.status_code}; a browser treats anything but a "
        "successful preflight as a refusal and never sends the POST"
    )
    assert response.headers.get("access-control-allow-origin") == "*"
    assert "POST" in response.headers.get("access-control-allow-methods", "")


def test_the_post_response_is_readable_by_the_page(client: TestClient) -> None:
    """Passing the preflight is not enough -- the response needs the header too.

    Without `Access-Control-Allow-Origin` on the actual response the browser blocks
    the page from reading it, so the form's success path never runs and the visitor
    is shown a failure for a submission the server actually accepted.
    """
    response = client.post(
        f"/interest/{PRODUCT}",
        json={"email": "visitor@example.com"},
        headers={"Origin": DEPLOY_ORIGIN, "Content-Type": "application/json"},
    )

    # 404 here (the stubbed product does not exist) -- what matters is that this is
    # a real, handled response and that the browser is allowed to read it.
    assert response.status_code == 404
    assert response.headers.get("access-control-allow-origin") == "*", (
        "the response must be readable cross-origin, or the page cannot tell a "
        "successful submission from a failed one"
    )


def test_cross_origin_reads_are_not_opened_up(client: TestClient) -> None:
    """The grant stays as narrow as the beacons need.

    `allow_methods` is POST/OPTIONS only, so no GET is ever approved for a
    cross-origin read -- `/dashboard/*`, `/metrics` and `/health` gain nothing from
    this middleware. Pinned because the middleware is app-wide (Starlette has no
    per-route form) and widening it later would be silent.
    """
    response = client.options(
        "/health",
        headers={"Origin": DEPLOY_ORIGIN, "Access-Control-Request-Method": "GET"},
    )

    allowed = response.headers.get("access-control-allow-methods", "")
    assert "GET" not in allowed, f"GET must not be cross-origin readable; got {allowed!r}"
