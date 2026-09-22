"""Evaluation rules: unknown status, errors, third-party sellers, store-level (cases 6-9)."""

import httpx
import pytest
from sqlalchemy import select

from app.models import AvailabilityScope, EventType, Event, InventoryState, InventoryStatus as S, SellerType
from app.retailers.base import Observation, ProductRef
from app.retailers.registry import RetailerManager
from app.retailers.structured_data import normalize_availability, parse_product_page
from app.services.inventory_service import evaluate
from app.utils.rate_limit import HttpStatusError

ONLINE = ProductRef(id=1, retailer="target", sku="12345678", product_name="X")
STORE = ProductRef(id=2, retailer="target", sku="12345678", product_name="X", store_id="1234", city="Athens", state="TN")


def obs(status, scope=AvailabilityScope.ONLINE_AVAILABLE, seller=SellerType.FIRST_PARTY_RETAILER, **kw):
    return Observation(status=status, scope=scope, seller_type=seller, **kw)


# -- pure evaluation ------------------------------------------------------------
def test_unknown_is_never_out_of_stock(settings):
    ev = evaluate(obs(S.UNKNOWN, AvailabilityScope.UNKNOWN), ONLINE, settings)
    assert ev.status == S.UNKNOWN and not ev.alertable and not ev.closes_episode


def test_error_is_never_out_of_stock(settings):
    ev = evaluate(Observation(status=S.ERROR, error="boom"), ONLINE, settings)
    assert ev.status == S.ERROR and not ev.alertable and not ev.closes_episode


def test_third_party_filtered_by_default(settings):
    ev = evaluate(obs(S.AVAILABLE, seller=SellerType.THIRD_PARTY, seller_name="Cards4U"), ONLINE, settings)
    assert not ev.alertable and ev.ignored_reason == "THIRD_PARTY" and ev.closes_episode


def test_third_party_accepted_when_configured(settings):
    s = settings.model_copy(update={"accept_third_party": True})
    assert evaluate(obs(S.AVAILABLE, seller=SellerType.THIRD_PARTY), ONLINE, s).alertable


def test_per_product_third_party_override(settings):
    p = ProductRef(id=1, retailer="target", sku="1", product_name="X", accept_third_party=True)
    assert evaluate(obs(S.AVAILABLE, seller=SellerType.THIRD_PARTY), p, settings).alertable


def test_online_availability_is_not_store_availability(settings):
    ev = evaluate(obs(S.AVAILABLE, AvailabilityScope.ONLINE_AVAILABLE), STORE, settings)
    assert ev.status == S.UNKNOWN and not ev.alertable and ev.ignored_reason == "STORE_DATA_UNAVAILABLE"


def test_store_available_is_alertable(settings):
    assert evaluate(obs(S.AVAILABLE, AvailabilityScope.STORE_AVAILABLE), STORE, settings).alertable


def test_store_unavailable(settings):
    ev = evaluate(obs(S.OUT_OF_STOCK, AvailabilityScope.STORE_UNAVAILABLE), STORE, settings)
    assert ev.status == S.OUT_OF_STOCK and not ev.alertable and ev.closes_episode


def test_preorder_and_limited_flags(settings):
    assert evaluate(obs(S.LIMITED), ONLINE, settings).alertable
    assert not evaluate(obs(S.PREORDER), ONLINE, settings).alertable
    s = settings.model_copy(update={"alert_on_preorder": True, "alert_on_limited": False})
    assert evaluate(obs(S.PREORDER), ONLINE, s).alertable
    assert not evaluate(obs(S.LIMITED), ONLINE, s).alertable


# -- through the monitor -------------------------------------------------------------
async def test_http_error_keeps_previous_status(harness):
    pid = harness.add("A")
    harness.sim.set_status("A", S.OUT_OF_STOCK)
    await harness.check(pid)
    harness.sim.queue("A", [HttpStatusError("HTTP 500", 500), HttpStatusError("HTTP 500", 500)])
    await harness.check(pid)
    await harness.check(pid)
    with harness.rt.db.session() as s:
        st = s.scalar(select(InventoryState).where(InventoryState.product_id == pid))
        errors = list(s.scalars(select(Event).where(Event.event_type == EventType.CHECK_ERROR.value)))
    assert st.status == "OUT_OF_STOCK" and st.consecutive_errors == 2 and st.poll_mode == "ERROR"
    assert len(errors) == 1  # one event per error streak, not per failed poll
    harness.sim.set_status("A", S.OUT_OF_STOCK)
    await harness.check(pid)
    with harness.rt.db.session() as s:
        st = s.scalar(select(InventoryState).where(InventoryState.product_id == pid))
    assert st.consecutive_errors == 0 and st.poll_mode == "NORMAL"


async def test_error_during_open_episode_does_not_close_it(harness):
    pid = harness.add("A")
    for status in (S.OUT_OF_STOCK, S.AVAILABLE):
        harness.sim.set_status("A", status)
        await harness.check(pid)
    harness.sim.queue("A", [HttpStatusError("HTTP 500", 500), Observation(status=S.UNKNOWN)])
    await harness.check(pid)
    await harness.check(pid)
    await harness.check(pid)  # AVAILABLE again
    assert len(harness.alerts) == 1


async def test_unknown_status_then_available(harness):
    pid = harness.add("A")
    harness.sim.set_status("A", S.OUT_OF_STOCK)
    await harness.check(pid)
    harness.sim.queue("A", [Observation(status=S.UNKNOWN, message="no markup")])
    await harness.check(pid)
    assert harness.alerts == []


