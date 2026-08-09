"""Telegram notifier.

Uses ``getUpdates`` polling rather than a webhook so the factory needs no inbound
networking, no public hostname, and no TLS certificate on the VPS.

When the bot token is absent this object reports itself unavailable and every call
is a no-op — escalations stay queued in Postgres and flush once a token exists.
"""

from __future__ import annotations

import httpx

from factory.logging import get_logger
from factory.settings import Settings, get_settings

log = get_logger(__name__)

_TIMEOUT = httpx.Timeout(20.0, connect=5.0)
_MESSAGE_LIMIT = 3900  # Telegram hard-caps at 4096 characters.


class TelegramNotifier:
    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._offset: int | None = None

    @property
    def available(self) -> bool:
        return self._settings.has_telegram

    @property
    def _base(self) -> str:
        token = self._settings.secret("TELEGRAM_BOT_TOKEN")
        return f"https://api.telegram.org/bot{token}"

    async def send(self, text: str) -> bool:
        if not self.available:
            return False
        payload = {
            "chat_id": self._settings.telegram_chat_id,
            "text": text[:_MESSAGE_LIMIT],
            "disable_web_page_preview": True,
        }
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                response = await client.post(f"{self._base}/sendMessage", json=payload)
        except httpx.HTTPError as exc:
            log.warning("telegram.send_failed", detail=str(exc)[:200])
            return False
        if response.status_code != 200:
            log.warning("telegram.send_rejected", status=response.status_code)
            return False
        return True

    async def poll_replies(self) -> list[str]:
        """Fetch new messages addressed to the bot since the last poll."""
        if not self.available:
            return []
        params: dict[str, str | int] = {"timeout": 0}
        if self._offset is not None:
            params["offset"] = self._offset
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                response = await client.get(f"{self._base}/getUpdates", params=params)
        except httpx.HTTPError as exc:
            log.warning("telegram.poll_failed", detail=str(exc)[:200])
            return []
        if response.status_code != 200:
            return []

        messages: list[str] = []
        for update in response.json().get("result", []):
            update_id = update.get("update_id")
            if isinstance(update_id, int):
                self._offset = update_id + 1
            message = update.get("message") or update.get("edited_message") or {}
            text = message.get("text")
            if text:
                messages.append(str(text))
        return messages


class NullNotifier:
    """Used in dry runs and tests; keeps escalations queued rather than sending."""

    def __init__(self) -> None:
        self.sent: list[str] = []

    @property
    def available(self) -> bool:
        return False

    async def send(self, text: str) -> bool:
        self.sent.append(text)
        return False

    async def poll_replies(self) -> list[str]:
        return []


def build_notifier(settings: Settings | None = None) -> TelegramNotifier | NullNotifier:
    settings = settings or get_settings()
    return TelegramNotifier(settings) if settings.has_telegram else NullNotifier()


# --- one-time setup helper ---------------------------------------------------
#
# A bot token alone is not enough to send messages — Telegram also needs the chat
# to send them to, and that chat id is not visible anywhere in the BotFather flow.
# The standard way to find it is "message the bot, then read the id back off
# getUpdates", which is exactly what this discovers. This is the step operators get
# stuck on, so it is a first-class command rather than a paragraph in a README.


class NoBotToken(RuntimeError):
    """TELEGRAM_BOT_TOKEN is not set; there is nothing to query."""


async def discover_chats(bot_token: str) -> list[dict[str, object]]:
    """Every chat that has messaged this bot, most recent first.

    Returns raw chat objects (id, type, and a human-readable title/username/name)
    rather than bare ids, so an operator managing multiple bots or groups can tell
    them apart before picking one.
    """
    url = f"https://api.telegram.org/bot{bot_token}/getUpdates"
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        response = await client.get(url, params={"timeout": 0, "limit": 100})

    if response.status_code == 401:
        raise NoBotToken(f"Telegram rejected this token (401): {response.text[:200]}")
    response.raise_for_status()

    seen: dict[int, dict[str, object]] = {}
    for update in response.json().get("result", []):
        message = update.get("message") or update.get("edited_message") or {}
        chat = message.get("chat")
        if not chat or "id" not in chat:
            continue
        chat_id = int(chat["id"])
        label = (
            chat.get("title")
            or chat.get("username")
            or " ".join(filter(None, [chat.get("first_name"), chat.get("last_name")]))
            or "(no name)"
        )
        # Last-seen wins if the same chat appears twice — most recent list order.
        seen[chat_id] = {"id": chat_id, "type": chat.get("type", "unknown"), "label": label}

    return list(reversed(seen.values()))


def cli() -> int:
    """``factory-telegram-chatid`` — the missing step between "create a bot" and
    "set TELEGRAM_CHAT_ID"."""
    import asyncio
    import sys

    settings = get_settings()
    token = settings.secret("TELEGRAM_BOT_TOKEN")
    if not token:
        print(
            "TELEGRAM_BOT_TOKEN is not set. Set it in your environment or .env file "
            "first — see DEPLOYMENT.md for how to create a bot with @BotFather.",
            file=sys.stderr,
        )
        return 2

    try:
        chats = asyncio.run(discover_chats(token))
    except NoBotToken as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except httpx.HTTPError as exc:
        print(f"Could not reach Telegram: {exc}", file=sys.stderr)
        return 2

    if not chats:
        print(
            "No messages found yet. Open a chat with your bot in Telegram, send it "
            'any message (e.g. "hi"), then run this command again.\n'
            "For a group, add the bot to the group and send a message there instead."
        )
        return 1

    print(f"Found {len(chats)} chat(s) that have messaged this bot:\n")
    for chat in chats:
        print(f"  {chat['id']:<15} {chat['type']:<10} {chat['label']}")
    print("\nSet TELEGRAM_CHAT_ID to the id you want the factory to send escalations to.")
    return 0
