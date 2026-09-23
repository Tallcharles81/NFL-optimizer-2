"""One-shot (GitHub Actions) mode and the ntfy phone-push channel."""

import json
from pathlib import Path

import httpx
from sqlalchemy import func, select

from app.models import InventoryStatus as S, Notification, Product
from app.notifications.base import AlertMessage
from app.notifications.ntfy import NtfyNotifier
from app.oneshot import run_once

CATALOG = Path(__file__).resolve().parents[1] / "catalog" / "target_30th_celebration.csv"


def sent(h, kind="RESTOCK_CONFIRMED"):
    with h.rt.db.session() as s:
        return s.scalar(select(func.count(Notification.id)).where(
            Notification.status == "SENT", Notification.event_type == kind))


async def test_runs_dedupe_across_restarts(harness):
    harness.add("A")
    harness.sim.set_status("A", S.OUT_OF_STOCK)
    await run_once(harness.settings, runtime=harness.rt)
    harness.sim.set_status("A", S.AVAILABLE)
    for _ in range(4):  # four scheduled runs, each a fresh process
        harness.restart()
        summary = await run_once(harness.settings, runtime=harness.rt)
        assert summary["checked"] == 1
    assert sent(harness) == 1
    harness.sim.set_status("A", S.OUT_OF_STOCK)
    harness.restart()
    await run_once(harness.settings, runtime=harness.rt)
    harness.sim.set_status("A", S.AVAILABLE)
    harness.restart()
    await run_once(harness.settings, runtime=harness.rt)
    assert sent(harness) == 2


async def test_unreadable_retailer_alerts_once_per_day(harness):
    harness.add("A")
    harness.add("B")
    for _ in range(3):
        await run_once(harness.settings, runtime=harness.rt)  # simulated status defaults to UNKNOWN
    system = [m for m in harness.console.sent if m.kind == "system"]
    assert len(system) == 1 and "can't read" in system[0].title


async def test_catalog_import_is_idempotent(harness):
    from app.services.product_service import import_catalog

    with harness.rt.db.session() as s:
        first = import_catalog(s, str(CATALOG))
        second = import_catalog(s, str(CATALOG))
    assert len(first["added"]) == 9 and first["errors"] == []
    assert second["added"] == [] and second["existing"] == 9
    with harness.rt.db.session() as s:
        etb = s.scalar(select(Product).where(Product.sku == "1010892076"))
    assert etb.dpci == "361-00-8095" and etb.upc == "196214158801"


async def test_ntfy_payload():
    captured = []
    n = NtfyNotifier("my-secret-topic", transport=httpx.MockTransport(
        lambda r: captured.append((str(r.url), json.loads(r.content))) or httpx.Response(200)))
    await n.send(AlertMessage(kind="restock", title="🚨 POKÉMON RESTOCK DETECTED",
                              product_name="30th Celebration ETB", retailer="Target", status="AVAILABLE",
                              sku="1010892076", url="https://www.target.com/p/-/A-1010892076",
                              extra={"DPCI (in store)": "361-00-8095"}))
    url, body = captured[0]
    assert url == "https://ntfy.sh"
    assert body["topic"] == "my-secret-topic" and body["priority"] == 5
    assert body["click"] == "https://www.target.com/p/-/A-1010892076"
    assert body["actions"][0]["label"] == "OPEN PRODUCT"
    assert "DPCI (in store): 361-00-8095" in body["message"]
    await n.aclose()


async def test_loop_mode_sweeps_repeatedly_and_alerts_once(harness):
    harness.add("A")
    harness.sim.set_status("A", S.OUT_OF_STOCK)
    await run_once(harness.settings, runtime=harness.rt)
    harness.sim.set_status("A", S.AVAILABLE)
    summary = await run_once(harness.settings, runtime=harness.rt, loop_minutes=0.02, sweep_seconds=0.1)
    assert summary["sweeps"] >= 3
    assert sent(harness) == 1  # detected on the first sweep, not repeated on later sweeps


async def test_loop_mode_catches_restock_mid_run(harness):
    import asyncio

    harness.add("A")
    harness.sim.set_status("A", S.OUT_OF_STOCK)

    async def restock_soon():
        await asyncio.sleep(0.3)
        harness.sim.set_status("A", S.AVAILABLE)

    task = asyncio.create_task(restock_soon())
    summary = await run_once(harness.settings, runtime=harness.rt, loop_minutes=0.02, sweep_seconds=0.1)
    await task
    assert summary["restocks"] == 1 and sent(harness) == 1


async def test_concurrent_sweep_checks_all_and_alerts_once_each(harness):
    for sku in ("A", "B", "C", "D"):
        harness.add(sku)
        harness.sim.set_status(sku, S.OUT_OF_STOCK)
    await run_once(harness.settings, runtime=harness.rt, concurrency=3)
    for sku in ("A", "C"):
        harness.sim.set_status(sku, S.AVAILABLE)
    summary = await run_once(harness.settings, runtime=harness.rt, concurrency=3)
    assert summary["checked"] == 4 and summary["restocks"] == 2
    await run_once(harness.settings, runtime=harness.rt, concurrency=3)
    assert sent(harness) == 2


async def test_checks_per_sweep_rotates_least_recently_checked(make_harness):
    from app.models import InventoryState

    h = make_harness(checks_per_sweep="simulated=1")
    ids = [h.add(sku) for sku in ("A", "B", "C")]
    for sku in ("A", "B", "C"):
        h.sim.set_status(sku, S.OUT_OF_STOCK)

    def checked():
        with h.rt.db.session() as s:
            return {st.product_id for st in s.scalars(select(InventoryState))
                    if st.last_checked_at is not None}

    summary = await run_once(h.settings, runtime=h.rt)
    assert summary["checked"] == 1 and summary["skipped"] == 2 and len(checked()) == 1
    await run_once(h.settings, runtime=h.rt)
    await run_once(h.settings, runtime=h.rt)
    assert checked() == set(ids)


def test_checks_per_sweep_setting(settings):
    assert settings.checks_per_sweep_limits() == {"walmart": 1}
    assert settings.model_copy(update={"checks_per_sweep": ""}).checks_per_sweep_limits() == {}


async def test_walmart_catalog_imports(harness):
    from app.services.product_service import import_catalog

    with harness.rt.db.session() as s:
        result = import_catalog(s, str(CATALOG.parent / "walmart_30th_celebration.csv"))
        assert result["errors"] == [] and len(result["added"]) == 5
        assert {p.retailer.slug for p in result["added"]} == {"walmart"}
