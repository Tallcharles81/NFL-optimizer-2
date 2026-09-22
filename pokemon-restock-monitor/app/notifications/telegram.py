"""Telegram bot notifications (sendMessage with an OPEN PRODUCT URL button)."""

from __future__ import annotations

import httpx

from app.notifications.base import AlertMessage, NotificationError, Notifier


class TelegramNotifier(Notifier):
    name = "telegram"

    def __init__(self, bot_token: str, chat_id: str, transport: httpx.AsyncBaseTransport | None = None,
                 timeout: float = 10):
        self._url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        self.chat_id = chat_id
        self._client = httpx.AsyncClient(timeout=timeout, transport=transport)

    def build_payload(self, msg: AlertMessage) -> dict:
        text = msg.as_text()
        if msg.is_simulation:
            text = "[SIMULATION]\n" + text
        payload = {"chat_id": self.chat_id, "text": text[:4000], "disable_web_page_preview": False}
        if msg.url:
            payload["reply_markup"] = {"inline_keyboard": [[{"text": "OPEN PRODUCT", "url": msg.url}]]
                                       + [[{"text": label, "url": link}] for label, link in msg.links]}
        return payload

    async def send(self, message: AlertMessage) -> None:
        try:
            resp = await self._client.post(self._url, json=self.build_payload(message))
        except httpx.HTTPError as exc:
            # Don't leak the bot token (it is part of the URL).
            raise NotificationError(f"Telegram request failed: {type(exc).__name__}", retryable=True) from None
        if resp.status_code >= 400:
            raise NotificationError(f"Telegram HTTP {resp.status_code}: {resp.text[:200]}",
                                    retryable=resp.status_code >= 500 or resp.status_code == 429)

    async def aclose(self) -> None:
        await self._client.aclose()
