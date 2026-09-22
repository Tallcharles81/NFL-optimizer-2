"""Rate limiting, Retry-After, retries, circuit breaker, bot challenges, polling policy (case 5)."""

from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import httpx
import pytest

from app.config import ABSOLUTE_MIN_POLL_SECONDS
from app.models import InventoryState, InventoryStatus as S
from app.monitoring.scheduler import PollingPolicy
from app.retailers.base import ProductRef
from app.retailers.registry import RetailerManager
from app.utils.rate_limit import RateLimiter, RetailerUnavailableError, parse_retry_after
from app.utils.retry import RetryableError, backoff_delay, retry_async

PRODUCT = ProductRef(id=1, retailer="target", sku="12345678", product_name="X")
PAGE = ('<script type="application/ld+json">{"@type":"Product","offers":{"@type":"Offer",'
        '"availability":"InStock","seller":{"name":"Target"}}}</script>')


class FakeClock:
    def __init__(self):
        self.mono = 0.0
        self.wall = datetime(2026, 9, 22, 16, 0, tzinfo=timezone.utc)
        self.slept = []

    def monotonic(self):
        return self.mono

    def now(self):
        return self.wall

    async def sleep(self, secs):
        self.slept.append(secs)
        self.mono += secs
        self.wall += timedelta(seconds=secs)


def limiter(clock, **kw):
    return RateLimiter("t", min_interval=kw.pop("min_interval", 5), monotonic=clock.monotonic, now=clock.now,
                       sleep=clock.sleep, **kw)


async def test_minimum_delay_between_requests():
    c = FakeClock()
    lim = limiter(c, min_interval=5)
    await lim.acquire()
    await lim.acquire()
    c.mono += 2
    await lim.acquire()
    assert c.slept == [5, 3]


async def test_429_with_retry_after_pauses_retailer():
    c = FakeClock()
    lim = limiter(c)
    lim.record_rate_limited(120)
    assert lim.is_paused() and lim.health == "RATE_LIMITED"
    with pytest.raises(RetailerUnavailableError):
        await lim.acquire()
    c.wall += timedelta(seconds=121)
    await lim.acquire()  # allowed again


async def test_429_without_retry_after_backs_off_exponentially():
    c = FakeClock()
    lim = limiter(c, backoff_base=60, backoff_max=3600, circuit_threshold=10)
    assert lim.record_rate_limited(None) == 60
    assert lim.record_rate_limited(None) == 120
    assert lim.record_rate_limited(None) == 240


def test_parse_retry_after_formats():
    assert parse_retry_after("30") == 30
    now = datetime(2026, 9, 22, 16, 0, tzinfo=timezone.utc)
    assert parse_retry_after(format_datetime(now + timedelta(seconds=90), usegmt=True), now) == 90
    assert parse_retry_after("garbage") is None and parse_retry_after(None) is None


async def test_circuit_breaker_after_repeated_failures():
    c = FakeClock()
    lim = limiter(c, circuit_threshold=3, circuit_cooldown=1800)
    for _ in range(2):
        lim.record_failure("HTTP 500")
    assert not lim.is_paused() and lim.health == "DEGRADED"
    lim.record_failure("HTTP 500")
    assert lim.is_paused() and lim.health == "SUSPENDED"
    assert 1799 <= lim.seconds_until_available() <= 1800
    lim.resume()
    assert not lim.is_paused()


async def test_retry_only_retryable_errors():
    calls = []

    async def flaky():
        calls.append(1)
        if len(calls) < 3:
            raise RetryableError("timeout")
        return "ok"

    async def nosleep(_):
        pass

    assert await retry_async(flaky, max_retries=3, base_delay=1, max_delay=10, sleep=nosleep) == "ok"

    async def bad():
        calls.append(2)
        raise ValueError("not retryable")

    calls.clear()
    with pytest.raises(ValueError):
        await retry_async(bad, max_retries=3, base_delay=1, max_delay=10, sleep=nosleep)
    assert len(calls) == 1
    assert [backoff_delay(i, 2, 30) for i in (1, 2, 3, 4, 5)] == [2, 4, 8, 16, 30]


def _manager(settings, handler):
    s = settings.model_copy(update={"retry_base_delay_seconds": 0, "retry_max_delay_seconds": 0})
    return RetailerManager(s, transport=httpx.MockTransport(handler))


def _robots_ok(request):
    return httpx.Response(200, text="User-agent: *\nAllow: /\n") if request.url.path == "/robots.txt" else None


