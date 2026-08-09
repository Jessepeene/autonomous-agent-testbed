"""Telegram chat-id discovery.

Getting a bot token from @BotFather is the easy half of Telegram setup; finding the
chat id to send escalations to is the step operators get stuck on, because it is not
shown anywhere in the BotFather flow — it only appears in ``getUpdates`` after the
operator has messaged the bot. These tests exercise that discovery against a mocked
Telegram API, no real bot required.
"""

from __future__ import annotations

import httpx
import pytest

from factory.hitl.telegram import NoBotToken, discover_chats

_REQUEST = httpx.Request("GET", "https://api.telegram.org/botfake-token/getUpdates")


def _updates_response(*chats: dict[str, object]) -> httpx.Response:
    results = [
        {
            "update_id": 100 + i,
            "message": {"message_id": i, "chat": chat, "text": "hi"},
        }
        for i, chat in enumerate(chats)
    ]
    return httpx.Response(200, json={"ok": True, "result": results}, request=_REQUEST)


async def test_discover_chats_returns_nothing_when_no_one_has_messaged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_get(self: httpx.AsyncClient, url: str, **kwargs: object) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "result": []}, request=_REQUEST)

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    assert await discover_chats("fake-token") == []


async def test_discover_chats_extracts_a_private_chat(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_get(self: httpx.AsyncClient, url: str, **kwargs: object) -> httpx.Response:
        return _updates_response(
            {"id": 123456789, "type": "private", "first_name": "Jesse", "last_name": "P"}
        )

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    chats = await discover_chats("fake-token")

    assert len(chats) == 1
    assert chats[0]["id"] == 123456789
    assert chats[0]["type"] == "private"
    assert chats[0]["label"] == "Jesse P"


async def test_discover_chats_extracts_a_group_chat_by_title(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_get(self: httpx.AsyncClient, url: str, **kwargs: object) -> httpx.Response:
        return _updates_response({"id": -100200300, "type": "group", "title": "Ops Alerts"})

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    chats = await discover_chats("fake-token")

    assert chats[0]["id"] == -100200300
    assert chats[0]["label"] == "Ops Alerts"


async def test_discover_chats_deduplicates_repeated_messages_from_the_same_chat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chat = {"id": 1, "type": "private", "first_name": "Jesse"}

    async def fake_get(self: httpx.AsyncClient, url: str, **kwargs: object) -> httpx.Response:
        return _updates_response(chat, chat, chat)

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    chats = await discover_chats("fake-token")

    assert len(chats) == 1


async def test_discover_chats_handles_multiple_distinct_chats(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_get(self: httpx.AsyncClient, url: str, **kwargs: object) -> httpx.Response:
        return _updates_response(
            {"id": 1, "type": "private", "first_name": "Alice"},
            {"id": 2, "type": "private", "first_name": "Bob"},
        )

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    chats = await discover_chats("fake-token")

    assert {c["id"] for c in chats} == {1, 2}


async def test_a_rejected_token_raises_a_clear_error(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_get(self: httpx.AsyncClient, url: str, **kwargs: object) -> httpx.Response:
        return httpx.Response(401, json={"ok": False, "description": "Unauthorized"})

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    with pytest.raises(NoBotToken):
        await discover_chats("wrong-token")


async def test_unrelated_server_error_raises_rather_than_reporting_no_chats(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 500 from Telegram is a transport problem, not "nobody has messaged yet" —
    the two must not be reported identically."""

    async def fake_get(self: httpx.AsyncClient, url: str, **kwargs: object) -> httpx.Response:
        return httpx.Response(500, json={"ok": False}, request=_REQUEST)

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    with pytest.raises(httpx.HTTPStatusError):
        await discover_chats("fake-token")


async def test_update_with_no_chat_is_skipped_without_crashing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_get(self: httpx.AsyncClient, url: str, **kwargs: object) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "ok": True,
                "result": [{"update_id": 1, "my_chat_member": {"date": 0}}],
            },
            request=_REQUEST,
        )

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    assert await discover_chats("fake-token") == []
