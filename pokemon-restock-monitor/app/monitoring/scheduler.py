"""Smart polling.

* NORMAL:           POLL_INTERVAL_SECONDS ± POLL_JITTER_SECONDS (default 60-120s)
* RECENTLY_ACTIVE:  RECENTLY_ACTIVE_INTERVAL_SECONDS ± jitter (default 30-60s)
                    for RECENTLY_ACTIVE_WINDOW_MINUTES after any stock change
* ERROR:            exponential backoff (ERROR_BACKOFF_BASE_SECONDS, doubling,
                    capped at ERROR_BACKOFF_MAX_SECONDS; Retry-After honored)
* AVAILABLE_HOLD:   after a confirmed restock we already alerted on, only a slow
                    AVAILABLE_HOLD_INTERVAL_SECONDS poll to notice the sell-out

Per-product (``products.poll_interval_seconds``) and per-retailer
(``retailers.poll_interval_seconds``) overrides replace the NORMAL base
interval. Nothing can poll a real retailer faster than
``ABSOLUTE_MIN_POLL_SECONDS``.

A single APScheduler job "ticks" every few seconds and dispatches products
whose persisted ``next_check_at`` is due, so schedules survive restarts.
"""

from __future__ import annotations

import asyncio
import logging
import random
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import func, or_, select

from app.config import ABSOLUTE_MIN_POLL_SECONDS, Settings
from app.database import Database, utcnow
from app.models import AlertState, EventType, InventoryState, Product, Retailer
from app.utils.logging import log_event
from app.utils.retry import backoff_delay

if TYPE_CHECKING:
    from app.monitoring.monitor import InventoryMonitor

logger = logging.getLogger("scheduler")


class PollingPolicy:
    def __init__(self, settings: Settings, rng: random.Random | None = None):
        self.s = settings
        self.rng = rng or random.Random()

    def _jittered(self, base: float, jitter: float) -> float:
        jitter = min(jitter, base / 2)
        return self.rng.uniform(base - jitter, base + jitter)

    def base_interval(self, product, retailer_row: Retailer | None) -> float:
        if getattr(product, "poll_interval_seconds", None):
            return product.poll_interval_seconds
        if retailer_row is not None and retailer_row.poll_interval_seconds:
            return retailer_row.poll_interval_seconds
        return self.s.poll_interval_seconds

    def next_interval(self, state: InventoryState, product, retailer_row: Retailer | None,
                      retailer_cls=None, now: datetime | None = None, retry_after: float | None = None) -> float:
        now = now or utcnow()
        base = self.base_interval(product, retailer_row)
        if state.consecutive_errors > 0:
            seconds = backoff_delay(state.consecutive_errors, self.s.error_backoff_base_seconds,
                                    self.s.error_backoff_max_seconds)
            if retry_after:
                seconds = max(seconds, retry_after)
        elif state.episode_open and state.alert_state == AlertState.CONFIRMED.value:
            seconds = max(self.s.available_hold_interval_seconds, base)
        elif state.recently_active_until and now < state.recently_active_until:
            seconds = min(self._jittered(self.s.recently_active_interval_seconds,
                                         self.s.recently_active_jitter_seconds), base)
        else:
            seconds = self._jittered(base, self.s.poll_jitter_seconds)
        if not getattr(retailer_cls, "exempt_from_poll_floor", False):
            seconds = max(seconds, ABSOLUTE_MIN_POLL_SECONDS)
        return seconds


