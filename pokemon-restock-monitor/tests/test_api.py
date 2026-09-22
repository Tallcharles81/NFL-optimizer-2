"""Dashboard, API, health, auth -- and a guard that no purchasing code exists."""

import re
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import create_app
from app.models import InventoryStatus as S


def client_for(harness, **overrides):
    if overrides:
        harness.rt.settings = harness.settings = harness.settings.model_copy(update=overrides)
    return TestClient(create_app(harness.settings, runtime=harness.rt))


async def test_pages_render_with_data(harness):
    pid = harness.add("A", product_name="Pokémon Test Box")
    for status in (S.OUT_OF_STOCK, S.AVAILABLE):
        harness.sim.set_status("A", status)
        await harness.check(pid)
    with client_for(harness) as c:
        for path in ("/dashboard", "/products", "/retailers", "/events", "/settings",
                     "/events?type=RESTOCK_CONFIRMED"):
            r = c.get(path)
            assert r.status_code == 200, path
        dash = c.get("/dashboard").text
        assert "Pokémon Test Box" in dash and "Recent restocks" in dash and "Alert speed" in dash
        assert c.get("/api/metrics").json()["latency"]["samples"] == 1
        events = c.get("/api/events?type=RESTOCK_CONFIRMED").json()
        assert len(events) == 1 and events[0]["notification_sent"] is True


def test_health_reports_components(harness):
    with client_for(harness) as c:
        body = c.get("/health").json()
    for key in ("status", "database_status", "scheduler_status", "retailer_status",
                "last_successful_check", "last_notification"):
        assert key in body
    assert body["database_status"] == "ok" and body["scheduler_status"] == "disabled"


def test_health_is_down_when_scheduler_not_running(harness):
    harness.settings = harness.rt.settings = harness.settings.model_copy(update={"monitor_enabled": True})
    app = create_app(harness.settings, runtime=harness.rt)
    c = TestClient(app)  # no lifespan -> scheduler never started
    r = c.get("/health")
    assert r.status_code == 503 and r.json()["status"] == "down"
    assert "scheduler not running" in r.json()["problems"]


def test_product_crud_api(harness):
    with client_for(harness) as c:
        r = c.post("/api/products", json={"retailer": "target", "sku": "12345678",
                                          "product_name": "Pokémon ETB", "store_id": "1234",
                                          "store_name": "Athens Target", "city": "Athens", "state": "TN"})
        assert r.status_code == 201
        body = r.json()
        assert body["product"]["store_id"] == "1234"
        assert any("store-level" in w for w in body["warnings"])
        pid = body["product"]["id"]
        assert c.post("/api/products", json={"retailer": "target", "sku": "12345678",
                                             "product_name": "dup", "store_id": "1234"}).status_code == 409
        assert c.patch(f"/api/products/{pid}", json={"enabled": False}).json()["enabled"] is False
        assert c.get("/api/stores").json()[0]["store_id"] == "1234"
        assert c.delete(f"/api/products/{pid}").status_code == 204
        assert c.post("/api/products", json={"retailer": "nope", "sku": "1", "product_name": "x"}).status_code == 422


def test_form_add_product(harness):
    with client_for(harness) as c:
        r = c.post("/products", data={"retailer": "target", "sku": "87654321", "product_name": "Form product",
                                      "product_url": "https://www.target.com/p/-/A-87654321", "enabled": "on"},
                   follow_redirects=False)
        assert r.status_code == 303 and "Product+added" in r.headers["location"]
        assert "Form product" in c.get("/products").text


def test_dashboard_password(harness):
    with client_for(harness, dashboard_password="s3cret") as c:
        assert c.get("/dashboard").status_code == 401
        assert c.get("/api/products").status_code == 401
        assert c.get("/dashboard", auth=("admin", "s3cret")).status_code == 200
        assert c.get("/health").status_code == 200  # health stays open for Docker healthchecks


def test_test_notification_button(harness):
    with client_for(harness) as c:
        r = c.post("/settings/test-notification", follow_redirects=False)
        assert r.status_code == 303 and "console" in r.headers["location"]
    assert harness.console.sent[-1].kind == "test"


def test_no_checkout_or_payment_functionality():
    """The system must only MONITOR -> VERIFY -> ALERT. Nothing may buy."""
    root = Path(__file__).resolve().parents[1] / "app"
    forbidden = re.compile(r"add[_-]?to[_-]?cart|\bcheckout\b|place[_-]?order|payment|credit[_-]?card|"
                           r"card[_-]?number|\bcvv\b|submit[_-]?order|password\s*=\s*['\"]", re.I)
    allowed = {"settings.html"}  # contains the human-readable "no checkout" guarantee
    # The Target parser *reads* whether the Add to cart button is enabled to learn the stock
    # status. Those read-only references are allowed there -- nothing else is.
    read_only_ok = re.compile(r"add[_-]?to[_-]?cart", re.I)
    hits = []
    for f in root.rglob("*"):
        if f.suffix not in {".py", ".html"} or f.name in allowed:
            continue
        rel = str(f.relative_to(root))
        for n, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            match = forbidden.search(line)
            if not match:
                continue
            if rel == "retailers/target.py" and all(read_only_ok.fullmatch(m.group(0))
                                                     for m in forbidden.finditer(line)):
                continue
            hits.append(f"{rel}:{n}: {line.strip()}")
    assert hits == [], "\n".join(hits)


def test_browser_never_interacts_with_pages():
    """The optional browser only loads and reads pages: no clicks, typing or form submits."""
    root = Path(__file__).resolve().parents[1] / "app"
    interaction = re.compile(r"\.(click|dblclick|fill|type|press|check|tap|select_option|set_input_files|"
                             r"dispatch_event|submit)\(|\.evaluate\(", re.I)
    hits = [f"{f.relative_to(root)}:{n}: {line.strip()}"
            for f in root.rglob("*.py") if "playwright" in f.read_text(encoding="utf-8")
            for n, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1)
            if interaction.search(line)]
    assert hits == [], "\n".join(hits)
    assert any("playwright" in f.read_text(encoding="utf-8") for f in root.rglob("*.py"))
