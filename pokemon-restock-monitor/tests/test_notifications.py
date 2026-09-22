"""Notifications: Discord format, dedupe (case 10), restart safety."""

import json

import httpx
import pytest
from sqlalchemy import func, select

from app.models import Event, EventType, InventoryStatus as S, Notification
from app.notifications.base import AlertMessage, NotificationError, Notifier
from app.notifications.discord import DiscordNotifier
from app.notifications.telegram import TelegramNotifier

WEBHOOK = "https://discord.com/api/webhooks/123/secret-token"


def sample_message():
    return AlertMessage(kind="restock", title="🚨 POKÉMON RESTOCK DETECTED",
                        product_name="Pokémon 30th Anniversary Collection", retailer="Target",
                        store="Athens, TN", status="AVAILABLE", sku="123456789", detected="4:17:32 PM EDT",
                        url="https://www.target.com/p/-/A-123456789")


async def test_discord_payload_format():
    captured = []

    def handler(request):
        captured.append(request)
        return httpx.Response(200, json={"id": "1"})

    d = DiscordNotifier(WEBHOOK, transport=httpx.MockTransport(handler))
    await d.send(sample_message())
    req = captured[0]
    assert req.url.params["with_components"] == "true" and req.url.params["wait"] == "true"
    body = json.loads(req.content)
    embed = body["embeds"][0]
    assert embed["title"] == "🚨 POKÉMON RESTOCK DETECTED"
    fields = {f["name"]: f["value"] for f in embed["fields"]}
    assert fields == {"Product": "Pokémon 30th Anniversary Collection", "Retailer": "Target",
                      "Store": "Athens, TN", "Status": "AVAILABLE", "SKU": "123456789",
                      "Detected": "4:17:32 PM EDT"}
    assert [f["name"] for f in embed["fields"]] == ["Product", "Retailer", "Store", "Status", "SKU", "Detected"]
    button = body["components"][0]["components"][0]
    assert button == {"type": 2, "style": 5, "label": "OPEN PRODUCT", "url": "https://www.target.com/p/-/A-123456789"}
    assert embed["url"] == "https://www.target.com/p/-/A-123456789"
    await d.aclose()


async def test_discord_falls_back_without_buttons():
    calls = []

    def handler(request):
        calls.append(json.loads(request.content))
        return httpx.Response(400, text="bad components") if "components" in calls[-1] else httpx.Response(200)

    d = DiscordNotifier(WEBHOOK, transport=httpx.MockTransport(handler))
    await d.send(sample_message())
    assert len(calls) == 2 and "components" not in calls[1]
    assert "[OPEN PRODUCT](https://www.target.com/p/-/A-123456789)" in calls[1]["embeds"][0]["description"]
    await d.aclose()


async def test_discord_errors_do_not_leak_webhook_token():
    def handler(request):
        raise httpx.ConnectError("boom")

    d = DiscordNotifier(WEBHOOK, transport=httpx.MockTransport(handler))
    with pytest.raises(NotificationError) as exc:
        await d.send(sample_message())
    assert "secret-token" not in str(exc.value) and exc.value.retryable
    await d.aclose()


async def test_telegram_payload_has_open_button():
    captured = []
    t = TelegramNotifier("tok", "42", transport=httpx.MockTransport(
        lambda r: captured.append(json.loads(r.content)) or httpx.Response(200, json={"ok": True})))
    await t.send(sample_message())
    kb = captured[0]["reply_markup"]["inline_keyboard"][0][0]
    assert kb == {"text": "OPEN PRODUCT", "url": "https://www.target.com/p/-/A-123456789"}
    assert "Athens, TN" in captured[0]["text"]
    await t.aclose()


def test_plain_text_layout():
    text = sample_message().as_text()
    assert text.startswith("🚨 POKÉMON RESTOCK DETECTED\n\nProduct:\nPokémon 30th Anniversary Collection\n\nRetailer:\nTarget")
    assert text.endswith("OPEN PRODUCT:\nhttps://www.target.com/p/-/A-123456789")


# -- dedupe -------------------------------------------------------------------------------
def sent_rows(h):
    with h.rt.db.session() as s:
        return s.scalar(select(func.count(Notification.id)).where(Notification.status == "SENT"))


