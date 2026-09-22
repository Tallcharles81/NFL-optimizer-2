"""Optional normal page rendering with Playwright.

Used only when a retailer page needs JavaScript to render its public content
and ``<RETAILER>_USE_BROWSER=true``. The browser identifies itself with the
same honest User-Agent, loads one page, and returns the HTML. There is no
stealth plugin, fingerprint spoofing, CAPTCHA solving or proxy rotation. If a
challenge page appears, the retailer is paused like any other block.

Install with: pip install -r requirements-browser.txt && playwright install chromium
"""

from __future__ import annotations

import asyncio
import time

from app.config import Settings
from app.retailers.http_client import HttpResult, looks_like_challenge
from app.utils.rate_limit import BotChallengeError, HttpStatusError, RateLimitedError, RateLimiter


# One browser per process, reused for every page (launching Chromium per page is slow).
_playwright = None
_browser = None
_lock: asyncio.Lock | None = None


def _get_lock() -> asyncio.Lock:
    # Created lazily so it belongs to the running event loop.
    global _lock
    if _lock is None:
        _lock = asyncio.Lock()
    return _lock

# Not needed to read a page's text, so not downloaded: faster, and lighter on the retailer.
SKIPPED_RESOURCES = {"image", "media", "font"}


async def _get_browser():
    global _playwright, _browser
    async with _get_lock():
        if _browser is None or not _browser.is_connected():
            try:
                from playwright.async_api import async_playwright
            except ImportError as exc:  # pragma: no cover - optional dependency
                raise RuntimeError(
                    "Playwright is not installed. pip install -r requirements-browser.txt"
                ) from exc
            _playwright = await async_playwright().start()
            _browser = await _playwright.chromium.launch(headless=True)
    return _browser


async def close_browser() -> None:
    global _playwright, _browser, _lock
    if _browser is None and _playwright is None:
        _lock = None
        return
    async with _get_lock():
        if _browser is not None:
            try:
                await _browser.close()
            except Exception:  # noqa: BLE001
                pass
        if _playwright is not None:
            try:
                await _playwright.stop()
            except Exception:  # noqa: BLE001
                pass
        _browser = _playwright = None
    _lock = None


async def _skip_heavy_resources(route) -> None:
    if route.request.resource_type in SKIPPED_RESOURCES:
        await route.abort()
    else:
        await route.continue_()


async def _wait_until_stable(page, selectors: list[str], settle: float, max_wait: float) -> None:
    """Wait until the given regions exist and stop changing for ``settle`` seconds."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max_wait
    last, stable_since = None, loop.time()
    while loop.time() < deadline:
        parts = []
        for sel in selectors:
            loc = page.locator(sel)
            if await loc.count():
                parts.append(await loc.first.inner_html())
        snapshot = "|".join(parts)
        now = loop.time()
        if snapshot and snapshot == last:
            if now - stable_since >= settle:
                return
        else:
            last, stable_since = snapshot, now
        await asyncio.sleep(0.4)


async def fetch_rendered_page(url: str, limiter: RateLimiter, settings: Settings,
                              ready_selectors: list[str] | None = None) -> HttpResult:
    """Load one page normally and return its rendered HTML.

    With ``ready_selectors`` it returns as soon as those regions have rendered and
    stayed unchanged for a moment, instead of waiting a fixed time.
    """
    await limiter.acquire()
    start = time.perf_counter()
    browser = await _get_browser()
    context = await browser.new_context(user_agent=settings.effective_user_agent)
    try:
        await context.route("**/*", _skip_heavy_resources)
        page = await context.new_page()
        resp = await page.goto(url, wait_until="domcontentloaded",
                               timeout=settings.request_timeout_seconds * 1000 * 2)
        status = resp.status if resp else 0
        if ready_selectors:
            try:
                await page.wait_for_selector(", ".join(ready_selectors), timeout=15_000)
            except Exception:  # noqa: BLE001 - use whatever has rendered
                pass
            # The buy box can update after it first appears (e.g. stock loads a moment
            # later), so only read it once it has stopped changing.
            await _wait_until_stable(page, ready_selectors, settle=2.5, max_wait=10)
        else:
            # Retail pages keep background requests going, so "network idle" may never
            # happen; wait a bounded time for the page to finish rendering instead.
            try:
                await page.wait_for_load_state("networkidle", timeout=10_000)
            except Exception:  # noqa: BLE001
                pass
        html = await page.content()
    finally:
        await context.close()
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
