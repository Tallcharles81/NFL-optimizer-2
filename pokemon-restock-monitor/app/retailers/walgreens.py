"""Walgreens -- generic schema.org implementation (unverified).

Uses the public product page's schema.org availability markup. Not yet
validated against live Walgreens pages; treat results with care and review
Walgreens's terms of use before enabling. Store-level inventory is not
implemented (returns UNKNOWN). Requires a full product URL per product.
"""

from app.retailers.structured_data import StructuredDataRetailerMonitor


class WalgreensMonitor(StructuredDataRetailerMonitor):
    slug = "walgreens"
    display_name = "Walgreens"
    first_party_seller_names = ("walgreens", "walgreens.com")
