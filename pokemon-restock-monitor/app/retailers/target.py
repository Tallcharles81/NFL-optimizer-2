"""Target.

Everything Target-specific lives in this file.

Data source: the public product page at ``https://www.target.com/p/-/A-<TCIN>``.
Target publishes no schema.org availability, so the page is rendered in a
browser (TARGET_USE_BROWSER=true) and the main product's buy box is read the
way a shopper sees it: the price module and the Add to cart button. Requests are made with
an honest User-Agent, only when robots.txt allows the URL, and through the
shared rate limiter. If Target serves a CAPTCHA/bot challenge or a 403, the
monitor stops polling Target for ``BLOCKED_COOLDOWN_SECONDS`` -- it never
tries to get past it.

Store-level inventory: Target does not publish a documented, public
store-inventory interface that we are permitted to use, so store checks
return UNKNOWN (never "available because it's available online"). If you
obtain a permitted data source (e.g. an official partner API), implement
``get_store_inventory`` here and set ``supports_store_inventory = True``.

Seller: Target Plus marketplace listings are sold by third parties. When the
page's offer names a seller other than Target it is classified THIRD_PARTY
and (with ACCEPT_THIRD_PARTY=false) will not generate a restock alert.
"""

from __future__ import annotations

import re

from bs4 import BeautifulSoup

from app.models.enums import AvailabilityScope, InventoryStatus
from app.retailers.base import Observation, ProductRef
from app.retailers.structured_data import ParsedOffer, StructuredDataRetailerMonitor

TCIN_RE = re.compile(r"^\d{6,10}$")
TCIN_IN_URL_RE = re.compile(r"/A-(\d{6,10})")

# --- Buy-box reading ---------------------------------------------------------------
# Target's product page has no schema.org availability, even after rendering. What
# it does show every visitor is the main product's buy box: the price module (e.g.
# "$69.99 Out of Stock") and the Add to cart button, which is disabled when out of
# stock. Only these modules are read -- "Add to cart" buttons on recommended
# products elsewhere on the page are ignored.
ADD_TO_CART_MODULES = ('[data-module-type="ProductDetailAddToCart"]',
                       '[data-test="module-product-detail-add-to-cart"]')
PRICE_MODULES = ('[data-test="module-product-detail-price-v2"]', '[data-module-type^="ProductDetailPrice"]')
FULFILLMENT_MODULES = ('[data-module-type="ProductDetailFulfillmentMessaging"]',)
OUT_OF_STOCK_RE = re.compile(r"\b(out of stock|sold out|no longer available|not available)\b", re.I)
PREORDER_RE = re.compile(r"\bpre-?order\b", re.I)
BUY_LABEL_RE = re.compile(r"\b(add to cart|pre-?order|ship it|pick it up|deliver it|buy now)\b", re.I)
PRICE_RE = re.compile(r"\$\s?(\d{1,4}(?:,\d{3})*(?:\.\d{2})?)")
SELLER_RE = re.compile(r"sold\s*(?:(?:and|&)\s*shipped\s*)?by\s+([A-Za-z0-9&'.,\- ]{2,60}?)"
                       r"(?=\s*(?:$|\||\.\s|\.$|view|learn|ships|shipping|return|report))", re.I)
# Target Plus (marketplace partner) listings carry this block in the buy box.
TARGET_PLUS_SELECTOR = '[data-test="targetPlusExtraInfoSection"]'



def _first(soup, selectors):
    for sel in selectors:
        el = soup.select_one(sel)
        if el is not None:
            return el
    return None


def _is_disabled(button) -> bool:
    return (button.has_attr("disabled") or button.get("aria-disabled") == "true"
            or (button.get("data-component-state") or "").lower() == "disabled"
            or "disabled" in (button.get("data-test") or "").lower())


