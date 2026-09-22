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
