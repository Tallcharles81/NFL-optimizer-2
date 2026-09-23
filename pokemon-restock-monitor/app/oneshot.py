"""One-shot mode: check every product once, verify, alert, exit.

    python -m app.main --once [--catalog catalog/target_30th_celebration.csv] [--loop-minutes 20]

Built for schedulers such as GitHub Actions (no always-on computer needed).
All state lives in the SQLite database, which the workflow saves between
runs, so de-duplication, restock episodes, error backoff and retailer
pauses carry over from one run to the next.

If a retailer can't be read at all during a run (every check errored or came
back UNKNOWN), a system alert is sent at most once per day so the monitor
never fails silently.
"""

from __future__ import annotations

import asyncio
import logging
import random
from collections import defaultdict
from datetime import timedelta

from sqlalchemy import delete, select, text

from app.config import Settings
from app.database import utcnow
from app.models import Event, EventType, InventoryCheck, InventoryState, InventoryStatus, Product, Retailer
from app.runtime import Runtime, build_runtime
from app.services.product_service import import_catalog
from app.utils.logging import log_event

logger = logging.getLogger("oneshot")

KEEP_CHECK_HISTORY_DAYS = 14


async def run_once(settings: Settings, catalog: str | list[str] | None = None, runtime: Runtime | None = None,
                   is_test: bool = False, loop_minutes: float = 0, sweep_seconds: float = 75,
                   concurrency: int = 1, sleep=asyncio.sleep) -> dict:
    """Check every product once -- or, with ``loop_minutes``, keep sweeping for that long.

    A sweep checks all products; a new sweep starts every ``sweep_seconds`` (± 20 %),
    or right away if a sweep took longer. ``concurrency`` products per retailer are checked
    at once (each retailer's rate limiter still spaces request starts). ``is_test`` labels
    alerts [SIMULATION].
    """
    rt = runtime or build_runtime(settings, is_simulation=is_test)
    summary = {"sweeps": 0, "checked": 0, "skipped": 0, "restocks": 0}
    try:
        for path in ([catalog] if isinstance(catalog, str) else catalog or []):
            with rt.db.session() as s:
                result = import_catalog(s, path)
                for p in result["added"]:
                    log_event(logger, "product_imported", sku=p.sku, name=p.product_name)
                for err in result["errors"]:
                    log_event(logger, "catalog_error", logging.ERROR, error=err)

        await rt.monitor.recover()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + loop_minutes * 60
        rng = random.Random()
        while True:
            started = loop.time()
            await _sweep(rt, summary, concurrency)
            summary["sweeps"] += 1
            _prune(rt)
            if loop_minutes <= 0:
                break
            wait = max(0.0, sweep_seconds * rng.uniform(0.8, 1.2) - (loop.time() - started))
            if loop.time() + wait >= deadline:
                break
            await sleep(wait)
        log_event(logger, "run_complete", sweeps=summary["sweeps"], checked=summary["checked"],
                  skipped=summary["skipped"], restocks=summary["restocks"])
        return summary
    finally:
        if runtime is None:
            await rt.aclose()
            # Closing the last connection checkpoints the WAL into restock.db, so the
            # workflow can save a single self-contained file even after a failure.
            rt.db.dispose()


async def _sweep(rt: Runtime, summary: dict, concurrency: int = 1) -> None:
    now = utcnow()
    with rt.db.session() as s:
        rows = s.execute(
            select(Product.id, Retailer.slug, InventoryState.consecutive_errors, InventoryState.next_check_at,
                   InventoryState.last_checked_at)
            .join(Retailer, Retailer.id == Product.retailer_id)
            .outerjoin(InventoryState, InventoryState.product_id == Product.id)
            .where(Product.enabled.is_(True), Retailer.enabled.is_(True))
            .order_by(Product.id)
        ).all()
    rows = _limit_per_retailer(rows, rt.settings.checks_per_sweep_limits(), summary)
    by_retailer = defaultdict(list)
    # Each retailer gets its own slots, so a slow retailer never holds up another.
    sems = defaultdict(lambda: asyncio.Semaphore(max(1, concurrency)))

    async def check(pid: int, slug: str) -> None:
        async with sems[slug]:
            outcome = await rt.monitor.check_product(pid)
        if outcome.skipped:
            summary["skipped"] += 1
            return
        summary["checked"] += 1
        by_retailer[slug].append(outcome.observation)
        if outcome.confirmed_event_id:
            summary["restocks"] += 1

    tasks = []
    for pid, slug, errors, next_check, _last in rows:
        # Respect per-product error backoff across sweeps and runs.
        if errors and next_check and next_check > now:
            summary["skipped"] += 1
            continue
        tasks.append(check(pid, slug))
    await asyncio.gather(*tasks)
    await _health_alerts(rt, by_retailer)


def _limit_per_retailer(rows, limits: dict[str, int], summary: dict) -> list:
    """Keep at most ``limits[slug]`` products per limited retailer: the least recently checked."""
    if not limits:
        return list(rows)
    oldest_first = sorted(rows, key=lambda r: (r[4] is not None, r[4] or 0, r[0]))
    taken: dict[str, int] = defaultdict(int)
    keep = set()
    for row in oldest_first:
        slug = row[1]
        if slug in limits and taken[slug] >= limits[slug]:
            continue
        taken[slug] += 1
        keep.add(row[0])
    summary["skipped"] += len(rows) - len(keep)
    return [r for r in rows if r[0] in keep]


async def _health_alerts(rt: Runtime, by_retailer: dict) -> None:
    """One alert per retailer per day if nothing could be read this run."""
    today = utcnow().strftime("%Y%m%d")
    for slug, observations in by_retailer.items():
        readable = [o for o in observations if o.request_success and o.status != InventoryStatus.UNKNOWN]
        if readable or not observations:
            continue
        sample = next((o.error or o.message for o in observations if o.error or o.message), "unknown reason")
        await rt.notifications.send_system_alert(
            f"unreadable:{slug}:{today}",
            f"Restock monitor can't read {slug.title()} right now",
            f"None of {len(observations)} product checks returned a stock status, so restocks at "
            f"{slug.title()} would be missed. Reason: {sample}",
        )


def _prune(rt: Runtime) -> None:
    """Keep the saved database small."""
    cutoff = utcnow() - timedelta(days=KEEP_CHECK_HISTORY_DAYS)
    with rt.db.session() as s:
        s.execute(delete(InventoryCheck).where(InventoryCheck.checked_at < cutoff))
        # Every scheduled run logs a MONITOR_STARTED event; keep only a day of them.
        s.execute(delete(Event).where(Event.event_type == EventType.MONITOR_STARTED.value,
                                      Event.created_at < utcnow() - timedelta(days=1)))
    if rt.db.url.startswith("sqlite"):
        with rt.db.engine.connect() as conn:
            conn.execute(text("PRAGMA wal_checkpoint(TRUNCATE)"))
            conn.commit()
