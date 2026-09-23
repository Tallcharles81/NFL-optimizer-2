"""A deliberately polite HTTP client for retailer requests.

* Identifies itself honestly with a descriptive User-Agent including a contact.
* Honors robots.txt for every URL before fetching it.
* Every request goes through the retailer's RateLimiter.
* Retries only transient failures (timeouts, connection errors, 5xx).
* 429 -> pause the retailer (Retry-After honored), no immediate retry.
* 403 / CAPTCHA / bot challenge -> stop and pause the retailer. We never try
  to solve, bypass or evade a challenge.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import httpx

from app.config import Settings
from app.utils.logging import log_event
from app.utils.rate_limit import (
    AccessNotPermittedError,
    BotChallengeError,
    HttpStatusError,
    RateLimitedError,
    RateLimiter,
    parse_retry_after,
)
from app.utils.retry import RetryableError, retry_async

logger = logging.getLogger("http")

RETRYABLE_STATUS = {500, 502, 503, 504}
BLOCK_STATUS = {403, 412}

# Markers of an anti-bot interstitial. If we see one we stop -- we do not try
# to get past it.
CHALLENGE_MARKERS = (
    "px-captcha",
    "captcha-delivery",
    "/cdn-cgi/challenge-platform",
    "cf-chl-",
    "g-recaptcha",
    "h-captcha",
    "are you a robot",
    "are you a human",
    "robot or human?",
    "unusual traffic from your computer",
    "access to this page has been denied",
    "request unsuccessful. incapsula",
)


def looks_like_challenge(body: str) -> bool:
    sample = body[:50_000].lower()
    return any(marker in sample for marker in CHALLENGE_MARKERS)


@dataclass
class HttpResult:
    url: str
    status_code: int
    text: str
    headers: dict
    elapsed_ms: int


class PoliteHttpClient:
    def __init__(
        self,
        retailer: str,
        limiter: RateLimiter,
        settings: Settings,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.retailer = retailer
        self.limiter = limiter
        self.settings = settings
        self.user_agent = settings.effective_user_agent
        self._client = httpx.AsyncClient(
            timeout=settings.request_timeout_seconds,
            follow_redirects=True,
            headers={
                "User-Agent": self.user_agent,
                "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.8",
            },
            transport=transport,
        )
        self._robots: dict[str, tuple[float, RobotFileParser | None]] = {}

    async def aclose(self) -> None:
        await self._client.aclose()

    # -- robots.txt ----------------------------------------------------------------
    async def _robots_for(self, url: str) -> RobotFileParser | None:
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        cached = self._robots.get(origin)
        if cached and time.monotonic() - cached[0] < self.settings.robots_cache_seconds:
            return cached[1]
        parser: RobotFileParser | None = RobotFileParser()
        await self.limiter.acquire()
        try:
            resp = await self._client.get(f"{origin}/robots.txt")
        except httpx.HTTPError as exc:
            # Cannot determine policy right now: don't cache, fail this check.
            raise RetryableError(f"robots.txt fetch failed: {exc}") from exc
        if resp.status_code in (401, 403):
            parser.disallow_all = True
        elif resp.status_code >= 500:
            raise RetryableError(f"robots.txt returned HTTP {resp.status_code}")
        elif resp.status_code >= 400:
            parser = None  # no robots.txt -> no restrictions expressed
        else:
            parser.parse(resp.text.splitlines())
        self._robots[origin] = (time.monotonic(), parser)
        return parser

    async def ensure_allowed(self, url: str) -> None:
        parser = await self._robots_for(url)
        if parser is not None and not parser.can_fetch(self.user_agent, url):
            reason = f"robots.txt disallows {urlsplit(url).path} for our user agent"
            self.limiter.record_not_permitted(reason)
            raise AccessNotPermittedError(reason)

    # -- requests --------------------------------------------------------------------
    async def get(self, url: str, *, params: dict | None = None) -> HttpResult:
        async def attempt() -> HttpResult:
            await self.limiter.acquire()
            start = time.perf_counter()
            try:
                resp = await self._client.get(url, params=params)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                raise RetryableError(f"{type(exc).__name__}: {exc}") from exc
            elapsed = int((time.perf_counter() - start) * 1000)
            return self._classify(url, resp, elapsed)

        def on_retry(n: int, exc: BaseException, delay: float) -> None:
            log_event(logger, "request_retry", logging.WARNING, retailer=self.retailer,
                      attempt=n, delay_s=round(delay, 1), error=str(exc))

        try:
            await retry_async(
                lambda: self.ensure_allowed(url),
                max_retries=self.settings.max_retries,
                base_delay=self.settings.retry_base_delay_seconds,
                max_delay=self.settings.retry_max_delay_seconds,
                on_retry=on_retry,
            )
            result = await retry_async(
                attempt,
                max_retries=self.settings.max_retries,
                base_delay=self.settings.retry_base_delay_seconds,
                max_delay=self.settings.retry_max_delay_seconds,
                on_retry=on_retry,
            )
        except RetryableError as exc:
            self.limiter.record_failure(str(exc))
            raise HttpStatusError(f"transient failure after retries: {exc}", 0) from exc
        except HttpStatusError as exc:
            self.limiter.record_failure(str(exc))
            raise
        self.limiter.record_success()
        return result

    def _classify(self, url: str, resp: httpx.Response, elapsed_ms: int) -> HttpResult:
        status = resp.status_code
        if status == 429:
            retry_after = parse_retry_after(resp.headers.get("Retry-After"))
            delay = self.limiter.record_rate_limited(retry_after)
            log_event(logger, "rate_limited", logging.WARNING, retailer=self.retailer,
                      url=url, retry_after=retry_after, pause_s=round(delay, 1))
            raise RateLimitedError(f"HTTP 429 from {self.retailer}", retry_after=retry_after)
        if status in RETRYABLE_STATUS:
            raise RetryableError(f"HTTP {status}")
        text = resp.text
        # Some retailers (e.g. Walmart) answer a bot block with HTTP 412 or a redirect to a
        # /blocked page instead of 403.
        blocked_redirect = urlsplit(str(resp.url)).path.startswith("/blocked")
        if status in BLOCK_STATUS or blocked_redirect or (status == 200 and looks_like_challenge(text)):
            reason = f"HTTP {status}: retailer denied access or served a bot challenge"
            self.limiter.record_blocked(reason)
            log_event(logger, "bot_challenge_or_forbidden", logging.ERROR, retailer=self.retailer,
                      url=url, http_status=status, action="pausing retailer; no bypass attempted")
            raise BotChallengeError(reason)
        if status >= 400 and status != 404:
            raise HttpStatusError(f"HTTP {status}", status)
        return HttpResult(url=str(resp.url), status_code=status, text=text,
                          headers=dict(resp.headers), elapsed_ms=elapsed_ms)
