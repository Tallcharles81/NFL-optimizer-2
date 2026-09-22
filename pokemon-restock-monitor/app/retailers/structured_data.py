"""Parse public, standardized product markup (schema.org) from a product page.

Retailers publish schema.org ``Product``/``Offer`` markup (JSON-LD,
microdata, or Open Graph ``product:availability`` meta tags) specifically so
machines such as search engines can read price and availability. It is the
most legitimate public signal available on a normal product page, so the
generic retailer implementation relies only on it.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

from bs4 import BeautifulSoup

from app.models.enums import AvailabilityScope, InventoryStatus, SellerType
from app.retailers.base import Observation, ProductRef, RetailerMonitor

logger = logging.getLogger("retailers.structured_data")

_AVAILABILITY_MAP = {
    "instock": InventoryStatus.AVAILABLE,
    "in stock": InventoryStatus.AVAILABLE,
    "onlineonly": InventoryStatus.AVAILABLE,
    "limitedavailability": InventoryStatus.LIMITED,
    "preorder": InventoryStatus.PREORDER,
    "presale": InventoryStatus.PREORDER,
    "outofstock": InventoryStatus.OUT_OF_STOCK,
    "out of stock": InventoryStatus.OUT_OF_STOCK,
    "oos": InventoryStatus.OUT_OF_STOCK,
    "soldout": InventoryStatus.OUT_OF_STOCK,
    # Back-ordered items are not "in stock"; be conservative.
    "backorder": InventoryStatus.OUT_OF_STOCK,
    "discontinued": InventoryStatus.UNAVAILABLE,
    # For an *online* check, in-store-only means you can't buy it online.
    "instoreonly": InventoryStatus.UNAVAILABLE,
    "pending": InventoryStatus.UNKNOWN,
}


def normalize_availability(value: str | None) -> InventoryStatus:
    if not value:
        return InventoryStatus.UNKNOWN
    token = str(value).strip().rstrip("/").rsplit("/", 1)[-1].strip().lower()
    return _AVAILABILITY_MAP.get(token, _AVAILABILITY_MAP.get(token.replace("_", ""), InventoryStatus.UNKNOWN))


@dataclass
class ParsedOffer:
    status: InventoryStatus
    seller_name: str | None = None
    price: float | None = None
    method: str = ""


@dataclass
class ParsedProduct:
    offers: list[ParsedOffer] = field(default_factory=list)
    name: str | None = None
    image: str | None = None

    @property
    def found(self) -> bool:
        return bool(self.offers)


def _as_list(value) -> list:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _types(node: dict) -> set[str]:
    return {str(t).lower() for t in _as_list(node.get("@type"))}


def _walk(node):
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v)


def _price(offer: dict) -> float | None:
    for key in ("price", "lowPrice"):
        raw = offer.get(key)
        if raw is None:
            continue
        try:
            return float(str(raw).replace("$", "").replace(",", ""))
        except ValueError:
            continue
    return None


def _seller(offer: dict) -> str | None:
    seller = offer.get("seller") or offer.get("offeredBy")
    if isinstance(seller, list):
        seller = seller[0] if seller else None
    if isinstance(seller, dict):
        return seller.get("name")
    if isinstance(seller, str):
        return seller
    return None


def parse_product_page(html: str) -> ParsedProduct:
    soup = BeautifulSoup(html, "html.parser")
    result = ParsedProduct()

    # 1. JSON-LD
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = script.string or script.get_text() or ""
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            continue
        for node in _walk(data):
            types = _types(node)
            if "product" in types:
                result.name = result.name or node.get("name")
                img = node.get("image")
                if isinstance(img, list):
                    img = img[0] if img else None
                if isinstance(img, dict):
                    img = img.get("url")
                result.image = result.image or img
                for offer in _as_list(node.get("offers")):
                    if not isinstance(offer, dict):
                        continue
                    sub_offers = _as_list(offer.get("offers")) if "aggregateoffer" in _types(offer) else []
                    for o in sub_offers or [offer]:
                        if isinstance(o, dict) and o.get("availability"):
                            result.offers.append(ParsedOffer(
                                status=normalize_availability(o.get("availability")),
                                seller_name=_seller(o), price=_price(o), method="json-ld",
                            ))
    if result.found:
        return result

    # 2. Microdata: itemprop="availability"
    for tag in soup.find_all(attrs={"itemprop": "availability"}):
        value = tag.get("href") or tag.get("content") or tag.get_text(strip=True)
        status = normalize_availability(value)
        if status != InventoryStatus.UNKNOWN:
            result.offers.append(ParsedOffer(status=status, method="microdata"))
    if result.found:
        return result

    # 3. Open Graph / product meta tags
    for prop in ("product:availability", "og:availability"):
        tag = soup.find("meta", attrs={"property": prop})
        if tag and tag.get("content"):
            status = normalize_availability(tag["content"])
            if status != InventoryStatus.UNKNOWN:
                result.offers.append(ParsedOffer(status=status, method="meta"))
                break
    return result


class StructuredDataRetailerMonitor(RetailerMonitor):
    """Generic retailer: fetch the public product page, read schema.org markup.

    Subclasses set slug/display_name/first_party_seller_names and may override
    ``get_product_url`` and ``fetch_page``.
    """

    implementation_status = "generic (schema.org markup) - unverified for this retailer"

    async def fetch_page(self, url: str):
        if self.http is None:
            raise RuntimeError(f"{self.slug}: no HTTP client configured")
        return await self.http.get(url)

    def choose_offer(self, offers: list[ParsedOffer]) -> ParsedOffer:
        """Prefer the retailer's own offer over marketplace offers."""
        first_party = [o for o in offers if self.classify_seller(o.seller_name) == SellerType.FIRST_PARTY_RETAILER]
        pool = first_party or offers
        purchasable = [o for o in pool if o.status in (InventoryStatus.AVAILABLE, InventoryStatus.LIMITED)]
        return (purchasable or pool)[0]

    def missing_markup_message(self) -> str:
        return (
            f"No schema.org availability markup found on the {self.display_name} product page. "
            "Status is UNKNOWN (never assumed out of stock)."
        )

    async def get_product_status(self, product: ProductRef) -> Observation:
        url = self.get_product_url(product)
        if not url:
            return Observation(status=InventoryStatus.UNKNOWN, source=self.slug,
                               message="No product URL configured for this product.")
        page = await self.fetch_page(url)
        source = f"{self.slug}:product-page"
        if page.status_code == 404:
            return Observation(
                status=InventoryStatus.UNAVAILABLE, scope=AvailabilityScope.ONLINE_UNAVAILABLE,
                source=source, http_status=404, response_time_ms=page.elapsed_ms,
                message="Product page returned 404 (listing removed or wrong SKU/URL).",
            )
        parsed = parse_product_page(page.text)
        if not parsed.found:
            return Observation(status=InventoryStatus.UNKNOWN, source=source, http_status=page.status_code,
                               response_time_ms=page.elapsed_ms, message=self.missing_markup_message())
        offer = self.choose_offer(parsed.offers)
        purchasable = offer.status in (InventoryStatus.AVAILABLE, InventoryStatus.LIMITED, InventoryStatus.PREORDER)
        return Observation(
            status=offer.status,
            scope=AvailabilityScope.ONLINE_AVAILABLE if purchasable else (
                AvailabilityScope.ONLINE_UNAVAILABLE if offer.status != InventoryStatus.UNKNOWN
                else AvailabilityScope.UNKNOWN),
            seller_type=self.classify_seller(offer.seller_name),
            seller_name=offer.seller_name,
            price=offer.price,
            source=f"{source}:{offer.method}",
            http_status=page.status_code,
            response_time_ms=page.elapsed_ms,
        )
