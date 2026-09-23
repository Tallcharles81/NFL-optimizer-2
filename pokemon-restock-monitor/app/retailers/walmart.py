"""Walmart -- product page reader (being validated against live pages).

Products are identified by Walmart item ID (the number in
``https://www.walmart.com/ip/<name>/<item id>``). Only offers sold by Walmart
itself count; marketplace sellers are THIRD_PARTY. Store-level inventory is
not implemented (returns UNKNOWN).
"""

from __future__ import annotations

import re

from app.retailers.base import ProductRef
from app.retailers.structured_data import StructuredDataRetailerMonitor

ITEM_ID_RE = re.compile(r"^\d{5,14}$")


class WalmartMonitor(StructuredDataRetailerMonitor):
    slug = "walmart"
    display_name = "Walmart"
    first_party_seller_names = ("walmart", "walmart.com", "walmart inc.")

    BASE_URL = "https://www.walmart.com"

    def get_product_url(self, product: ProductRef) -> str | None:
        if product.product_url and product.product_url.startswith(self.BASE_URL):
            return product.product_url
        if ITEM_ID_RE.match(product.sku or ""):
            return f"{self.BASE_URL}/ip/{product.sku}"
        return product.product_url
