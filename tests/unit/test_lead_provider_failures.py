"""Classifying a rejected lead-provider request -- the same mocking convention
as test_domain_attach.py's coverage of Vercel: `monkeypatch.setattr(
httpx.AsyncClient, ...)`, no real network.

HTTP 401/403 means the key is present (`available` already checked that) but
being refused -- an account problem only a human can fix, not something a
retry with the same key will ever resolve. HTTP 400/422 means the request
itself is malformed -- a bug in how this factory calls the provider, not an
account problem. Both are raised as a distinguishable exception instead of the
previous silent `return []`, so a caller (`leads.py::ingest`) can tell "found
nobody" from "the request itself failed" and act very differently on each.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from factory.engines.marketing.providers.apollo import ApolloProvider
from factory.engines.marketing.providers.base import (
    LeadSearchPlan,
    ProviderAuthError,
    ProviderRequestError,
)
from factory.engines.marketing.providers.hunter import HunterProvider, find_email
from factory.settings import Settings

_REQUEST = httpx.Request("POST", "https://api.apollo.io/api/v1/mixed_people/search")
_HUNTER_REQUEST = httpx.Request("GET", "https://api.hunter.io/v2/domain-search")
_HUNTER_FINDER_REQUEST = httpx.Request("GET", "https://api.hunter.io/v2/email-finder")


def _apollo(settings: Settings) -> ApolloProvider:
    return ApolloProvider(settings.model_copy(update={"apollo_api_key": "apollo-test-key"}))


def _hunter(settings: Settings) -> HunterProvider:
    return HunterProvider(settings.model_copy(update={"hunter_api_key": "hunter-test-key"}))


def _plan() -> LeadSearchPlan:
    return LeadSearchPlan(
        job_titles=["Financial Controller"],
        company_domains=["example.test"],
        rationale="Controllers approve recurring software spend.",
    )


# --- ApolloProvider --------------------------------------------------------------------


@pytest.mark.parametrize("status", [401, 403])
async def test_apollo_raises_auth_error_on_401_and_403(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    async def fake_post(self: httpx.AsyncClient, url: str, **kwargs: Any) -> httpx.Response:
        return httpx.Response(status, text="Invalid API Key", request=_REQUEST)

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    with pytest.raises(ProviderAuthError) as excinfo:
        await _apollo(settings).search(_plan())

    assert excinfo.value.status_code == status
    assert excinfo.value.provider == "apollo.io"


@pytest.mark.parametrize("status", [400, 422])
async def test_apollo_raises_request_error_on_400_and_422(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    async def fake_post(self: httpx.AsyncClient, url: str, **kwargs: Any) -> httpx.Response:
        return httpx.Response(status, text="Unprocessable parameters", request=_REQUEST)

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    with pytest.raises(ProviderRequestError) as excinfo:
        await _apollo(settings).search(_plan())

    assert excinfo.value.status_code == status


@pytest.mark.parametrize("status", [429, 500, 503])
async def test_apollo_degrades_gracefully_on_transient_failures(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    """429/5xx may resolve on their own -- these must not raise, unlike the
    diagnosable cases above, matching the existing graceful-degrade behaviour."""

    async def fake_post(self: httpx.AsyncClient, url: str, **kwargs: Any) -> httpx.Response:
        return httpx.Response(status, text="try later", request=_REQUEST)

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    result = await _apollo(settings).search(_plan())

    assert result == []


# --- HunterProvider --------------------------------------------------------------------


@pytest.mark.parametrize("status", [401, 403])
async def test_hunter_raises_auth_error_on_401_and_403(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    async def fake_get(self: httpx.AsyncClient, url: str, **kwargs: Any) -> httpx.Response:
        return httpx.Response(status, text="Invalid API Key", request=_HUNTER_REQUEST)

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)

    with pytest.raises(ProviderAuthError) as excinfo:
        await _hunter(settings).search(_plan())

    assert excinfo.value.status_code == status
    assert excinfo.value.provider == "hunter.io"


@pytest.mark.parametrize("status", [400, 422])
async def test_hunter_raises_request_error_on_400_and_422(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    async def fake_get(self: httpx.AsyncClient, url: str, **kwargs: Any) -> httpx.Response:
        return httpx.Response(status, text="bad domain", request=_HUNTER_REQUEST)

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)

    with pytest.raises(ProviderRequestError) as excinfo:
        await _hunter(settings).search(_plan())

    assert excinfo.value.status_code == status


async def test_hunter_degrades_gracefully_on_transient_failures(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_get(self: httpx.AsyncClient, url: str, **kwargs: Any) -> httpx.Response:
        return httpx.Response(503, text="try later", request=_HUNTER_REQUEST)

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)

    result = await _hunter(settings).search(_plan())

    assert result == []


async def test_hunter_stops_after_the_first_domain_once_auth_fails(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The key applies uniformly across every domain in the loop -- one 401/403
    already proves the account is the problem, so the remaining domains must
    never be queried with the same rejected key."""
    calls = 0

    async def fake_get(self: httpx.AsyncClient, url: str, **kwargs: Any) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(403, text="Invalid API Key", request=_HUNTER_REQUEST)

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    plan = LeadSearchPlan(
        company_domains=["one.test", "two.test", "three.test"],
        rationale="Multiple domains to search.",
    )

    with pytest.raises(ProviderAuthError):
        await _hunter(settings).search(plan)

    assert calls == 1


