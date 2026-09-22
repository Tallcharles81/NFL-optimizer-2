"""Retry helper with exponential backoff.

Only exceptions that the caller explicitly classifies as retryable are
retried. Rate-limit (429), bot-challenge and permission errors are *never*
retried here -- the rate limiter pauses the retailer instead.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import TypeVar

T = TypeVar("T")


class RetryableError(Exception):
    """Raised for transient failures (timeouts, connection errors, 5xx)."""


def backoff_delay(attempt: int, base: float, maximum: float) -> float:
    """attempt=1 -> base, 2 -> 2*base, 3 -> 4*base ... capped at maximum."""
    return min(base * (2 ** max(attempt - 1, 0)), maximum)


async def retry_async(
    fn: Callable[[], Awaitable[T]],
    *,
    max_retries: int,
    base_delay: float,
    max_delay: float,
    retry_on: Callable[[BaseException], bool] = lambda e: isinstance(e, RetryableError),
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    on_retry: Callable[[int, BaseException, float], None] | None = None,
) -> T:
    attempt = 0
    while True:
        try:
            return await fn()
        except BaseException as exc:  # noqa: BLE001 - classified below
            if not retry_on(exc) or attempt >= max_retries:
                raise
            attempt += 1
            delay = backoff_delay(attempt, base_delay, max_delay)
            if on_retry:
                on_retry(attempt, exc, delay)
            await sleep(delay)
