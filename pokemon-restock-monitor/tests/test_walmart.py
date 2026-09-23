"""Walmart page reading: only offers sold by Walmart itself count."""

import json

import httpx

from app.models import InventoryStatus as S, SellerType
from app.retailers.base import ProductRef
from app.retailers.registry import RetailerManager
from app.retailers.walmart import WALMART_SELLER_ID, page_upc, parse_walmart_page

ITEM = ProductRef(id=1, retailer="walmart", sku="20640569221", product_name="ETB")


def offer(status="IN_STOCK", seller="OPLoot LLC", seller_type="EXTERNAL", seller_id="7CAE25F8", price=48.95):
    return {"availabilityStatus": status, "sellerName": seller, "sellerDisplayName": seller,
            "sellerType": seller_type, "sellerId": seller_id,
            "priceInfo": {"currentPrice": {"price": price, "priceString": f"${price}"}}}


def walmart_offer(status="IN_STOCK", price=49.99):
    return offer(status, "Walmart.com", "INTERNAL", WALMART_SELLER_ID, price)


def page(buy_box: dict, secondary: list[dict] | None = None, upc="196214158801") -> str:
    product = dict(buy_box, usItemId="20640569221", upc=upc, secondaryOffers=secondary or [])
    data = {"props": {"pageProps": {"initialData": {"data": {"product": product}}}}}
    return (f'<html><head><script type="application/ld+json">{{"@type":"WebPage"}}</script></head><body>'
            f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(data)}</script></body></html>')


def test_walmart_sold_in_stock():
    o = parse_walmart_page(page(walmart_offer()))
    assert o.status == S.AVAILABLE and o.seller_name == "Walmart.com" and o.price == 49.99


def test_walmart_sold_out_of_stock():
    assert parse_walmart_page(page(walmart_offer("OUT_OF_STOCK"))).status == S.OUT_OF_STOCK


def test_marketplace_only_is_third_party():
    o = parse_walmart_page(page(offer(), [offer(seller="Florida Box Breakers")]))
    assert o.status == S.AVAILABLE and o.seller_name == "OPLoot LLC (Marketplace)"


def test_walmart_offer_behind_marketplace_buy_box_is_found():
    o = parse_walmart_page(page(offer(), [walmart_offer()]))
    assert o.seller_name == "Walmart.com" and o.status == S.AVAILABLE


def test_walmart_out_of_stock_while_marketplace_in_stock_is_out_of_stock():
    o = parse_walmart_page(page(offer(), [walmart_offer("OUT_OF_STOCK")]))
    assert o.seller_name == "Walmart.com" and o.status == S.OUT_OF_STOCK


def test_external_seller_named_walmart_is_not_walmart():
    o = parse_walmart_page(page(offer(seller="Walmart", seller_type="EXTERNAL")))
    assert o.seller_name == "Walmart (Marketplace)"


def test_no_product_data_returns_none():
    assert parse_walmart_page("<html><body>nothing</body></html>") is None
    assert page_upc(page(walmart_offer())) == "196214158801"


def _manager(settings, html: str, final_path: str = "/ip/20640569221"):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /account/\n")
        if final_path != request.url.path:
            return httpx.Response(302, headers={"Location": f"https://www.walmart.com{final_path}"})
        return httpx.Response(200, text=html)

    s = settings.model_copy(update={"min_request_interval_seconds": 0, "max_retries": 0})
    return RetailerManager(s, transport=httpx.MockTransport(handler))


async def test_monitor_reports_walmart_sold(settings):
    mgr = _manager(settings, page(offer(), [walmart_offer()]))
    obs = await mgr.get("walmart").check(ITEM)
    await mgr.aclose()
    assert obs.status == S.AVAILABLE and obs.seller_type == SellerType.FIRST_PARTY_RETAILER
    assert obs.source == "walmart:product-page:next-data"


async def test_monitor_marketplace_is_third_party(settings):
    mgr = _manager(settings, page(offer()))
    obs = await mgr.get("walmart").check(ITEM)
    await mgr.aclose()
    assert obs.seller_type == SellerType.THIRD_PARTY


async def test_blocked_redirect_pauses_walmart(settings):
    mgr = _manager(settings, "<html>Robot or human?</html>", final_path="/blocked")
    walmart = mgr.get("walmart")
    obs = await walmart.safe_check(ITEM)
    await mgr.aclose()
    assert obs.status == S.ERROR and walmart.limiter.is_paused()


async def test_http_412_is_a_block_not_retried(settings):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /account/\n")
        calls.append(request.url.path)
        return httpx.Response(412, text="")

    s = settings.model_copy(update={"min_request_interval_seconds": 0})
    mgr = RetailerManager(s, transport=httpx.MockTransport(handler))
    walmart = mgr.get("walmart")
    obs = await walmart.safe_check(ITEM)
    await mgr.aclose()
    assert obs.status == S.ERROR and walmart.limiter.is_paused() and len(calls) == 1
