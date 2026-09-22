"""GameStop -- generic schema.org implementation (unverified).

Uses the public product page's schema.org availability markup. Not yet
validated against live GameStop pages; treat results with care and review
GameStop's terms of use before enabling. Store-level inventory is not
implemented (returns UNKNOWN). Requires a full product URL per product.
"""

from app.retailers.structured_data import StructuredDataRetailerMonitor


class GameStopMonitor(StructuredDataRetailerMonitor):
    slug = "gamestop"
    display_name = "GameStop"
    first_party_seller_names = ("gamestop",)
