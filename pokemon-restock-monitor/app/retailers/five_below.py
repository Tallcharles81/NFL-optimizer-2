"""Five Below -- generic schema.org implementation (unverified).

Uses the public product page's schema.org availability markup. Not yet
validated against live Five Below pages; treat results with care and review
Five Below's terms of use before enabling. Store-level inventory is not
implemented (returns UNKNOWN). Requires a full product URL per product.
"""

from app.retailers.structured_data import StructuredDataRetailerMonitor


class FiveBelowMonitor(StructuredDataRetailerMonitor):
    slug = "five_below"
    display_name = "Five Below"
    first_party_seller_names = ("five below", "fivebelow")
