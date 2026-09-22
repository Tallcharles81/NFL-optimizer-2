"""Optional normal page rendering with Playwright.

Used only when a retailer page needs JavaScript to render its public content
and ``<RETAILER>_USE_BROWSER=true``. The browser identifies itself with the
same honest User-Agent, loads one page, and returns the HTML. There is no
stealth plugin, fingerprint spoofing, CAPTCHA solving or proxy rotation. If a
challenge page appears, the retailer is paused like any other block.

Install with: pip install -r requirements-browser.txt && playwright install chromium
"""

from __future__ import annotations

import time

from app.config import Settings
from app.retailers.http_client import HttpResult, looks_like_challenge
from app.utils.rate_limit import BotChallengeError, HttpStatusError, RateLimitedError, RateLimiter


async def fetch_rendered_page(url: str, limiter: RateLimiter, settings: Settings) -> HttpResult:
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:  # pragma: no cover - optional dependency
        raise RuntimeError(
            "Playwright is not installed. pip install -r requirements-browser.txt"
        ) from exc

    await limiter.acquire()
    start = time.perf_counter()
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            page = await browser.new_page(user_agent=settings.effective_user_agent)
            resp = await page.goto(url, wait_until="networkidle",
                                   timeout=settings.request_timeout_seconds * 1000 * 2)
            status = resp.status if resp else 0
            html = await page.content()
        finally:
            await browser.close()
    elapsed = int((time.perf_counter() - start) * 1000)
    if status == 429:
        limiter.record_rate_limited(None)
        raise RateLimitedError("HTTP 429 (browser)")
    if status == 403 or looks_like_challenge(html):
        limiter.record_blocked(f"HTTP {status}: bot challenge or access denied (browser)")
        raise BotChallengeError("Retailer served a challenge page; not bypassing.")
    if status >= 400 and status != 404:
        limiter.record_failure(f"HTTP {status}")
        raise HttpStatusError(f"HTTP {status}", status)
    limiter.record_success()
    return HttpResult(url=url, status_code=status, text=html, headers={}, elapsed_ms=elapsed)
