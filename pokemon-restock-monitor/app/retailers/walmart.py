"""Walmart.

Data source: the public product page ``https://www.walmart.com/ip/<item id>``
(robots.txt allows ``/ip/``), fetched with plain HTTP -- no browser needed. The
page is built from a ``__NEXT_DATA__`` JSON block that lists the buy-box offer
and any other sellers' offers, each with its seller and availability. Walmart
publishes no schema.org availability.

Seller: Walmart item pages are shared by Walmart and Marketplace sellers.
Only an offer sold by Walmart itself counts as first party. If Walmart has an
offer on the page it is reported (in stock or not), even when a Marketplace
seller has the buy box; otherwise the buy-box offer is reported as
THIRD_PARTY, which never alerts with ACCEPT_THIRD_PARTY=false.

If Walmart serves a bot challenge or a /blocked page, the monitor pauses
Walmart -- it never tries to get past it. Store-level inventory is not
implemented (returns UNKNOWN).
"""

from __future__ import annotations

import json
import re

from bs4 import BeautifulSoup

from app.models.enums import InventoryStatus
from app.retailers.base import ProductRef
from app.retailers.structured_data import ParsedOffer, StructuredDataRetailerMonitor

ITEM_ID_RE = re.compile(r"^\d{5,14}$")
ITEM_ID_IN_URL_RE = re.compile(r"/ip/(?:[^/?#]+/)?(\d{5,14})")

# Walmart's own seller account on walmart.com.
WALMART_SELLER_ID = "F55CDC31AB754BB68FE0B39041159D63"
WALMART_SELLER_NAMES = ("walmart", "walmart.com", "walmart inc.")

_STATUS = {
    "IN_STOCK": InventoryStatus.AVAILABLE,
    "LIMITED_STOCK": InventoryStatus.LIMITED,
    "OUT_OF_STOCK": InventoryStatus.OUT_OF_STOCK,
    "NOT_AVAILABLE": InventoryStatus.OUT_OF_STOCK,
    "PRE_ORDER": InventoryStatus.PREORDER,
    "PREORDER": InventoryStatus.PREORDER,
}


def _product_data(html: str) -> dict | None:
    script = BeautifulSoup(html, "html.parser").find("script", id="__NEXT_DATA__")
    if script is None:
        return None
    try:
        data = json.loads(script.get_text() or "")
        product = data["props"]["pageProps"]["initialData"]["data"]["product"]
    except (json.JSONDecodeError, KeyError, TypeError):
        return None
    return product if isinstance(product, dict) else None


def _price(offer: dict) -> float | None:
    try:
        return float(offer["priceInfo"]["currentPrice"]["price"])
    except (KeyError, TypeError, ValueError):
        return None


def is_walmart_offer(offer: dict) -> bool:
    """Sold by Walmart itself (never a Marketplace seller)."""
    if str(offer.get("sellerType") or "").upper() == "EXTERNAL":
        return False
    if offer.get("sellerId") == WALMART_SELLER_ID:
        return True
    names = {str(offer.get(k) or "").strip().lower() for k in ("sellerName", "sellerDisplayName")}
    return bool(names & set(WALMART_SELLER_NAMES))


def _to_parsed(offer: dict) -> ParsedOffer:
    status = _STATUS.get(str(offer.get("availabilityStatus") or "").upper(), InventoryStatus.UNKNOWN)
    if is_walmart_offer(offer):
        seller = "Walmart.com"
    else:
        # Tagged so a Marketplace seller can never be mistaken for Walmart by name.
        seller = f"{offer.get('sellerDisplayName') or offer.get('sellerName') or 'unknown seller'} (Marketplace)"
    return ParsedOffer(status=status, seller_name=seller, price=_price(offer), method="next-data")


def parse_walmart_page(html: str) -> ParsedOffer | None:
    """The offer that matters for this item page, or None if the page has no product data."""
    product = _product_data(html)
    if product is None or not product.get("availabilityStatus"):
        return None
    offers = [product] + [o for o in (product.get("secondaryOffers") or []) if isinstance(o, dict)]
    walmart = [o for o in offers if is_walmart_offer(o)]
    if walmart:
        in_stock = [o for o in walmart if _STATUS.get(str(o.get("availabilityStatus")).upper())
                    in (InventoryStatus.AVAILABLE, InventoryStatus.LIMITED)]
        return _to_parsed((in_stock or walmart)[0])
    # Marketplace sellers only: report the buy-box offer as third party.
    return _to_parsed(product)


def page_upc(html: str) -> str | None:
    product = _product_data(html)
    return str(product.get("upc")) if product and product.get("upc") else None


class WalmartMonitor(StructuredDataRetailerMonitor):
    slug = "walmart"
    display_name = "Walmart"
    implementation_status = "online: product page data (sold by Walmart only); store-level: not available"
    first_party_seller_names = WALMART_SELLER_NAMES

    BASE_URL = "https://www.walmart.com"

    @staticmethod
    def item_id_from_url(url: str | None) -> str | None:
        if not url:
            return None
        m = ITEM_ID_IN_URL_RE.search(url)
        return m.group(1) if m else None

    def get_product_url(self, product: ProductRef) -> str | None:
        if product.product_url and product.product_url.startswith(self.BASE_URL):
            return product.product_url
        if ITEM_ID_RE.match(product.sku or ""):
            return f"{self.BASE_URL}/ip/{product.sku}"
        return product.product_url

    def missing_markup_message(self) -> str:
        return ("Couldn't find the product data on the Walmart page; Walmart may have changed its page "
                "layout. Status is UNKNOWN, never assumed out of stock.")

    def fallback_offer(self, html: str) -> ParsedOffer | None:
        return parse_walmart_page(html)
