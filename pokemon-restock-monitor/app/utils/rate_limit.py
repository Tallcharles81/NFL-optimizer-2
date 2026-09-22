"""Per-retailer rate limiter and circuit breaker.

Guarantees:
* a minimum delay between any two requests to the same retailer;
* HTTP 429 pauses the retailer, honouring ``Retry-After`` when supplied;
* repeated failures open a circuit breaker (retailer SUSPENDED);
* a CAPTCHA / bot challenge / 403 pauses the retailer for a long cooldown
  (BLOCKED). We never try to get around it;
* robots.txt disallow marks the retailer NOT_PERMITTED for that URL.

Nothing here tries to evade a limit: no proxies, no identity rotation.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

from app.models.enums import RetailerHealth
from app.utils.logging import log_event
from app.utils.retry import backoff_delay

logger = logging.getLogger("rate_limit")


class MonitorRequestError(Exception):
    """Base class for request errors that the monitor maps to an ERROR check."""

    kind = "REQUEST_ERROR"


class RateLimitedError(MonitorRequestError):
    kind = "RATE_LIMITED"

    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


class RetailerUnavailableError(MonitorRequestError):
    """The limiter is paused for this retailer; no request was sent."""

    kind = "RETAILER_PAUSED"

    def __init__(self, message: str, until: datetime | None, health: str):
        super().__init__(message)
        self.until = until
        self.health = health


class BotChallengeError(MonitorRequestError):
    kind = "BOT_CHALLENGE"


class AccessNotPermittedError(MonitorRequestError):
    kind = "NOT_PERMITTED"


class HttpStatusError(MonitorRequestError):
    kind = "HTTP_ERROR"

    def __init__(self, message: str, status_code: int):
        super().__init__(message)
        self.status_code = status_code


def parse_retry_after(value: str | None, now: datetime | None = None) -> float | None:
    """Parse a Retry-After header (delta-seconds or HTTP-date) into seconds."""
    if not value:
        return None
    value = value.strip()
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    now = now or datetime.now(timezone.utc)
    return max(0.0, (when - now).total_seconds())


class RateLimiter:
    def __init__(
        self,
        name: str,
        *,
        min_interval: float,
        circuit_threshold: int = 5,
        circuit_cooldown: float = 1800,
        blocked_cooldown: float = 21600,
        backoff_base: float = 60,
        backoff_max: float = 3600,
        monotonic: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        self.name = name
        self.min_interval = min_interval
        self.circuit_threshold = circuit_threshold
        self.circuit_cooldown = circuit_cooldown
        self.blocked_cooldown = blocked_cooldown
        self.backoff_base = backoff_base
        self.backoff_max = backoff_max
        self._monotonic = monotonic
        self._now = now
        self._sleep = sleep
        self._lock = asyncio.Lock()
        self._last_request: float | None = None

        self.consecutive_failures = 0
        self.paused_until: datetime | None = None
        self.pause_reason: str | None = None
        self.health: str = RetailerHealth.IDLE.value
        self.total_requests = 0
        self.rate_limit_events = 0

    # -- state ---------------------------------------------------------------
    def is_paused(self) -> bool:
        return self.paused_until is not None and self._now() < self.paused_until

    def seconds_until_available(self) -> float:
        if not self.is_paused():
            return 0.0
        return (self.paused_until - self._now()).total_seconds()

    def _pause(self, seconds: float, health: RetailerHealth, reason: str) -> None:
        until = self._now() + timedelta(seconds=seconds)
        if self.paused_until is None or until > self.paused_until:
            self.paused_until = until
        self.pause_reason = reason
        self.health = health.value
        log_event(
            logger,
            "retailer_paused",
            logging.WARNING,
            retailer=self.name,
            health=health.value,
            pause_seconds=round(seconds, 1),
            until=self.paused_until.isoformat(),
            reason=reason,
        )

    def restore(self, paused_until: datetime | None, health: str | None, reason: str | None, failures: int) -> None:
        """Restore persisted state after a restart."""
        self.consecutive_failures = failures or 0
        if paused_until and paused_until > self._now():
            self.paused_until = paused_until
            self.pause_reason = reason
            self.health = health or RetailerHealth.SUSPENDED.value

    def resume(self) -> None:
        """Manual override from the dashboard."""
        self.paused_until = None
        self.pause_reason = None
        self.consecutive_failures = 0
        self.health = RetailerHealth.IDLE.value

    # -- request gating ------------------------------------------------------
    async def acquire(self) -> None:
        """Wait for our turn. Raises RetailerUnavailableError while paused."""
        async with self._lock:
            if self.is_paused():
                raise RetailerUnavailableError(
                    f"{self.name} paused until {self.paused_until.isoformat()} ({self.pause_reason})",
                    self.paused_until,
                    self.health,
                )
            if self._last_request is not None:
                wait = self._last_request + self.min_interval - self._monotonic()
                if wait > 0:
                    await self._sleep(wait)
            self._last_request = self._monotonic()
            self.total_requests += 1

    # -- outcomes --------------------------------------------------------------
    def record_success(self) -> None:
        self.consecutive_failures = 0
        self.health = RetailerHealth.OK.value
        if self.paused_until and not self.is_paused():
            self.paused_until = None
            self.pause_reason = None

    def record_failure(self, reason: str) -> None:
        self.consecutive_failures += 1
        if self.consecutive_failures >= self.circuit_threshold:
            self._pause(
                self.circuit_cooldown,
                RetailerHealth.SUSPENDED,
                f"circuit breaker: {self.consecutive_failures} consecutive failures (last: {reason})",
            )
        else:
            self.health = RetailerHealth.DEGRADED.value

    def record_rate_limited(self, retry_after: float | None) -> float:
        self.consecutive_failures += 1
        self.rate_limit_events += 1
        if retry_after is not None:
            delay = max(retry_after, self.min_interval)
            reason = f"HTTP 429, Retry-After={retry_after:.0f}s"
        else:
            delay = backoff_delay(self.consecutive_failures, self.backoff_base, self.backoff_max)
            reason = f"HTTP 429, no Retry-After; exponential backoff {delay:.0f}s"
        self._pause(delay, RetailerHealth.RATE_LIMITED, reason)
        if self.consecutive_failures >= self.circuit_threshold:
            self._pause(self.circuit_cooldown, RetailerHealth.SUSPENDED, "circuit breaker after repeated 429s")
        return delay

    def record_blocked(self, reason: str) -> None:
        self.consecutive_failures += 1
        self._pause(self.blocked_cooldown, RetailerHealth.BLOCKED, reason)

    def record_not_permitted(self, reason: str) -> None:
        # robots.txt is per-URL; do not pause the whole retailer, just flag it.
        self.health = RetailerHealth.NOT_PERMITTED.value
        self.pause_reason = reason

    def snapshot(self) -> dict:
        return {
            "name": self.name,
            "health": self.health,
            "paused": self.is_paused(),
            "paused_until": self.paused_until.isoformat() if self.is_paused() else None,
            "pause_reason": self.pause_reason if self.is_paused() or self.health == "NOT_PERMITTED" else None,
            "consecutive_failures": self.consecutive_failures,
            "min_interval_seconds": self.min_interval,
            "total_requests": self.total_requests,
            "rate_limit_events": self.rate_limit_events,
        }
