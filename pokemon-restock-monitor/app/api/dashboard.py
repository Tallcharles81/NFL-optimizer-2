"""/dashboard and /settings pages plus /api/metrics."""

from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import get_runtime, get_session, require_auth, templates
from app.api.health import build_health
from app.api.retailers import retailer_rows
from app.database import utcnow
from app.models import AlertState, Event, EventType, InventoryState
from app.runtime import Runtime
from app.services.analytics_service import latency_metrics, restock_analytics
from app.services.product_service import list_products, list_stores

router = APIRouter(dependencies=[Depends(require_auth)])


@router.get("/")
def root():
    return RedirectResponse("/dashboard")


@router.get("/api/metrics")
def metrics(session: Session = Depends(get_session), rt: Runtime = Depends(get_runtime)):
    return {"latency": latency_metrics(session),
            "analytics": restock_analytics(session, rt.settings, include_simulation=True)}


@router.get("/dashboard")
def dashboard(request: Request, session: Session = Depends(get_session), rt: Runtime = Depends(get_runtime)):
    now = utcnow()
    products = list_products(session)
    available = [p for p in products if p.state_row and p.state_row.episode_open
                 and p.state_row.alert_state == AlertState.CONFIRMED.value]
    erroring = [p for p in products if p.state_row and p.state_row.consecutive_errors > 0]
    recent_restocks = list(session.scalars(
        select(Event).where(Event.event_type == EventType.RESTOCK_CONFIRMED.value)
        .order_by(Event.created_at.desc()).limit(10)))
    recent_errors = list(session.scalars(
        select(Event).where(Event.event_type.in_([
            EventType.CHECK_ERROR.value, EventType.RATE_LIMITED.value, EventType.RETAILER_BLOCKED.value,
            EventType.RETAILER_SUSPENDED.value, EventType.MONITOR_STALLED.value]))
        .order_by(Event.created_at.desc()).limit(8)))
    restocks_24h = session.scalar(select(func.count(Event.id)).where(
        Event.event_type == EventType.RESTOCK_CONFIRMED.value, Event.created_at >= now - timedelta(hours=24)))
    false_pos_24h = session.scalar(select(func.count(Event.id)).where(
        Event.event_type == EventType.FALSE_POSITIVE.value, Event.created_at >= now - timedelta(hours=24)))
    last_success = session.scalar(select(func.max(InventoryState.last_success_at)))
    names = {p.id: p for p in products}
    health, _ = build_health(rt)
    return templates.TemplateResponse(request, "dashboard.html", {
        "health": health,
        "products": products,
        "enabled_count": sum(1 for p in products if p.enabled),
        "available": available,
        "erroring": erroring,
        "recent_restocks": recent_restocks,
        "recent_errors": recent_errors,
        "restocks_24h": restocks_24h,
        "false_pos_24h": false_pos_24h,
        "last_success": last_success,
        "names": names,
        "retailers": [r for r in retailer_rows(session, rt) if r["products"] or r["health"] not in ("IDLE",)],
        "notif": rt.notifications.status(),
        "last_notification": rt.notifications.last_notification_at(),
        "latency": latency_metrics(session),
        "analytics": restock_analytics(session, rt.settings),
    })


@router.get("/settings")
def settings_page(request: Request, session: Session = Depends(get_session), rt: Runtime = Depends(get_runtime),
                  msg: str | None = None):
    return templates.TemplateResponse(request, "settings.html", {
        "settings": rt.settings.public_dict(),
        "stores": list_stores(session),
        "channels": rt.notifications.status(),
        "msg": msg,
    })


@router.post("/settings/test-notification")
async def test_notification(rt: Runtime = Depends(get_runtime)):
    report = await rt.notifications.send_test_message()
    text = f"Test sent via: {', '.join(report.sent) or 'none'}"
    if report.failed:
        text += f". Failed: {report.failed}"
    from urllib.parse import quote_plus

    return RedirectResponse(f"/settings?msg={quote_plus(text[:400])}", status_code=303)
