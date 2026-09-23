"""FAST_ALERT: alert on the first sighting, then double-check."""

from sqlalchemy import select

from app.models import Event, EventType, InventoryStatus as S
from app.oneshot import run_once


async def test_fast_alert_pings_once_before_verification(make_harness):
    h = make_harness(fast_alert=True, restock_reminder_max=0)
    pid = h.add("A")
    h.sim.set_status("A", S.OUT_OF_STOCK)
    await h.check(pid)
    h.sim.set_status("A", S.AVAILABLE)
    out = await h.check(pid)
    assert out.verification.outcome.value == "CONFIRMED" and out.confirmed_event_id
    assert len(h.alerts) == 1  # the early alert only; no second ping for the same restock
    assert "Double-checking" in h.alerts[0].extra["Verified"]
    with h.rt.db.session() as s:
        confirmed = s.get(Event, out.confirmed_event_id)
        assert confirmed.notification_sent and confirmed.details["alerted_early"]
    # Later checks and a restart don't re-send it.
    await h.check(pid)
    h.restart()
    await run_once(h.settings, runtime=h.rt)
    assert h.alerts == []


async def test_fast_alert_false_alarm_follow_up(make_harness):
    h = make_harness(fast_alert=True)
    pid = h.add("A")
    h.sim.set_status("A", S.OUT_OF_STOCK)
    await h.check(pid)
    h.sim.queue("A", [S.AVAILABLE])  # flicker; the double-check sees OOS
    out = await h.check(pid)
    assert out.verification.outcome.value == "FALSE_POSITIVE"
    assert len(h.alerts) == 1
    system = [m for m in h.console.sent if m.kind == "system"]
    assert len(system) == 1 and "False alarm" in system[0].title


async def test_fast_alert_off_by_default(harness):
    pid = harness.add("A")
    harness.sim.set_status("A", S.OUT_OF_STOCK)
    await harness.check(pid)
    harness.sim.queue("A", [S.AVAILABLE])
    await harness.check(pid)
    assert harness.alerts == []
    with harness.rt.db.session() as s:
        detected = s.scalar(select(Event).where(Event.event_type == EventType.RESTOCK_DETECTED.value))
        assert not detected.notification_sent


async def test_fast_alert_falls_back_when_early_alert_failed(make_harness):
    h = make_harness(fast_alert=True, restock_reminder_max=0)
    pid = h.add("A")
    h.sim.set_status("A", S.OUT_OF_STOCK)
    await h.check(pid)
    h.sim.set_status("A", S.AVAILABLE)
    real_send = h.rt.notifications.send_early_alert

    async def failing(event_id):
        from app.services.notification_service import DeliveryReport
        return DeliveryReport("x", failed={"console": "down"})

    h.rt.notifications.send_early_alert = failing
    out = await h.check(pid)
    h.rt.notifications.send_early_alert = real_send
    assert out.confirmed_event_id and len(h.alerts) == 1  # the normal confirmed alert
    assert "confirmed" in h.alerts[0].extra["Verified"]
