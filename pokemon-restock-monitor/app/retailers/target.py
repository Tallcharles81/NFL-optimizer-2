"""Target.

Everything Target-specific lives in this file.

Data source: the public product page at ``https://www.target.com/p/-/A-<TCIN>``
and the schema.org availability markup it publishes. Requests are made with
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

from app.models.enums import AvailabilityScope, InventoryStatus
from app.retailers.base import Observation, ProductRef
from app.retailers.structured_data import StructuredDataRetailerMonitor

TCIN_RE = re.compile(r"^\d{6,10}$")
TCIN_IN_URL_RE = re.compile(r"/A-(\d{6,10})")


class TargetMonitor(StructuredDataRetailerMonitor):
    slug = "target"
    display_name = "Target"
    implementation_status = "online: public product page (schema.org markup); store-level: not available"
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
        return (
            "Target product page did not include schema.org availability markup in the initial "
            "HTML (Target renders much of the page client-side). Status is UNKNOWN, never assumed "
            "out of stock. Options: set TARGET_USE_BROWSER=true to render the page normally, or "
            "implement a Target-permitted data source in app/retailers/target.py."
        )

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
