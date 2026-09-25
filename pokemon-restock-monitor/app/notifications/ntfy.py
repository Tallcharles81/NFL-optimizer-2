"""Phone push notifications via ntfy (https://ntfy.sh) -- no account needed.

Install the ntfy app, subscribe to a hard-to-guess topic name, and set
NTFY_TOPIC to the same name. Anyone who knows the topic can read it, so
treat it like a password.
"""

from __future__ import annotations

import httpx

from app.notifications.base import AlertMessage, NotificationError, Notifier


class NtfyNotifier(Notifier):
    name = "ntfy"

    def __init__(self, topic: str, server: str = "https://ntfy.sh", token: str | None = None,
                 transport: httpx.AsyncBaseTransport | None = None, timeout: float = 10):
        self.topic = topic
        self.server = server.rstrip("/")
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        self._client = httpx.AsyncClient(timeout=timeout, transport=transport, headers=headers)

    def build_payload(self, msg: AlertMessage) -> dict:
        title = f"[SIMULATION] {msg.title}" if msg.is_simulation else msg.title
        lines = [f"{k}: {v}" for k, v in [*msg.core_fields(), *msg.extra.items()] if k != "Verified"]
        if msg.text:
            lines.insert(0, msg.text)
        payload = {
            "topic": self.topic,
            "title": title[:250],
            "message": "\n".join(lines)[:3900] or title,
            "priority": 5 if msg.kind == "restock" else 3,
            "tags": {"restock": ["rotating_light"], "status": ["white_check_mark"]}.get(msg.kind, ["warning"]),
        }
        if msg.url:
            payload["click"] = msg.url
            payload["actions"] = [{"action": "view", "label": "OPEN PRODUCT", "url": msg.url, "clear": True}]
            payload["actions"] += [{"action": "view", "label": label, "url": link}
                                   for label, link in msg.links[:2]]
        return payload

    async def send(self, message: AlertMessage) -> None:
        try:
            resp = await self._client.post(self.server, json=self.build_payload(message))
        except httpx.HTTPError as exc:
            raise NotificationError(f"ntfy request failed: {type(exc).__name__}", retryable=True) from None
        if resp.status_code >= 400:
            raise NotificationError(f"ntfy HTTP {resp.status_code}: {resp.text[:200]}",
                                    retryable=resp.status_code >= 500 or resp.status_code == 429)

    async def aclose(self) -> None:
        await self._client.aclose()
