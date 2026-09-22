"""One-shot mode: check every product once, verify, alert, exit.

    python -m app.main --once [--catalog catalog/target_30th_celebration.csv]

Built for schedulers such as GitHub Actions (no always-on computer needed).
All state lives in the SQLite database, which the workflow saves between
runs, so de-duplication, restock episodes, error backoff and retailer
pauses carry over from one run to the next.

If a retailer can't be read at all during a run (every check errored or came
back UNKNOWN), a system alert is sent at most once per day so the monitor
never fails silently.
"""

from __future__ import annotations

import logging
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


async def run_once(settings: Settings, catalog: str | None = None, runtime: Runtime | None = None,
                   is_test: bool = False) -> dict:
    """``is_test`` labels every alert [SIMULATION] (for live end-to-end tests)."""
    rt = runtime or build_runtime(settings, is_simulation=is_test)
    try:
        if catalog:
            with rt.db.session() as s:
                result = import_catalog(s, catalog)
                for p in result["added"]:
                    log_event(logger, "product_imported", sku=p.sku, name=p.product_name)
                for err in result["errors"]:
                    log_event(logger, "catalog_error", logging.ERROR, error=err)

        await rt.monitor.recover()

        now = utcnow()
        with rt.db.session() as s:
            rows = s.execute(
                select(Product.id, Retailer.slug, InventoryState.consecutive_errors, InventoryState.next_check_at)
                .join(Retailer, Retailer.id == Product.retailer_id)
                .outerjoin(InventoryState, InventoryState.product_id == Product.id)
                .where(Product.enabled.is_(True), Retailer.enabled.is_(True))
                .order_by(Product.id)
            ).all()

        summary = {"checked": 0, "skipped": 0, "restocks": 0, "by_retailer": defaultdict(list)}
        for pid, slug, errors, next_check in rows:
            # Respect per-product error backoff across runs.
            if errors and next_check and next_check > now:
                summary["skipped"] += 1
                continue
            outcome = await rt.monitor.check_product(pid)
            if outcome.skipped:
                summary["skipped"] += 1
                continue
            summary["checked"] += 1
            obs = outcome.observation
            summary["by_retailer"][slug].append(obs)
            if outcome.confirmed_event_id:
                summary["restocks"] += 1

        await _health_alerts(rt, summary["by_retailer"])
        _prune(rt)
        summary["by_retailer"] = {k: len(v) for k, v in summary["by_retailer"].items()}
        log_event(logger, "run_complete", checked=summary["checked"], skipped=summary["skipped"],
                  restocks=summary["restocks"])
        return summary
    finally:
        if runtime is None:
            await rt.aclose()
            # Closing the last connection checkpoints the WAL into restock.db, so the
            # workflow can save a single self-contained file even after a failure.
            rt.db.dispose()


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