async def restock(h, sku="A"):
    pid = h.add(sku)
    h.sim.set_status(sku, S.OUT_OF_STOCK)
    await h.check(pid)
    h.sim.set_status(sku, S.AVAILABLE)
    out = await h.check(pid)
    return pid, out.confirmed_event_id


async def test_resending_same_event_is_suppressed(harness):
    pid, event_id = await restock(harness)
    assert len(harness.alerts) == 1
    for _ in range(5):
        report = await harness.rt.notifications.send_restock_alert(event_id)
        assert report.sent == [] and report.skipped == ["console"]
    assert len(harness.alerts) == 1 and sent_rows(harness) == 1


async def test_no_duplicate_alert_after_restart(harness):
    pid, _ = await restock(harness)
    rt = harness.restart()
    await rt.monitor.recover()
    for _ in range(3):
        await harness.check(pid)  # still AVAILABLE after restart
    assert harness.alerts == []  # new console instance received nothing
    assert sent_rows(harness) == 1


async def test_unsent_alert_is_delivered_after_crash(harness):
    """Crash between RESTOCK_CONFIRMED and delivery -> recovery sends it exactly once."""

    class Crashing(Notifier):
        name = "console"

        async def send(self, message):
            raise NotificationError("process died", retryable=False)

    harness.rt.notifications.channels = [Crashing()]
    pid, event_id = await restock(harness)
    with harness.rt.db.session() as s:
        assert not s.get(Event, event_id).notification_sent
    rt = harness.restart()
    summary = await rt.monitor.recover()
    assert summary["resent_notifications"] == 1 and len(harness.alerts) == 1
    summary = await rt.monitor.recover()  # idempotent
    assert summary["resent_notifications"] == 0 and len(harness.alerts) == 1


async def test_failed_channel_does_not_block_others(harness, settings):
    class Broken(Notifier):
        name = "discord"

        async def send(self, message):
            raise NotificationError("HTTP 500", retryable=True)

    async def nosleep(_):
        pass

    harness.rt.notifications.channels.insert(0, Broken())
    harness.rt.notifications.channel_status["discord"] = {"last_sent_at": None, "last_error": None}
    harness.rt.notifications._retry_sleep = nosleep
    pid, event_id = await restock(harness)
    with harness.rt.db.session() as s:
        rows = {n.channel: n for n in s.scalars(select(Notification))}
        assert rows["discord"].status == "FAILED" and rows["discord"].attempts == 3
        assert rows["console"].status == "SENT"
        assert s.get(Event, event_id).notification_sent


async def test_concurrent_sends_deliver_once(harness):
    import asyncio

    pid, event_id = await restock(harness)
    with harness.rt.db.session() as s:
        s.execute(Notification.__table__.delete())
    reports = await asyncio.gather(*[harness.rt.notifications.send_restock_alert(event_id) for _ in range(5)])
    assert sum(len(r.sent) for r in reports) == 1


async def test_system_alert_dedupes(harness):
    for _ in range(3):
        await harness.rt.notifications.send_system_alert("target:BLOCKED:2026092216", "Blocked", "x")
    assert len([m for m in harness.console.sent if m.kind == "system"]) == 1


async def test_only_confirmed_events_can_alert(harness):
    pid = harness.add("A")
    with harness.rt.db.session() as s:
        ev = Event(event_type=EventType.RESTOCK_DETECTED.value, product_id=pid)
        s.add(ev)
        s.flush()
        eid = ev.id
    with pytest.raises(ValueError):
        await harness.rt.notifications.send_restock_alert(eid)


async def test_notification_service_builds_target_message(harness):
    """Message content for a store-level product."""
    pid = harness.add("T", store_id="S1", store_name="Athens Target", city="Athens", state="TN", max_quantity=2)
    harness.sim.set_status("T", S.OUT_OF_STOCK, store_id="S1")
    await harness.check(pid)
    harness.sim.set_status("T", S.AVAILABLE, store_id="S1")
    await harness.check(pid)
    msg = harness.alerts[0]
    assert msg.store == "Athens, TN (Athens Target)" and msg.extra["Max qty to buy"] == "2"
    assert msg.detected.endswith(("EDT", "EST"))
