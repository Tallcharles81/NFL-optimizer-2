#!/usr/bin/env python
"""Run ONE real, rate-limited check against a retailer and print what the monitor sees.

Doesn't write to the database or send alerts. Use it to confirm a SKU is
readable before relying on it:

  python scripts/test_monitor.py --retailer target --sku 12345678
  python scripts/test_monitor.py --product-id 3
  python scripts/test_monitor.py --retailer target --sku 12345678 --store-id 1234
"""

import argparse
import asyncio
import json
import sys

import _bootstrap  # noqa: F401

from app.config import get_settings
from app.database import init_database
from app.models import Product
from app.retailers.base import ProductRef
from app.retailers.registry import RetailerManager
from app.services.inventory_service import evaluate
from app.utils.logging import configure_logging


async def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--product-id", type=int)
    p.add_argument("--retailer")
    p.add_argument("--sku")
    p.add_argument("--url")
    p.add_argument("--store-id")
    args = p.parse_args()
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)

    if args.product_id:
        db = init_database(settings.database_url)
        with db.session() as s:
            prod = s.get(Product, args.product_id)
            if prod is None:
                print(f"No product with id {args.product_id}")
                return 1
            ref = ProductRef.from_model(prod, prod.retailer.slug)
    elif args.retailer and args.sku:
        ref = ProductRef(id=0, retailer=args.retailer.lower(), sku=args.sku, product_name="(test)",
                         product_url=args.url, store_id=args.store_id)
    else:
        p.error("give --product-id or --retailer and --sku")

    mgr = RetailerManager(settings)
    retailer = mgr.get(ref.retailer)
    print(f"Checking {retailer.display_name} SKU {ref.sku} @ {ref.location_label}")
    print(f"URL: {retailer.get_product_url(ref)}")
    obs = await retailer.safe_check(ref)
    ev = evaluate(obs, ref, settings)
    await mgr.aclose()
    out = {k: (v.value if hasattr(v, "value") else v) for k, v in vars(obs).items()}
    out["checked_at"] = obs.checked_at.isoformat()
    if out.get("inventory_changed_at"):
        out["inventory_changed_at"] = obs.inventory_changed_at.isoformat()
    print(json.dumps(out, indent=2, default=str))
    print(f"\nNormalized status: {ev.status.value}  alertable: {ev.alertable}"
          + (f"  ignored: {ev.ignored_reason}" if ev.ignored_reason else ""))
    if ev.note:
        print(f"Note: {ev.note}")
    print(f"Retailer limiter: {retailer.limiter.snapshot()}")
    return 0 if obs.request_success else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
