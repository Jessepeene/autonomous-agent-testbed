"""`integrations/serper.py` -- the shared Serper client backing both the
web-search lead provider and the general-purpose `web_search` agent tool.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from factory.integrations.serper import SerperError, available, search
from factory.settings import Settings

_REQUEST = httpx.Request("POST", "https://google.serper.dev/search")


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {"serper_api_key": "serper-test-key"}
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def test_available_false_without_a_key() -> None:
    assert available(_settings(serper_api_key=None)) is False


def test_available_true_with_a_key() -> None:
    assert available(_settings()) is True


async def test_returns_empty_list_when_no_key_is_configured() -> None:
    result = await search("bookkeeping team page", settings=_settings(serper_api_key=None))
    assert result == []


async def test_parses_organic_results(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_post(self: httpx.AsyncClient, url: str, **kwargs: Any) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "organic": [
                    {
                        "title": "Acme Books - Our Team",
                        "link": "https://acme-books.test/team",
                        "snippet": "Meet the team at Acme Books.",
                        "position": 1,
                    },
                    # An entry with no link is not a usable result -- dropped.
                    {"title": "no link here", "snippet": "..."},
                ]
            },
            request=_REQUEST,
        )

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    results = await search("bookkeeping team page", settings=_settings())

    assert len(results) == 1
    assert results[0].title == "Acme Books - Our Team"
    assert results[0].link == "https://acme-books.test/team"
    assert results[0].snippet == "Meet the team at Acme Books."


@pytest.mark.parametrize("status", [401, 403, 400, 422])
async def test_raises_serper_error_on_a_non_200_response(
    monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    async def fake_post(self: httpx.AsyncClient, url: str, **kwargs: Any) -> httpx.Response:
        return httpx.Response(status, text="rejected", request=_REQUEST)

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    with pytest.raises(SerperError) as excinfo:
        await search("bookkeeping team page", settings=_settings())

    assert excinfo.value.status_code == status