# --- Hunter's Email Finder (used only by the web-search lead provider's fallback) -------


@pytest.mark.parametrize("status", [401, 403])
async def test_find_email_raises_auth_error_on_401_and_403(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    async def fake_get(self: httpx.AsyncClient, url: str, **kwargs: Any) -> httpx.Response:
        return httpx.Response(status, text="Invalid API Key", request=_HUNTER_FINDER_REQUEST)

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)

    with pytest.raises(ProviderAuthError) as excinfo:
        await find_email(
            "Ada",
            "Lovelace",
            "acme-books.test",
            settings=settings.model_copy(update={"hunter_api_key": "hunter-test-key"}),
        )

    assert excinfo.value.status_code == status


@pytest.mark.parametrize("status", [400, 422])
async def test_find_email_raises_request_error_on_400_and_422(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    async def fake_get(self: httpx.AsyncClient, url: str, **kwargs: Any) -> httpx.Response:
        return httpx.Response(status, text="bad request", request=_HUNTER_FINDER_REQUEST)

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)

    with pytest.raises(ProviderRequestError) as excinfo:
        await find_email(
            "Ada",
            "Lovelace",
            "acme-books.test",
            settings=settings.model_copy(update={"hunter_api_key": "hunter-test-key"}),
        )

    assert excinfo.value.status_code == status


async def test_find_email_returns_none_without_a_key(settings: Settings) -> None:
    result = await find_email(
        "Ada", "Lovelace", "acme-books.test", settings=settings.model_copy(update={})
    )
    assert result is None


async def test_find_email_returns_the_address_on_a_confident_match(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_get(self: httpx.AsyncClient, url: str, **kwargs: Any) -> httpx.Response:
        return httpx.Response(
            200,
            json={"data": {"email": "ada@acme-books.test", "score": 92}},
            request=_HUNTER_FINDER_REQUEST,
        )

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)

    result = await find_email(
        "Ada",
        "Lovelace",
        "acme-books.test",
        settings=settings.model_copy(update={"hunter_api_key": "hunter-test-key"}),
    )

    assert result == "ada@acme-books.test"


async def test_find_email_returns_none_below_the_confidence_floor(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_get(self: httpx.AsyncClient, url: str, **kwargs: Any) -> httpx.Response:
        return httpx.Response(
            200,
            json={"data": {"email": "ada@acme-books.test", "score": 40}},
            request=_HUNTER_FINDER_REQUEST,
        )

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)

    result = await find_email(
        "Ada",
        "Lovelace",
        "acme-books.test",
        settings=settings.model_copy(update={"hunter_api_key": "hunter-test-key"}),
    )

    assert result is None
