#!/usr/bin/env python
"""Import a product list from CSV (columns: name,tcin[,upc,dpci,url,max_quantity]).

  python scripts/import_products.py catalog/target_30th_celebration.csv
  python scripts/import_products.py catalog/target_30th_celebration.csv --max-quantity 2
  python scripts/import_products.py catalog/target_30th_celebration.csv --all-stores --online

Already-imported rows are skipped, so it is safe to re-run.
"""

import argparse
import csv
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
)


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
    added = skipped = failed = 0
    with open(args.csv_file, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    with db.session() as s:
        ensure_retailers(s)
        sync_config_stores(s, settings)
        locations: list[dict] = [{}]
        if args.all_stores:
            stores = list_stores(s, args.retailer)
            if not stores:
                print(f"No stores configured for {args.retailer}; set TARGET_STORES in .env.")
                return 2
            locations = [dict(store_id=st.store_id, store_name=st.name, city=st.city, state=st.state,
                              zip_code=st.zip_code) for st in stores] + ([{}] if args.online else [])
        for row in rows:
            row = {k.strip().lower(): (v or "").strip() for k, v in row.items() if k}
            if not row.get("tcin"):
                print(f"  skip   {row.get('name')}: no TCIN")
                skipped += 1
                continue
            mq = row.get("max_quantity") or args.max_quantity
            for loc in locations:
                try:
                    data = ProductCreate(retailer=args.retailer, sku=row["tcin"], product_name=row["name"],
                                         product_url=row.get("url") or None, upc=row.get("upc") or None,
                                         dpci=row.get("dpci") or None, max_quantity=int(mq) if mq else None,
                                         enabled=not args.disabled, **loc)
                except ValidationError as exc:
                    print(f"  ERROR  {row.get('name')}: {exc.errors()[0]['msg']}")
                    failed += 1
                    continue
                try:
                    with s.begin_nested():
                        product = create_product(s, data)
                except ValueError:
                    skipped += 1
                    continue
                print(f"  added  id={product.id} {data.sku} {data.product_name} @ {product.location_label}")
                added += 1
    print(f"\n{added} added, {skipped} already present/skipped, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
