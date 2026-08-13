"""The general-purpose `web_search` tool (`tools/builtin.py`) -- shared by
LEAD_SCOUT and CHANNEL_SCOUT (see `factory/agents/roles.py`'s "search" tool
category), backed by `integrations/serper.py`.
"""

from __future__ import annotations

import pytest

from factory.integrations.serper import SerperError, SerperResult
from factory.settings import Settings
from factory.tools import builtin
from factory.tools.registry import REGISTRY


def test_web_search_is_registered_under_the_search_category_with_its_credential() -> None:
    descriptor = next(t for t in REGISTRY.all() if t.name == "web_search")
    assert descriptor.category == "search"
    assert descriptor.required_credential == "SERPER_API_KEY"


def test_web_search_is_withheld_without_a_key(settings: Settings) -> None:
    assert settings.secret("SERPER_API_KEY") is None
    tools = REGISTRY.get_mcp_tools(category=["search"], settings=settings, docker_available=True)
    assert tools == []

    with_key = settings.model_copy(update={"serper_api_key": "serper-test-key"})
    tools = REGISTRY.get_mcp_tools(category=["search"], settings=with_key, docker_available=True)
    assert {t.name for t in tools} == {"web_search"}


async def test_web_search_returns_narrowed_results(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_search(
        query: str, *, num: int = 10, settings: Settings | None = None
    ) -> list[SerperResult]:
        return [SerperResult(title="Acme Books", link="https://acme-books.test", snippet="...")]

    monkeypatch.setattr("factory.integrations.serper.search", fake_search)

    result = await builtin.web_search("bookkeeping firms")

    assert result == {
        "results": [{"title": "Acme Books", "link": "https://acme-books.test", "snippet": "..."}]
    }


async def test_web_search_returns_an_error_shape_on_rejection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_search(
        query: str, *, num: int = 10, settings: Settings | None = None
    ) -> list[SerperResult]:
        raise SerperError(403, "Invalid API Key")

    monkeypatch.setattr("factory.integrations.serper.search", fake_search)

    result = await builtin.web_search("bookkeeping firms")

    assert result["results"] == []
    assert "403" in result["error"]