async def test_third_party_seller_does_not_alert(harness):
    pid = harness.add("A")
    harness.sim.set_status("A", S.OUT_OF_STOCK)
    await harness.check(pid)
    harness.sim.set_status("A", S.AVAILABLE, seller_type=SellerType.THIRD_PARTY)
    for _ in range(3):
        await harness.check(pid)
    assert harness.alerts == []
    with harness.rt.db.session() as s:
        n = len(list(s.scalars(select(Event).where(Event.event_type == EventType.THIRD_PARTY_IGNORED.value))))
    assert n == 1
    # first-party stock appears -> alert
    harness.sim.set_status("A", S.AVAILABLE, seller_type=SellerType.FIRST_PARTY_RETAILER)
    out = await harness.check(pid)
    assert out.detection.kind == "THIRD_PARTY_TO_FIRST_PARTY"
    assert len(harness.alerts) == 1


async def test_store_unavailable_while_other_store_available(harness):
    a = harness.add("T", store_id="S1", store_name="Athens", city="Athens", state="TN")
    c = harness.add("T", store_id="S2", store_name="Cleveland", city="Cleveland", state="TN")
    harness.sim.set_status("T", S.OUT_OF_STOCK, store_id="S1")
    harness.sim.set_status("T", S.OUT_OF_STOCK, store_id="S2")
    await harness.check(a)
    await harness.check(c)
    harness.sim.set_status("T", S.AVAILABLE, store_id="S1")
    await harness.check(a)
    await harness.check(c)
    assert len(harness.alerts) == 1
    assert harness.alerts[0].store.startswith("Athens")


async def test_target_store_level_reports_unknown(settings):
    mgr = RetailerManager(settings)
    target = mgr.get("target")
    o = await target.check(STORE)
    assert o.status == S.UNKNOWN and o.scope == AvailabilityScope.UNKNOWN
    assert "not available" in o.message
    await mgr.aclose()


# -- structured data parsing (mocked HTML, no live requests) ---------------------------
JSONLD = """<html><head><script type="application/ld+json">
{"@context":"https://schema.org","@type":"Product","name":"Pokemon ETB","image":"https://img/x.jpg",
 "offers":{"@type":"Offer","price":"49.99","availability":"https://schema.org/%s",
 "seller":{"@type":"Organization","name":"%s"}}}
</script></head><body></body></html>"""


@pytest.mark.parametrize("avail,expected", [
    ("InStock", S.AVAILABLE), ("OutOfStock", S.OUT_OF_STOCK), ("LimitedAvailability", S.LIMITED),
    ("PreOrder", S.PREORDER), ("SoldOut", S.OUT_OF_STOCK), ("Discontinued", S.UNAVAILABLE),
])
def test_parse_json_ld(avail, expected):
    parsed = parse_product_page(JSONLD % (avail, "Target"))
    assert parsed.found and parsed.offers[0].status == expected
    assert parsed.offers[0].seller_name == "Target" and parsed.offers[0].price == 49.99


def test_parse_microdata_and_meta():
    html = '<div itemscope><link itemprop="availability" href="http://schema.org/InStock"></div>'
    assert parse_product_page(html).offers[0].status == S.AVAILABLE
    html = '<meta property="product:availability" content="out of stock">'
    assert parse_product_page(html).offers[0].status == S.OUT_OF_STOCK


def test_parse_no_markup_is_not_found():
    assert not parse_product_page("<html><body>hello</body></html>").found
    assert normalize_availability(None) == S.UNKNOWN
    assert normalize_availability("weird") == S.UNKNOWN


def _target_transport(page_html, robots="User-agent: *\nAllow: /\n", status=200):
    def handler(request: httpx.Request):
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=robots)
        assert "PokemonRestockMonitor" in request.headers["user-agent"]  # honest UA
        return httpx.Response(status, text=page_html)
    return httpx.MockTransport(handler)


async def test_target_parses_first_party_availability(settings):
    mgr = RetailerManager(settings, transport=_target_transport(JSONLD % ("InStock", "Target")))
    o = await mgr.get("target").safe_check(ONLINE)
    assert o.status == S.AVAILABLE and o.scope == AvailabilityScope.ONLINE_AVAILABLE
    assert o.seller_type == SellerType.FIRST_PARTY_RETAILER and o.price == 49.99
    await mgr.aclose()


async def test_target_marketplace_seller_is_third_party(settings):
    mgr = RetailerManager(settings, transport=_target_transport(JSONLD % ("InStock", "Some Card Shop LLC")))
    o = await mgr.get("target").safe_check(ONLINE)
    assert o.seller_type == SellerType.THIRD_PARTY
    assert not evaluate(o, ONLINE, settings).alertable
    await mgr.aclose()


async def test_target_missing_markup_is_unknown(settings):
    mgr = RetailerManager(settings, transport=_target_transport("<html>app shell</html>"))
    o = await mgr.get("target").safe_check(ONLINE)
    assert o.status == S.UNKNOWN and "TARGET_USE_BROWSER" in o.message
    await mgr.aclose()


async def test_target_url_from_tcin(settings):
    mgr = RetailerManager(settings)
    assert mgr.get("target").get_product_url(ONLINE) == "https://www.target.com/p/-/A-12345678"
    await mgr.aclose()


async def test_robots_disallow_is_respected(settings):
    calls = []

    def handler(request):
        calls.append(request.url.path)
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /p/\n")
        return httpx.Response(200, text=JSONLD % ("InStock", "Target"))

    mgr = RetailerManager(settings, transport=httpx.MockTransport(handler))
    o = await mgr.get("target").safe_check(ONLINE)
    assert o.status == S.ERROR and o.error_kind == "NOT_PERMITTED"
    assert calls == ["/robots.txt"]  # the product page was never requested
    await mgr.aclose()
