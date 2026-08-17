"""Phase 7's proxy seam: `integrations/outbound.py`'s client construction, and
`website.py::_fetch_page_text`'s retry logic built on top of it.

The three user-mandated guardrails, each with its own test below: a domain
already known to need the proxy skips the doomed direct attempt entirely; a
retriable direct failure (timeout/403) gets exactly one retry through the
proxy; and a 429 is never retried through the proxy under any code path --
that would be functionally evading the rate limit itself, exactly what
`IDEA_SYSTEM_PROMPT` already refuses to let a generated product do to
someone else.
"""

from __future__ import annotations

import httpx
import pytest

from factory.engines.marketing.providers import website as website_module
from factory.integrations import outbound
from factory.settings import Settings


def _settings(**overrides: object) -> Settings:
    return Settings(**overrides)  # type: ignore[arg-type]


def _client_for(handler: object) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))  # type: ignore[arg-type]


def _raise_if_called() -> httpx.AsyncClient:
    raise AssertionError("this egress point must not be used on this code path")


@pytest.fixture(autouse=True)
def _no_rate_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """The real `_FETCH_LIMITER` enforces a minimum 1s spacing between requests
    to a host -- correct in production, but every test below deliberately
    issues direct-then-proxy calls back to back to test the retry sequencing
    itself, which real rate limiting would just make slow, not more correct."""

    async def _no_wait() -> None:
        return None

    monkeypatch.setattr(website_module._FETCH_LIMITER, "wait", _no_wait)


# --- integrations/outbound.py: client construction -----------------------------------


def test_proxy_client_is_none_when_unconfigured() -> None:
    assert outbound.proxy_client(_settings(scrape_proxy_url=None)) is None


def test_proxy_client_is_constructed_when_a_url_is_set() -> None:
    client = outbound.proxy_client(_settings(scrape_proxy_url="http://proxy.test:8080"))
    assert client is not None


def test_direct_and_proxy_clients_share_the_same_honest_user_agent() -> None:
    direct = outbound.direct_client()
    proxy = outbound.proxy_client(_settings(scrape_proxy_url="http://proxy.test:8080"))
    assert proxy is not None
    assert direct.headers["user-agent"] == outbound.USER_AGENT
    assert proxy.headers["user-agent"] == outbound.USER_AGENT


# --- website.py::_fetch_page_text: the retry sequencing --------------------------------


async def test_a_successful_direct_fetch_never_touches_the_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/html"}, text="<p>hello</p>")

    monkeypatch.setattr(outbound, "direct_client", lambda: _client_for(handler))
    monkeypatch.setattr(outbound, "proxy_client", lambda settings: _raise_if_called())

    text, reason, used_proxy = await website_module._fetch_page_text(
        "https://acme.test/team", settings=_settings(), requires_proxy=False
    )

    assert text == "hello"
    assert reason is None
    assert used_proxy is False


async def test_a_direct_timeout_retries_once_through_the_proxy_and_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def direct_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("connect timed out", request=request)

    def proxy_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/html"}, text="<p>hi</p>")

    monkeypatch.setattr(outbound, "direct_client", lambda: _client_for(direct_handler))
    monkeypatch.setattr(outbound, "proxy_client", lambda settings: _client_for(proxy_handler))

    text, reason, used_proxy = await website_module._fetch_page_text(
        "https://acme.test/team",
        settings=_settings(scrape_proxy_url="http://proxy.test:8080"),
        requires_proxy=False,
    )

    assert text == "hi"
    assert reason is None
    assert used_proxy is True


async def test_a_direct_timeout_with_no_proxy_configured_returns_the_direct_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def direct_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("read timed out", request=request)

    monkeypatch.setattr(outbound, "direct_client", lambda: _client_for(direct_handler))
    monkeypatch.setattr(outbound, "proxy_client", lambda settings: None)

    text, reason, used_proxy = await website_module._fetch_page_text(
        "https://acme.test/team", settings=_settings(), requires_proxy=False
    )

    assert text is None
    assert reason == "timeout"
    assert used_proxy is False


async def test_a_direct_403_also_retries_through_the_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def direct_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="forbidden")

    def proxy_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/html"}, text="<p>ok</p>")

    monkeypatch.setattr(outbound, "direct_client", lambda: _client_for(direct_handler))
    monkeypatch.setattr(outbound, "proxy_client", lambda settings: _client_for(proxy_handler))

    text, _reason, used_proxy = await website_module._fetch_page_text(
        "https://acme.test/team",
        settings=_settings(scrape_proxy_url="http://proxy.test:8080"),
        requires_proxy=False,
    )

    assert text == "ok"
    assert used_proxy is True


async def test_a_direct_404_is_never_retried_through_the_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """404 classifies as `_FetchAttempt(None, None)` -- `None` is not in
    `_RETRIABLE_VIA_PROXY`, so this must return immediately, not spend a
    proxy attempt on something a different IP can never fix."""

    def direct_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="not found")

    monkeypatch.setattr(outbound, "direct_client", lambda: _client_for(direct_handler))
    monkeypatch.setattr(outbound, "proxy_client", lambda settings: _raise_if_called())

    text, reason, used_proxy = await website_module._fetch_page_text(
        "https://acme.test/team",
        settings=_settings(scrape_proxy_url="http://proxy.test:8080"),
        requires_proxy=False,
    )

    assert text is None
    assert reason is None
    assert used_proxy is False


async def test_a_429_is_never_retried_through_the_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The critical guardrail: retrying a rate-limit response through a
    different IP would be functionally evading the rate limit itself."""

    def direct_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, text="slow down")

    monkeypatch.setattr(outbound, "direct_client", lambda: _client_for(direct_handler))
    monkeypatch.setattr(outbound, "proxy_client", lambda settings: _raise_if_called())

    text, reason, used_proxy = await website_module._fetch_page_text(
        "https://acme.test/team",
        settings=_settings(scrape_proxy_url="http://proxy.test:8080"),
        requires_proxy=False,
    )

    assert text is None
    assert reason == "rate_limited"
    assert used_proxy is False


async def test_requires_proxy_skips_the_direct_attempt_entirely(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def proxy_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/html"}, text="<p>via proxy</p>")

    monkeypatch.setattr(outbound, "direct_client", lambda: _raise_if_called())
    monkeypatch.setattr(outbound, "proxy_client", lambda settings: _client_for(proxy_handler))

    text, _reason, used_proxy = await website_module._fetch_page_text(
        "https://known-blocked.test/team",
        settings=_settings(scrape_proxy_url="http://proxy.test:8080"),
        requires_proxy=True,
    )

    assert text == "via proxy"
    assert used_proxy is True


async def test_requires_proxy_with_no_proxy_configured_degrades_to_a_synthetic_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A domain flagged `requires_proxy` whose credential was later unset must
    not silently fall through to a direct attempt -- it fails the same way an
    unconfigured proxy always does, not by contradicting its own flag."""
    monkeypatch.setattr(outbound, "direct_client", lambda: _raise_if_called())
    monkeypatch.setattr(outbound, "proxy_client", lambda settings: None)

    text, reason, used_proxy = await website_module._fetch_page_text(
        "https://known-blocked.test/team", settings=_settings(), requires_proxy=True
    )

    assert text is None
    assert reason == "timeout"
    assert used_proxy is False
