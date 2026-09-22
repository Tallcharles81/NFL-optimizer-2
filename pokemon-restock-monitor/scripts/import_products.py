#!/usr/bin/env python
"""Import a product list from CSV (columns: name,tcin[,upc,dpci,url,max_quantity]).

  python scripts/import_products.py catalog/target_30th_celebration.csv
  python scripts/import_products.py catalog/target_30th_celebration.csv --max-quantity 2
  python scripts/import_products.py catalog/target_30th_celebration.csv --all-stores --online

Already-imported rows are skipped, so it is safe to re-run.
"""

import argparse
import sys

import _bootstrap  # noqa: F401

from app.config import get_settings
from app.database import init_database
from app.services.product_service import ensure_retailers, import_catalog, list_stores, sync_config_stores


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("csv_file")
    p.add_argument("--retailer", default="target")
    p.add_argument("--max-quantity", type=int, help="Default when the CSV has no max_quantity column")
    p.add_argument("--all-stores", action="store_true", help="One entry per configured store")
    p.add_argument("--online", action="store_true", help="With --all-stores: also add the online listing")
    p.add_argument("--disabled", action="store_true")
    args = p.parse_args()

    settings = get_settings()
    db = init_database(settings.database_url)
    with db.session() as s:
        ensure_retailers(s)
        sync_config_stores(s, settings)
        locations = None
        if args.all_stores:
            stores = list_stores(s, args.retailer)
            if not stores:
                print(f"No stores configured for {args.retailer}; set TARGET_STORES in .env.")
                return 2
            locations = [dict(store_id=st.store_id, store_name=st.name, city=st.city, state=st.state,
                              zip_code=st.zip_code) for st in stores] + ([{}] if args.online else [])
        result = import_catalog(s, args.csv_file, args.retailer, args.max_quantity, locations,
                                enabled=not args.disabled)
        for product in result["added"]:
            print(f"  added  id={product.id} {product.sku} {product.product_name} @ {product.location_label}")
        for err in result["errors"]:
            print(f"  ERROR  {err}")
    print(f"\n{len(result['added'])} added, {result['existing']} already present, {len(result['errors'])} failed")
    return 1 if result["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