class MonitorScheduler:
    def __init__(self, settings: Settings, db: Database, monitor: "InventoryMonitor"):
        self.settings = settings
        self.db = db
        self.monitor = monitor
        self._scheduler: AsyncIOScheduler | None = None
        self._sem = asyncio.Semaphore(settings.max_concurrent_checks)
        self._in_flight: set[int] = set()
        self._tasks: set[asyncio.Task] = set()
        self.started_at: datetime | None = None
        self.last_tick_at: datetime | None = None
        self.last_tick_error: str | None = None
        self.recovery_summary: dict | None = None
        self.checks_dispatched = 0

    async def start(self) -> None:
        self.recovery_summary = await self.monitor.recover()
        self._scheduler = AsyncIOScheduler(timezone="UTC")
        self._scheduler.add_job(self.tick, "interval", seconds=self.settings.scheduler_tick_seconds,
                                id="monitor-tick", max_instances=1, coalesce=True,
                                next_run_time=datetime.now().astimezone())
        self._scheduler.start()
        self.started_at = utcnow()
        log_event(logger, "scheduler_started", tick_seconds=self.settings.scheduler_tick_seconds,
                  max_concurrent_checks=self.settings.max_concurrent_checks)

    async def shutdown(self) -> None:
        if self._scheduler and self._scheduler.running:
            self._scheduler.shutdown(wait=False)
        if self._tasks:
            done, pending = await asyncio.wait(self._tasks, timeout=5)
            for t in pending:
                t.cancel()
        log_event(logger, "scheduler_stopped")

    @property
    def running(self) -> bool:
        return bool(self._scheduler and self._scheduler.running)

    def due_product_ids(self, now: datetime, limit: int = 100) -> list[int]:
        with self.db.session() as s:
            q = (
                select(Product.id)
                .join(Retailer, Retailer.id == Product.retailer_id)
                .outerjoin(InventoryState, InventoryState.product_id == Product.id)
                .where(Product.enabled.is_(True), Retailer.enabled.is_(True))
                .where(or_(InventoryState.next_check_at.is_(None), InventoryState.next_check_at <= now))
                .order_by(InventoryState.next_check_at.asc().nulls_first())
                .limit(limit)
            )
            return list(s.scalars(q))

    async def tick(self) -> None:
        now = utcnow()
        self.last_tick_at = now
        try:
            for pid in self.due_product_ids(now):
                if pid in self._in_flight:
                    continue
                self._in_flight.add(pid)
                task = asyncio.create_task(self._run(pid))
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)
                self.checks_dispatched += 1
            await self._watchdog(now)
            self.last_tick_error = None
        except Exception as exc:  # noqa: BLE001 - the scheduler must keep ticking
            self.last_tick_error = str(exc)
            logger.exception("Scheduler tick failed")

    async def _run(self, product_id: int) -> None:
        try:
            async with self._sem:
                await self.monitor.check_product(product_id)
        except Exception:  # noqa: BLE001
            logger.exception("Check failed for product %s", product_id)
        finally:
            self._in_flight.discard(product_id)

    async def _watchdog(self, now: datetime) -> None:
        """Make it obvious when monitoring has silently stopped working."""
        if not self.started_at or now - self.started_at < timedelta(minutes=self.settings.stall_alert_minutes):
            return
        with self.db.session() as s:
            enabled = s.scalar(select(func.count(Product.id)).where(Product.enabled.is_(True))) or 0
        if not enabled:
            return
        last = self.monitor.last_successful_check_at
        if last is None or now - last > timedelta(minutes=self.settings.stall_alert_minutes):
            from app.services.inventory_service import add_event

            key = f"stall:{now:%Y%m%d%H}"
            report = await self.monitor.notifications.send_system_alert(
                key, "Restock monitor is not getting successful checks",
                f"No successful inventory check since {last.isoformat() if last else 'startup'}. "
                "Check /health and the Retailers page (a retailer may be rate limiting or blocking).")
            if report.sent:
                with self.db.session() as s:
                    add_event(s, EventType.MONITOR_STALLED, None,
                              message=f"No successful check since {last.isoformat() if last else 'startup'}")

    def is_healthy(self) -> bool:
        if not self.running or self.last_tick_at is None:
            return False
        return utcnow() - self.last_tick_at < timedelta(seconds=max(30, self.settings.scheduler_tick_seconds * 6))

    def status(self) -> dict:
        return {
            "running": self.running,
            "healthy": self.is_healthy(),
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "last_tick_at": self.last_tick_at.isoformat() if self.last_tick_at else None,
            "last_tick_error": self.last_tick_error,
            "in_flight": sorted(self._in_flight),
            "checks_dispatched": self.checks_dispatched,
            "recovery": self.recovery_summary,
        }
