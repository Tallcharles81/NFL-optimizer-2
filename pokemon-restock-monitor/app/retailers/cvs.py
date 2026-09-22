"""CVS -- generic schema.org implementation (unverified).

Uses the public product page's schema.org availability markup. Not yet
validated against live CVS pages; treat results with care and review
CVS's terms of use before enabling. Store-level inventory is not
implemented (returns UNKNOWN). Requires a full product URL per product.
"""

from app.retailers.structured_data import StructuredDataRetailerMonitor


class CVSMonitor(StructuredDataRetailerMonitor):
    slug = "cvs"
    display_name = "CVS"
    first_party_seller_names = ("cvs", "cvs pharmacy", "cvs.com")
