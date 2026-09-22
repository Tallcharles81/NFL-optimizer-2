"""Walmart -- generic schema.org implementation (unverified).

Uses the public product page's schema.org availability markup. Not yet
validated against live Walmart pages; treat results with care and review
Walmart's terms of use before enabling. Store-level inventory is not
implemented (returns UNKNOWN). Requires a full product URL per product.
"""

from app.retailers.structured_data import StructuredDataRetailerMonitor


class WalmartMonitor(StructuredDataRetailerMonitor):
    slug = "walmart"
    display_name = "Walmart"
    first_party_seller_names = ("walmart", "walmart.com", "walmart inc.")
