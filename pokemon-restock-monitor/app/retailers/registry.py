"""Retailer registry and per-retailer runtime objects (limiter + HTTP client)."""

from __future__ import annotations

import httpx

from app.config import Settings
from app.retailers.base import RetailerMonitor
from app.retailers.cvs import CVSMonitor
from app.retailers.five_below import FiveBelowMonitor
from app.retailers.gamestop import GameStopMonitor
from app.retailers.hobby_lobby import HobbyLobbyMonitor
from app.retailers.http_client import PoliteHttpClient
from app.retailers.simulated import SimulatedRetailer
from app.retailers.target import TargetMonitor
from app.retailers.walgreens import WalgreensMonitor
from app.retailers.walmart import WalmartMonitor
from app.utils.rate_limit import RateLimiter

RETAILER_CLASSES: dict[str, type[RetailerMonitor]] = {
    cls.slug: cls
    for cls in (
        TargetMonitor,
        WalmartMonitor,
        GameStopMonitor,
        FiveBelowMonitor,
        HobbyLobbyMonitor,
        CVSMonitor,
        WalgreensMonitor,
        SimulatedRetailer,
    )
}


def get_retailer_class(slug: str) -> type[RetailerMonitor]:
    try:
        return RETAILER_CLASSES[slug.lower()]
    except KeyError:
        raise KeyError(f"Unknown retailer {slug!r}. Known: {', '.join(sorted(RETAILER_CLASSES))}") from None


class RetailerManager:
    """Lazily builds one RetailerMonitor (with its own limiter) per retailer."""

    def __init__(self, settings: Settings, overrides: dict[str, RetailerMonitor] | None = None,
                 transport: httpx.AsyncBaseTransport | None = None):
        self.settings = settings
        self.transport = transport
        self._monitors: dict[str, RetailerMonitor] = dict(overrides or {})
        self._min_interval_overrides: dict[str, float] = {}

    def make_limiter(self, slug: str) -> RateLimiter:
        s = self.settings
        return RateLimiter(
            slug,
            min_interval=self._min_interval_overrides.get(slug, s.min_request_interval_seconds),
            circuit_threshold=s.circuit_breaker_threshold,
            circuit_cooldown=s.circuit_breaker_cooldown_seconds,
            blocked_cooldown=s.blocked_cooldown_seconds,
            backoff_base=s.error_backoff_base_seconds,
            backoff_max=s.error_backoff_max_seconds,
        )

    def get(self, slug: str) -> RetailerMonitor:
        slug = slug.lower()
        if slug not in self._monitors:
            cls = get_retailer_class(slug)
            limiter = self.make_limiter(slug)
            http = None if cls is SimulatedRetailer else PoliteHttpClient(slug, limiter, self.settings, self.transport)
            self._monitors[slug] = cls(self.settings, limiter, http)
        return self._monitors[slug]

    def set_min_interval(self, slug: str, seconds: float | None) -> None:
        if seconds is None:
            return
        # Never allow a per-retailer override below 1s for a real retailer.
        cls = get_retailer_class(slug)
        if not cls.exempt_from_poll_floor:
            seconds = max(seconds, 1.0)
        self._min_interval_overrides[slug] = seconds
        if slug in self._monitors:
            self._monitors[slug].limiter.min_interval = seconds

    def active(self) -> dict[str, RetailerMonitor]:
        return dict(self._monitors)

    async def aclose(self) -> None:
        for monitor in self._monitors.values():
            await monitor.aclose()
