"""Repeat alerts while an item stays in stock."""

from datetime import timedelta

from sqlalchemy import select

from app.models import Event, EventType, InventoryState, InventoryStatus as S, SellerType
from app.oneshot import run_once


def reminders(h):
    return [m for m in h.alerts if m.title.startswith("🔁")]


async def restock(h, sku="A"):
    pid = h.add(sku)
    h.sim.set_status(sku, S.OUT_OF_STOCK)
    await h.check(pid)
    h.sim.set_status(sku, S.AVAILABLE)
    await h.check(pid)
    return pid


async def test_reminders_repeat_up_to_the_limit(make_harness):
    h = make_harness(restock_reminder_minutes=0, restock_reminder_max=3)
    pid = await restock(h)
    for _ in range(6):
        await h.check(pid)
    assert len(h.alerts) == 1 + 3  # the restock alert plus 3 reminders, then silence
    titles = [m.title for m in reminders(h)]
    assert titles == [f"🔁 STILL IN STOCK (reminder {n} of 3)" for n in (1, 2, 3)]
    assert reminders(h)[0].url == "https://example.com/simulated/A"


async def test_reminders_wait_for_the_interval(make_harness):
    h = make_harness(restock_reminder_minutes=10, restock_reminder_max=6)
    pid = await restock(h)
    for _ in range(3):
        await h.check(pid)  # immediately after the alert: too soon
    assert reminders(h) == []
    with h.rt.db.session() as s:  # pretend 11 minutes passed
        st = s.scalar(select(InventoryState).where(InventoryState.product_id == pid))
        st.last_alert_at = st.last_alert_at - timedelta(minutes=11)
    await h.check(pid)
    assert len(reminders(h)) == 1


async def test_reminders_stop_when_sold_out_and_restart_on_next_restock(make_harness):
    h = make_harness(restock_reminder_minutes=0, restock_reminder_max=2)
    pid = await restock(h)
    await h.check(pid)
    h.sim.set_status("A", S.OUT_OF_STOCK)
    for _ in range(3):
        await h.check(pid)
    assert len(reminders(h)) == 1
    h.sim.set_status("A", S.AVAILABLE)
    await h.check(pid)  # new restock alert
    await h.check(pid)
    await h.check(pid)
    await h.check(pid)
    assert len([m for m in h.alerts if m.title.startswith("🚨")]) == 2
    assert len(reminders(h)) == 1 + 2


async def test_no_reminders_for_third_party_listing(make_harness):
    h = make_harness(restock_reminder_minutes=0, restock_reminder_max=5)
    pid = await restock(h)
    h.sim.set_status("A", S.AVAILABLE, seller_type=SellerType.THIRD_PARTY)
    for _ in range(3):
        await h.check(pid)
    assert reminders(h) == []


async def test_reminders_disabled(make_harness):
    h = make_harness(restock_reminder_minutes=0, restock_reminder_max=0)
    pid = await restock(h)
    for _ in range(3):
        await h.check(pid)
    assert len(h.alerts) == 1


async def test_reminders_across_github_runs_never_duplicate(make_harness):
    h = make_harness(restock_reminder_minutes=0, restock_reminder_max=2)
    h.add("A")
    h.sim.set_status("A", S.OUT_OF_STOCK)
    await run_once(h.settings, runtime=h.rt)
    h.sim.set_status("A", S.AVAILABLE)
    total = 0
    for _ in range(5):  # five scheduled runs, each a fresh process
        h.restart()
        await run_once(h.settings, runtime=h.rt)
        total += len(h.alerts)
    assert total == 1 + 2
    with h.rt.db.session() as s:
        keys = sorted(s.scalars(select(Event.dedupe_key).where(
            Event.event_type == EventType.RESTOCK_REMINDER.value)))
    assert [k.rsplit(":", 1)[1] for k in keys] == ["reminder1", "reminder2"]


async def test_sold_out_notice_after_an_alerted_restock(make_harness):
    h = make_harness(restock_reminder_minutes=0, restock_reminder_max=0)
    pid = await restock(h)
    h.sim.set_status("A", S.OUT_OF_STOCK)
    for _ in range(3):
        await h.check(pid)
    sold = [m for m in h.console.sent if m.kind == "soldout"]
    assert len(sold) == 1 and sold[0].title == "Sold out: Product A"
    assert sold[0].text.startswith("In stock for about ")


async def test_no_sold_out_notice_without_a_restock_alert(make_harness):
    h = make_harness()
    pid = h.add("A")
    h.sim.set_status("A", S.OUT_OF_STOCK)
    for _ in range(3):
        await h.check(pid)
    assert h.console.sent == []


def test_format_duration():
    from app.services.notification_service import format_duration
    assert format_duration(9.6) == "10s" and format_duration(82) == "1m 22s"
