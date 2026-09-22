"""Discord webhook notifications.

Sends an embed laid out like:

    🚨 POKÉMON RESTOCK DETECTED
    Product / Retailer / Store / Status / SKU / Detected
    [OPEN PRODUCT]

The OPEN PRODUCT link button points at the retailer's product URL. Link
buttons on plain webhooks need ``?with_components=true``; if Discord rejects
components we fall back to the embed alone (its title and an "OPEN PRODUCT"
markdown link are clickable anyway).
"""

from __future__ import annotations

import asyncio
import logging

import httpx

from app.notifications.base import AlertMessage, NotificationError, Notifier

logger = logging.getLogger("notify.discord")

COLORS = {"restock": 0xE3350D, "system": 0xF5A623, "test": 0x3B82F6}


class DiscordNotifier(Notifier):
    name = "discord"

    def __init__(self, webhook_url: str, mention: str | None = None, use_buttons: bool = True,
                 transport: httpx.AsyncBaseTransport | None = None, timeout: float = 10,
                 sleep=asyncio.sleep):
        if not webhook_url.startswith("https://"):
            raise ValueError("DISCORD_WEBHOOK_URL must be an https:// Discord webhook URL")
        self.webhook_url = webhook_url
        self.mention = mention
        self.use_buttons = use_buttons
        self._client = httpx.AsyncClient(timeout=timeout, transport=transport)
        self._sleep = sleep

    def build_payload(self, msg: AlertMessage, with_components: bool) -> dict:
        title = msg.title if not msg.is_simulation else f"[SIMULATION] {msg.title}"
        fields = [{"name": k, "value": str(v)[:1024], "inline": False} for k, v in msg.core_fields()]
        fields += [{"name": k, "value": str(v)[:1024], "inline": True} for k, v in msg.extra.items()]
        embed: dict = {"title": title[:256], "color": COLORS.get(msg.kind, 0x888888), "fields": fields[:25]}
        description = []
        if msg.text:
            description.append(msg.text)
        if msg.url:
            embed["url"] = msg.url
            description.append(f"**[OPEN PRODUCT]({msg.url})**")
        for label, link in msg.links:
            description.append(f"[{label}]({link})")
        if description:
            embed["description"] = "\n\n".join(description)[:4000]
        if msg.image_url:
            embed["thumbnail"] = {"url": msg.image_url}
        content = title
        if self.mention and msg.kind == "restock":
            content = f"{self.mention} {content}"
        payload: dict = {
            "content": content[:2000],
            "embeds": [embed],
            "allowed_mentions": {"parse": ["everyone", "roles"] if self.mention else []},
        }
        if with_components and msg.url:
            payload["components"] = [{
                "type": 1,
                "components": [{"type": 2, "style": 5, "label": "OPEN PRODUCT", "url": msg.url}]
                + [{"type": 2, "style": 5, "label": label[:80], "url": link} for label, link in msg.links[:4]],
            }]
        return payload

    async def _post(self, payload: dict, with_components: bool) -> httpx.Response:
        params = {"wait": "true"}
        if with_components:
            params["with_components"] = "true"
        try:
            return await self._client.post(self.webhook_url, json=payload, params=params)
        except httpx.HTTPError as exc:
            # Never include the webhook URL (it contains the token) in errors.
            raise NotificationError(f"Discord request failed: {type(exc).__name__}", retryable=True) from None

    async def send(self, message: AlertMessage) -> None:
        with_components = self.use_buttons and bool(message.url)
        resp = await self._post(self.build_payload(message, with_components), with_components)
        if resp.status_code == 429:
            retry_after = _discord_retry_after(resp)
            if retry_after is not None and retry_after <= 10:
                await self._sleep(retry_after)
                resp = await self._post(self.build_payload(message, with_components), with_components)
            else:
                raise NotificationError("Discord rate limited", retryable=True, retry_after=retry_after)
        if resp.status_code == 400 and with_components:
            logger.warning("Discord rejected link button; retrying with embed only: %s", resp.text[:200])
            resp = await self._post(self.build_payload(message, False), False)
        if resp.status_code >= 400:
            raise NotificationError(
                f"Discord HTTP {resp.status_code}: {resp.text[:200]}",
                retryable=resp.status_code >= 500 or resp.status_code == 429,
            )

    async def aclose(self) -> None:
        await self._client.aclose()


def _discord_retry_after(resp: httpx.Response) -> float | None:
    try:
        return float(resp.json().get("retry_after"))
    except Exception:  # noqa: BLE001
        try:
            return float(resp.headers.get("Retry-After"))
        except (TypeError, ValueError):
            return None