async def test_http_429_is_not_retried_and_pauses(settings):
    hits = []

    def handler(request):
        if (r := _robots_ok(request)) is not None:
            return r
        hits.append(1)
        return httpx.Response(429, headers={"Retry-After": "300"})

    mgr = _manager(settings, handler)
    target = mgr.get("target")
    o = await target.safe_check(PRODUCT)
    assert o.status == S.ERROR and o.error_kind == "RATE_LIMITED" and o.retry_after == 300
    assert len(hits) == 1  # no hammering
    assert target.limiter.is_paused() and target.limiter.rate_limit_events == 1
    o2 = await target.safe_check(PRODUCT)
    assert o2.error_kind == "RETAILER_PAUSED" and len(hits) == 1
    await mgr.aclose()


async def test_http_5xx_retried_then_fails(settings):
    hits = []

    def handler(request):
        if (r := _robots_ok(request)) is not None:
            return r
        hits.append(1)
        return httpx.Response(503)

    mgr = _manager(settings, handler)
    o = await mgr.get("target").safe_check(PRODUCT)
    assert o.status == S.ERROR and o.error_kind == "HTTP_ERROR"
    assert len(hits) == 1 + settings.max_retries
    await mgr.aclose()


async def test_http_5xx_then_success(settings):
    hits = []

    def handler(request):
        if (r := _robots_ok(request)) is not None:
            return r
        hits.append(1)
        return httpx.Response(502) if len(hits) == 1 else httpx.Response(200, text=PAGE)

    mgr = _manager(settings, handler)
    o = await mgr.get("target").safe_check(PRODUCT)
    assert o.status == S.AVAILABLE and len(hits) == 2
    await mgr.aclose()


async def test_http_404_not_retried(settings):
    hits = []

    def handler(request):
        if (r := _robots_ok(request)) is not None:
            return r
        hits.append(1)
        return httpx.Response(404)

    mgr = _manager(settings, handler)
    o = await mgr.get("target").safe_check(PRODUCT)
    assert o.status == S.UNAVAILABLE and len(hits) == 1
    await mgr.aclose()


@pytest.mark.parametrize("status,body", [
    (403, "Forbidden"),
    (200, '<html><div id="px-captcha"></div>Press & Hold to confirm you are a human</html>'),
])
async def test_bot_challenge_stops_requests(settings, status, body):
    hits = []

    def handler(request):
        if (r := _robots_ok(request)) is not None:
            return r
        hits.append(1)
        return httpx.Response(status, text=body)

    mgr = _manager(settings, handler)
    target = mgr.get("target")
    o = await target.safe_check(PRODUCT)
    assert o.error_kind == "BOT_CHALLENGE"
    assert target.limiter.health == "BLOCKED" and target.limiter.is_paused()
    assert target.limiter.seconds_until_available() > settings.blocked_cooldown_seconds - 5
    await target.safe_check(PRODUCT)
    assert len(hits) == 1  # never retried, never "worked around"
    await mgr.aclose()


async def test_retailer_stops_after_repeated_failures(settings):
    hits = []

    def handler(request):
        if (r := _robots_ok(request)) is not None:
            return r
        hits.append(1)
        return httpx.Response(500)

    s = settings.model_copy(update={"max_retries": 0, "circuit_breaker_threshold": 3})
    mgr = _manager(s, handler)
    target = mgr.get("target")
    for _ in range(6):
        await target.safe_check(PRODUCT)
    assert len(hits) == 3 and target.limiter.health == "SUSPENDED"
    await mgr.aclose()


# -- polling policy -------------------------------------------------------------------
def _state(**kw):
    st = InventoryState(product_id=1)
    st.consecutive_errors = kw.get("errors", 0)
    st.episode_open = kw.get("open", False)
    st.alert_state = kw.get("alert", "NONE")
    st.recently_active_until = kw.get("recent")
    return st


class _Real:
    exempt_from_poll_floor = False


def test_polling_intervals(settings):
    pol = PollingPolicy(settings)
    now = datetime.now(timezone.utc)
    for _ in range(50):
        assert 60 <= pol.next_interval(_state(), PRODUCT, None, _Real, now) <= 120
        assert 30 <= pol.next_interval(_state(recent=now + timedelta(minutes=5)), PRODUCT, None, _Real, now) <= 60
    assert pol.next_interval(_state(errors=1), PRODUCT, None, _Real, now) == 60
    assert pol.next_interval(_state(errors=3), PRODUCT, None, _Real, now) == 240
    assert pol.next_interval(_state(errors=20), PRODUCT, None, _Real, now) == settings.error_backoff_max_seconds
    assert pol.next_interval(_state(errors=1), PRODUCT, None, _Real, now, retry_after=900) == 900
    assert pol.next_interval(_state(open=True, alert="CONFIRMED"), PRODUCT, None, _Real, now) == 300


def test_poll_floor_cannot_be_bypassed(settings):
    pol = PollingPolicy(settings)
    fast = ProductRef(id=1, retailer="target", sku="1", product_name="X", poll_interval_seconds=1)
    assert pol.next_interval(_state(), fast, None, _Real, datetime.now(timezone.utc)) >= ABSOLUTE_MIN_POLL_SECONDS
