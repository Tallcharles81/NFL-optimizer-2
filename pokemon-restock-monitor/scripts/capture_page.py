#!/usr/bin/env python
"""Diagnostics: fetch one product page exactly as the monitor does and save it.

Writes <out>/page.html and <out>/summary.txt (structured-data blocks found and
text around stock-related words) so the parser can be adapted to what the
retailer actually serves. Honors robots.txt and the rate limiter like a
normal check. Makes one page request.

  python scripts/capture_page.py --retailer target --sku 1010892076 --out diag
"""

import argparse
import asyncio
import json
import re
import sys
from pathlib import Path

import _bootstrap  # noqa: F401
from bs4 import BeautifulSoup

from app.config import get_settings
from app.retailers.base import ProductRef
from app.retailers.registry import RetailerManager
from app.retailers.structured_data import parse_product_page

KEYWORDS = ["add to cart", "out of stock", "sold out", "preorder", "pre-order", "not available",
            "notify me", "ship it", "pick it up", "deliver it", "shipping", "in stock",
            "availability", "IN_STOCK", "OUT_OF_STOCK", "UNAVAILABLE", "PRE_ORDER",
            "availability_status", "is_out_of_stock", "purchasable"]


async def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--retailer", default="target")
    p.add_argument("--sku", required=True)
    p.add_argument("--url")
    p.add_argument("--out", default="diag")
    args = p.parse_args()
    settings = get_settings()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    mgr = RetailerManager(settings)
    retailer = mgr.get(args.retailer)
    ref = ProductRef(id=0, retailer=args.retailer, sku=args.sku, product_name="diag", product_url=args.url)
    url = retailer.get_product_url(ref)
    lines = [f"url: {url}", f"browser: {settings.target_use_browser}"]
    try:
        page = await retailer.fetch_page(url)
    except Exception as exc:  # noqa: BLE001
        lines.append(f"FETCH FAILED: {type(exc).__name__}: {exc}")
        (out / "summary.txt").write_text("\n".join(lines))
        print("\n".join(lines))
        await mgr.aclose()
        return 1
    await mgr.aclose()
    html = page.text
    (out / "page.html").write_text(html, encoding="utf-8")
    lines += [f"http_status: {page.status_code}", f"bytes: {len(html)}"]

    soup = BeautifulSoup(html, "html.parser")
    lines.append(f"title: {soup.title.get_text(strip=True) if soup.title else None}")
    ld = soup.find_all("script", attrs={"type": "application/ld+json"})
    lines.append(f"json-ld blocks: {len(ld)}")
    for i, s in enumerate(ld):
        raw = (s.string or s.get_text() or "")[:1500]
        lines.append(f"--- json-ld {i}: {raw}")
    parsed = parse_product_page(html)
    lines.append(f"structured parse: found={parsed.found} offers={[(o.status.value, o.seller_name) for o in parsed.offers]}")
    for tag in soup.find_all("script", id=True):
        lines.append(f"script id={tag.get('id')} len={len(tag.get_text() or '')}")
    for name in ("__TGT_DATA__", "__NEXT_DATA__", "__PRELOADED_QUERIES__", "__CONFIG__"):
        lines.append(f"contains {name}: {name in html}")

    visible = soup.get_text(" ", strip=True)
    lines.append("=== visible text around keywords ===")
    for kw in KEYWORDS:
        for m in list(re.finditer(re.escape(kw), visible, re.I))[:3]:
            lines.append(f"[text:{kw}] ...{visible[max(0, m.start() - 120):m.end() + 120]}...")
    lines.append("=== raw html around keywords ===")
    for kw in KEYWORDS:
        for m in list(re.finditer(re.escape(kw), html, re.I))[:3]:
            snippet = html[max(0, m.start() - 200):m.end() + 200].replace("\n", " ")
            lines.append(f"[html:{kw}] ...{snippet}...")
    for sel in ('[data-module-type="ProductDetailAddToCart"]', '[data-test="module-product-detail-price-v2"]',
                '[data-module-type="ProductDetailFulfillmentMessaging"]'):
        el = soup.select_one(sel)
        lines.append(f"=== {sel} ===")
        lines.append(str(el)[:3500] if el else "(not found)")
    try:
        from app.retailers.target import parse_target_buy_box

        lines.append(f"buy box parse: {parse_target_buy_box(html)}")
    except ImportError:
        pass
    buttons = sorted({b.get_text(" ", strip=True)[:60] for b in soup.find_all("button") if b.get_text(strip=True)})
    lines.append(f"buttons: {json.dumps(buttons[:80])}")
    data_tests = sorted({t.get("data-test") for t in soup.find_all(attrs={"data-test": True})})
    lines.append(f"data-test attributes: {json.dumps(data_tests[:200])}")
    (out / "summary.txt").write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines[:40]))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
