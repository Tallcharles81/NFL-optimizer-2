#!/usr/bin/env python
"""Add a product to monitor.

Examples:
  # Online listing
  python scripts/add_product.py --retailer target --sku 12345678 \
      --name "Pokemon Example Product" --url "https://www.target.com/p/-/A-12345678"

  # At a specific store (store looked up by name from TARGET_STORES / saved stores)
  python scripts/add_product.py --retailer target --sku 12345678 \
      --name "Pokemon Example Product" --url "PRODUCT_URL" --store "Athens Target"

  # At an explicit store
  python scripts/add_product.py --retailer target --sku 12345678 --name "..." \
      --store-id 1234 --store "Athens Target" --city Athens --state TN --zip 37303

  # At every configured store for the retailer (plus the online listing)
  python scripts/add_product.py --retailer target --sku 12345678 --name "..." --all-stores --online
"""

import argparse
import sys

import _bootstrap  # noqa: F401
from pydantic import ValidationError

from app.config import get_settings
from app.database import init_database
from app.services.product_service import (
    ProductCreate,
    create_product,
    ensure_retailers,
    list_stores,
    sync_config_stores,
    validation_warnings,
)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--retailer", required=True, help="target, walmart, gamestop, five_below, hobby_lobby, cvs, walgreens")
    p.add_argument("--sku", required=True, help="Retailer SKU (Target: the TCIN, the number after /A- in the URL)")
    p.add_argument("--name", required=True, help="Product name")
    p.add_argument("--url", help="Product URL")
    p.add_argument("--image-url")
    p.add_argument("--store", help="Store name (looked up in configured stores if --store-id is omitted)")
    p.add_argument("--store-id")
    p.add_argument("--city")
    p.add_argument("--state")
    p.add_argument("--zip", dest="zip_code")
    p.add_argument("--max-quantity", type=int)
    p.add_argument("--upc", help="UPC barcode (shown in the alert)")
    p.add_argument("--dpci", help="Target DPCI, e.g. 361-00-8095 (shown in the alert; useful in store)")
    p.add_argument("--poll-interval", type=float, help="Per-product NORMAL poll interval in seconds")
    p.add_argument("--accept-third-party", choices=["yes", "no"], help="Override ACCEPT_THIRD_PARTY for this product")
    p.add_argument("--disabled", action="store_true", help="Add but don't monitor yet")
    p.add_argument("--all-stores", action="store_true", help="Add one entry per configured store of this retailer")
    p.add_argument("--online", action="store_true", help="With --all-stores: also add the online listing")
    args = p.parse_args()

    settings = get_settings()
    db = init_database(settings.database_url)
    base = dict(retailer=args.retailer, sku=args.sku, product_name=args.name, product_url=args.url,
                image_url=args.image_url, max_quantity=args.max_quantity, upc=args.upc, dpci=args.dpci, enabled=not args.disabled,
                poll_interval_seconds=args.poll_interval,
                accept_third_party=None if args.accept_third_party is None else args.accept_third_party == "yes")

    with db.session() as s:
        ensure_retailers(s)
        sync_config_stores(s, settings)
        stores = [st for st in list_stores(s, args.retailer.lower())]
        targets: list[dict] = []
        if args.all_stores:
            if not stores:
                print(f"No stores configured for {args.retailer}. Set TARGET_STORES in .env or use --store-id.")
                return 2
            targets = [dict(store_id=st.store_id, store_name=st.name, city=st.city, state=st.state,
                            zip_code=st.zip_code) for st in stores]
            if args.online:
                targets.append({})
        elif args.store_id:
            targets = [dict(store_id=args.store_id, store_name=args.store, city=args.city, state=args.state,
                            zip_code=args.zip_code)]
        elif args.store:
            wanted = args.store.strip().lower()
            match = [st for st in stores if st.name.lower() == wanted or (st.city or "").lower() == wanted
                     or st.label.lower() == wanted]
            if len(match) != 1:
                known = ", ".join(f"{st.name} (id {st.store_id})" for st in stores) or "none"
                print(f"Store {args.store!r} not found among configured {args.retailer} stores ({known}).\n"
                      "Add it to TARGET_STORES in .env (store_id|name|city|state|zip) or pass --store-id.")
                return 2
            st = match[0]
            targets = [dict(store_id=st.store_id, store_name=st.name, city=st.city, state=st.state,
                            zip_code=st.zip_code)]
        else:
            targets = [{}]

        for extra in targets:
            try:
                data = ProductCreate(**base, **extra)
                product = create_product(s, data)
            except (ValidationError, ValueError) as exc:
                print(f"ERROR: {exc}")
                return 1
            print(f"Added product id={product.id}: {product.product_name} [{data.retailer} {data.sku}] "
                  f"@ {product.location_label}")
            for w in validation_warnings(data):
                print(f"  warning: {w}")
    print("The running monitor picks new products up on its next scheduler tick.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