def parse_target_buy_box(html: str) -> ParsedOffer | None:
    """Stock status from the main product's buy box, or None if it isn't there."""
    soup = BeautifulSoup(html, "html.parser")
    atc = _first(soup, ADD_TO_CART_MODULES)
    price_el = _first(soup, PRICE_MODULES)
    if atc is None and price_el is None:
        return None
    parts = [el.get_text(" ", strip=True) for el in (price_el, _first(soup, FULFILLMENT_MODULES), atc) if el]
    text = " ".join(parts)
    price_match = PRICE_RE.search(price_el.get_text(" ", strip=True)) if price_el else None
    price = float(price_match.group(1).replace(",", "")) if price_match else None
    # Target Plus partner listings say "Sold & shipped by <partner>"; otherwise Target sells it.
    seller = "Target"
    partner = soup.select_one(TARGET_PLUS_SELECTOR)
    if partner is not None:
        m = SELLER_RE.search(partner.get("aria-label") or "") or SELLER_RE.search(partner.get_text(" ", strip=True))
        seller = m.group(1).strip() if m else "Target Plus partner"
    else:
        m = SELLER_RE.search(text)
        if m:
            seller = m.group(1).strip()

    buttons = []
    if atc is not None:
        for b in atc.find_all("button"):
            label = " ".join(filter(None, [b.get_text(" ", strip=True), b.get("aria-label") or ""]))
            if BUY_LABEL_RE.search(label):
                buttons.append((b, label))
    enabled = [(b, label) for b, label in buttons if not _is_disabled(b)]
    says_oos = bool(OUT_OF_STOCK_RE.search(text))

    if enabled and not says_oos:
        status = InventoryStatus.PREORDER if any(PREORDER_RE.search(label) for _, label in enabled) \
            else InventoryStatus.AVAILABLE
    elif says_oos or buttons:
        # Out-of-stock text, or every buy button is disabled.
        status = InventoryStatus.OUT_OF_STOCK
    else:
        return None
    return ParsedOffer(status=status, seller_name=seller, price=price, method="buy-box")


class TargetMonitor(StructuredDataRetailerMonitor):
    slug = "target"
    display_name = "Target"
    implementation_status = "online: rendered product page buy box; store-level: not available"
    supports_store_inventory = False
    first_party_seller_names = ("target", "target.com", "target corporation", "target stores")

    BASE_URL = "https://www.target.com"

    @staticmethod
    def is_valid_tcin(sku: str) -> bool:
        return bool(TCIN_RE.match(sku or ""))

    @staticmethod
    def tcin_from_url(url: str | None) -> str | None:
        if not url:
            return None
        m = TCIN_IN_URL_RE.search(url)
        return m.group(1) if m else None

    def get_product_url(self, product: ProductRef) -> str | None:
        if product.product_url and product.product_url.startswith(self.BASE_URL):
            return product.product_url
        if self.is_valid_tcin(product.sku):
            return f"{self.BASE_URL}/p/-/A-{product.sku}"
        return product.product_url

    async def fetch_page(self, url: str):
        if self.settings.target_use_browser:
            from app.retailers.browser import fetch_rendered_page

            # robots.txt is still honored before the browser loads anything
            await self.http.ensure_allowed(url)
            return await fetch_rendered_page(url, self.limiter, self.settings)
        return await super().fetch_page(url)

    def missing_markup_message(self) -> str:
        if not self.settings.target_use_browser:
            return (
                "Target's product page doesn't include stock status until it is rendered in a "
                "browser. Status is UNKNOWN, never assumed out of stock. Set TARGET_USE_BROWSER=true."
            )
        return (
            "Couldn't find the product's buy box (price / Add to cart) on the rendered Target page; "
            "Target may have changed its page layout. Status is UNKNOWN, never assumed out of stock."
        )

    def fallback_offer(self, html: str) -> ParsedOffer | None:
        return parse_target_buy_box(html)

    async def get_store_inventory(self, product: ProductRef) -> Observation:
        return Observation(
            status=InventoryStatus.UNKNOWN,
            scope=AvailabilityScope.UNKNOWN,
            source="target:store",
            message=(
                f"Store-level inventory for Target store {product.store_id} is not available: no "
                "permitted Target store-inventory data source is configured. Online availability is "
                "never reported as in-store availability."
            ),
        )
