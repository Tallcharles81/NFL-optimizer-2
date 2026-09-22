"""Hobby Lobby -- generic schema.org implementation (unverified).

Uses the public product page's schema.org availability markup. Not yet
validated against live Hobby Lobby pages; treat results with care and review
Hobby Lobby's terms of use before enabling. Store-level inventory is not
implemented (returns UNKNOWN). Requires a full product URL per product.
"""

from app.retailers.structured_data import StructuredDataRetailerMonitor


class HobbyLobbyMonitor(StructuredDataRetailerMonitor):
    slug = "hobby_lobby"
    display_name = "Hobby Lobby"
    first_party_seller_names = ("hobby lobby",)
