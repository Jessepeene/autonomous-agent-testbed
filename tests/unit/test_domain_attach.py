"""Vercel domain attachment + verification -- the pure HTTP-calling pieces.

Same mocking convention as test_deploy_readiness.py's identical coverage of
`deploy()`'s own Vercel call: `monkeypatch.setattr(httpx.AsyncClient, ...)`,
no real network. What matters here: a missing token or infra-shaped failure
(5xx/429/401/403/transport error) is flagged `is_infra=True` so `graph.py`
routes it to `DevOpsError` instead of a heal cycle -- domain attachment can
never be "fixed" by regenerating the product's code.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from factory.engines.development import domain as domain_module
from factory.settings import Settings

_REQUEST = httpx.Request("POST", "https://api.vercel.com/v10/projects/x/domains")


def _live(settings: Settings) -> Settings:
    return settings.model_copy(update={"vercel_token": "vercel-test-token"})


# --- attach() ------------------------------------------------------------------


async def test_attach_without_a_token_is_a_clean_infra_failure(settings: Settings) -> None:
    result = await domain_module.attach(settings, project="proj-1", hostname="example.com")

    assert result.ok is False
    assert result.is_infra is True


async def test_attach_reports_verified_true_when_vercel_says_so(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_post(self: httpx.AsyncClient, url: str, **kwargs: Any) -> httpx.Response:
        return httpx.Response(200, json={"verified": True}, request=_REQUEST)

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    result = await domain_module.attach(_live(settings), project="proj-1", hostname="example.com")

    assert result.ok is True
    assert result.verified is True
    assert result.records == []


async def test_attach_surfaces_the_dns_records_vercel_asks_for(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_post(self: httpx.AsyncClient, url: str, **kwargs: Any) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "verified": False,
                "verification": [
                    {"type": "TXT", "domain": "_vercel.example.com", "value": "vc-verify-abc123"}
                ],
            },
            request=_REQUEST,
        )

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    result = await domain_module.attach(_live(settings), project="proj-1", hostname="example.com")

    assert result.ok is True
    assert result.verified is False
    assert len(result.records) == 1
    assert result.records[0].type == "TXT"
    assert result.records[0].value == "vc-verify-abc123"


@pytest.mark.parametrize("status", [401, 403, 429, 500, 502, 503, 504])
async def test_infra_status_codes_are_flagged_is_infra(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    async def fake_post(self: httpx.AsyncClient, url: str, **kwargs: Any) -> httpx.Response:
        return httpx.Response(status, text="upstream trouble", request=_REQUEST)

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    result = await domain_module.attach(_live(settings), project="proj-1", hostname="example.com")

    assert result.ok is False
    assert result.is_infra is True


async def test_a_content_rejection_is_not_flagged_infra(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A malformed hostname is genuinely rejected content, not an outage -- a
    different reply from the human might avoid it, so this must not be routed
    to the same devops-escalation path as a real Vercel outage."""

    async def fake_post(self: httpx.AsyncClient, url: str, **kwargs: Any) -> httpx.Response:
        return httpx.Response(400, text="invalid domain", request=_REQUEST)

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    result = await domain_module.attach(_live(settings), project="proj-1", hostname="not a domain")

    assert result.ok is False
    assert result.is_infra is False


async def test_a_transport_error_is_infra(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fail_post(self: httpx.AsyncClient, url: str, **kwargs: Any) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=_REQUEST)

    monkeypatch.setattr(httpx.AsyncClient, "post", fail_post)

    result = await domain_module.attach(_live(settings), project="proj-1", hostname="example.com")

    assert result.ok is False
    assert result.is_infra is True


# --- check_verified() -----------------------------------------------------------


async def test_check_verified_without_a_token_returns_none(settings: Settings) -> None:
    result = await domain_module.check_verified(settings, project="proj-1", hostname="example.com")

    assert result is None


async def test_check_verified_returns_the_real_boolean(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_get(self: httpx.AsyncClient, url: str, **kwargs: Any) -> httpx.Response:
        return httpx.Response(200, json={"verified": True}, request=_REQUEST)

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)

    result = await domain_module.check_verified(
        _live(settings), project="proj-1", hostname="example.com"
    )

    assert result is True


async def test_check_verified_returns_none_on_transport_failure(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """None, not False -- a transient Vercel outage must never be misread as
    'DNS is not configured yet,' which would just be a true statement about a
    different failure mode."""

    async def fail_get(self: httpx.AsyncClient, url: str, **kwargs: Any) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=_REQUEST)

    monkeypatch.setattr(httpx.AsyncClient, "get", fail_get)

    result = await domain_module.check_verified(
        _live(settings), project="proj-1", hostname="example.com"
    )

    assert result is None


async def test_check_verified_returns_none_on_a_non_200(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_get(self: httpx.AsyncClient, url: str, **kwargs: Any) -> httpx.Response:
        return httpx.Response(404, text="not found", request=_REQUEST)

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)

    result = await domain_module.check_verified(
        _live(settings), project="proj-1", hostname="example.com"
    )

    assert result is None


# --- format_dns_instructions() ---------------------------------------------------


def test_format_dns_instructions_lists_every_record() -> None:
    records = [
        domain_module.DnsRecord(type="TXT", name="_vercel.example.com", value="vc-verify-abc"),
        domain_module.DnsRecord(type="A", name="example.com", value="76.76.21.21"),
    ]

    text = domain_module.format_dns_instructions("example.com", records)

    assert "TXT" in text
    assert "vc-verify-abc" in text
    assert "A" in text
    assert "76.76.21.21" in text


def test_format_dns_instructions_handles_no_records() -> None:
    text = domain_module.format_dns_instructions("example.com", [])

    assert "example.com" in text
    assert "attached" in text
