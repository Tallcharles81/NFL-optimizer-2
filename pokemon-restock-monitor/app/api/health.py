"""GET /health -- makes it obvious when the monitor stops working."""

from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy import func, select

from app.api.deps import get_runtime
from app.database import utcnow
from app.models import InventoryState, Product, Retailer, RetailerHealth
from app.runtime import Runtime

router = APIRouter()

BAD_RETAILER_HEALTH = {RetailerHealth.BLOCKED.value, RetailerHealth.SUSPENDED.value,
                       RetailerHealth.RATE_LIMITED.value, RetailerHealth.NOT_PERMITTED.value}


def build_health(rt: Runtime) -> tuple[dict, int]:
    problems: list[str] = []
    db_ok = rt.db.ping()
    if not db_ok:
        problems.append("database unreachable")
        return {"status": "down", "database_status": "error", "problems": problems}, 503

    now = utcnow()
    with rt.db.session() as s:
        retailers = list(s.scalars(select(Retailer).order_by(Retailer.slug)))
        enabled_products = s.scalar(select(func.count(Product.id)).where(Product.enabled.is_(True))) or 0
        last_success = s.scalar(select(func.max(InventoryState.last_success_at)))
        used = set(s.scalars(select(Retailer.slug).join(Product).where(Product.enabled.is_(True))))
    retailer_status = {}
    for r in retailers:
        paused = r.paused_until is not None and r.paused_until > now
        retailer_status[r.slug] = {
            "enabled": r.enabled,
            "health": r.health,
            "monitored_products": r.slug in used,
            "paused_until": r.paused_until.isoformat() if paused else None,
            "pause_reason": r.pause_reason if paused or r.health == RetailerHealth.NOT_PERMITTED.value else None,
            "last_success_at": r.last_success_at.isoformat() if r.last_success_at else None,
            "last_error": r.last_error,
        }
        if r.slug in used and r.health in BAD_RETAILER_HEALTH:
            problems.append(f"retailer {r.slug} is {r.health}")

    sched = rt.scheduler.status()
    if rt.settings.monitor_enabled:
        if not sched["running"]:
            problems.append("scheduler not running")
        elif not sched["healthy"]:
            problems.append("scheduler not ticking")
    stale_after = timedelta(minutes=rt.settings.stall_alert_minutes)
    if enabled_products and rt.settings.monitor_enabled and sched["started_at"]:
        if last_success is None or now - last_success > stale_after:
            problems.append("no successful inventory check recently")
    last_notification = rt.notifications.last_notification_at()

    status = "ok" if not problems else "degraded"
    if rt.settings.monitor_enabled and not sched["running"]:
        status = "down"
    body = {
        "status": status,
        "problems": problems,
        "database_status": "ok",
        "scheduler_status": "disabled" if not rt.settings.monitor_enabled else (
            "running" if sched["healthy"] else ("stalled" if sched["running"] else "stopped")),
        "scheduler": sched,
        "retailer_status": retailer_status,
        "enabled_products": enabled_products,
        "last_successful_check": last_success.isoformat() if last_success else None,
        "last_notification": last_notification.isoformat() if last_notification else None,
        "notification_channels": rt.notifications.status()["channels"],
        "time": now.isoformat(),
    }
    return body, 200 if status != "down" else 503


@router.get("/health")
def health(rt: Runtime = Depends(get_runtime)):
    body, code = build_health(rt)
    return JSONResponse(body, status_code=code)
