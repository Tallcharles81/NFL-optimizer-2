"""Status transitions and restock episodes (cases 1, 2, 3)."""

from sqlalchemy import func, select

from app.models import Event, EventType, InventoryState, InventoryStatus as S


def count_events(h, etype, pid=None):
    with h.rt.db.session() as s:
        q = select(func.count(Event.id)).where(Event.event_type == etype.value)
        if pid:
            q = q.where(Event.product_id == pid)
        return s.scalar(q)


def state(h, pid) -> InventoryState:
    with h.rt.db.session() as s:
        return s.scalar(select(InventoryState).where(InventoryState.product_id == pid))


async def test_out_of_stock_to_available_alerts(harness):
    pid = harness.add("A")
    harness.sim.set_status("A", S.OUT_OF_STOCK)
    await harness.check(pid)
    harness.sim.set_status("A", S.AVAILABLE)
    out = await harness.check(pid)

    assert out.detection.kind == "OUT_OF_STOCK_TO_AVAILABLE"
    assert out.verification.outcome.value == "CONFIRMED"
    assert count_events(harness, EventType.RESTOCK_DETECTED, pid) == 1
    assert count_events(harness, EventType.RESTOCK_CONFIRMED, pid) == 1
    assert len(harness.alerts) == 1
    alert = harness.alerts[0]
    assert alert.title == "🚨 POKÉMON RESTOCK DETECTED"
    assert alert.status == "AVAILABLE" and alert.sku == "A"
    assert alert.url == "https://example.com/simulated/A"
    st = state(harness, pid)
    assert st.status == "AVAILABLE" and st.episode_open and st.alert_state == "CONFIRMED"


async def test_available_to_out_of_stock_closes_episode(harness):
    pid = harness.add("A")
    harness.sim.set_status("A", S.OUT_OF_STOCK)
    await harness.check(pid)
    harness.sim.set_status("A", S.AVAILABLE)
    await harness.check(pid)
    harness.sim.set_status("A", S.OUT_OF_STOCK)
    out = await harness.check(pid)

    assert out.detection is None
    assert count_events(harness, EventType.SOLD_OUT, pid) == 1
    st = state(harness, pid)
    assert st.status == "OUT_OF_STOCK" and not st.episode_open
    assert len(harness.alerts) == 1  # a sell-out is not a restock alert


async def test_available_to_available_does_not_realert(harness):
    pid = harness.add("A")
    harness.sim.set_status("A", S.OUT_OF_STOCK)
    await harness.check(pid)
    harness.sim.set_status("A", S.AVAILABLE)
    for _ in range(10):
        await harness.check(pid)
    assert len(harness.alerts) == 1
    assert count_events(harness, EventType.RESTOCK_DETECTED, pid) == 1


async def test_second_restock_after_sellout_alerts_again(harness):
    pid = harness.add("A")
    for status in (S.OUT_OF_STOCK, S.AVAILABLE, S.AVAILABLE, S.OUT_OF_STOCK, S.AVAILABLE, S.AVAILABLE):
        harness.sim.set_status("A", status)
        await harness.check(pid)
    assert len(harness.alerts) == 2
    with harness.rt.db.session() as s:
        keys = list(s.scalars(select(Event.dedupe_key).where(
            Event.event_type == EventType.RESTOCK_CONFIRMED.value)))
    assert keys == [f"restock:{pid}:online:ep1", f"restock:{pid}:online:ep2"]


async def test_limited_to_available_is_marked(make_harness):
    h = make_harness(alert_on_limited=False)
    pid = h.add("A")
    h.sim.set_status("A", S.LIMITED)
    await h.check(pid)
    assert h.alerts == []
    h.sim.set_status("A", S.AVAILABLE)
    out = await h.check(pid)
    assert out.detection.kind == "LIMITED_TO_AVAILABLE"
    assert len(h.alerts) == 1
    assert "was limited" in h.alerts[0].extra["Change"]


async def test_limited_then_available_within_open_episode_is_one_alert(harness):
    pid = harness.add("A")
    for status in (S.OUT_OF_STOCK, S.LIMITED, S.AVAILABLE):
        harness.sim.set_status("A", status)
        await harness.check(pid)
    assert len(harness.alerts) == 1
    with harness.rt.db.session() as s:
        transitions = list(s.scalars(select(Event.transition).where(
            Event.event_type == EventType.STATUS_CHANGE.value, Event.product_id == pid)))
    assert "LIMITED_TO_AVAILABLE" in transitions


async def test_unknown_to_available_is_marked(harness):
    pid = harness.add("A")
    harness.sim.set_status("A", S.AVAILABLE)
    out = await harness.check(pid)
    assert out.detection.kind == "UNKNOWN_TO_AVAILABLE"
    assert "previous status unknown" in harness.alerts[0].extra["Change"]


async def test_unknown_to_available_can_be_suppressed(make_harness):
    h = make_harness(alert_on_unknown_to_available=False)
    pid = h.add("A")
    h.sim.set_status("A", S.AVAILABLE)
    await h.check(pid)
    assert h.alerts == []
    # ... but a real restock later still alerts
    h.sim.set_status("A", S.OUT_OF_STOCK)
    await h.check(pid)
    h.sim.set_status("A", S.AVAILABLE)
    await h.check(pid)
    assert len(h.alerts) == 1


async def test_detection_latency_metrics_recorded(harness):
    pid = harness.add("A")
    harness.sim.set_status("A", S.OUT_OF_STOCK)
    await harness.check(pid)
    harness.sim.set_status("A", S.AVAILABLE)
    await harness.check(pid)
    with harness.rt.db.session() as s:
        ev = s.scalar(select(Event).where(Event.event_type == EventType.RESTOCK_CONFIRMED.value))
    assert ev.inventory_changed_at is not None
    for field in ("detection_latency_ms", "detection_window_ms", "verification_latency_ms",
                  "notification_latency_ms", "total_latency_ms"):
        assert getattr(ev, field) is not None and getattr(ev, field) >= 0, field
    assert ev.notification_sent and ev.notified_at is not None
