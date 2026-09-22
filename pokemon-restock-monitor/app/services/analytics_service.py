"""Restock analytics and alert-latency metrics.

Historical statistics are only shown once ANALYTICS_MIN_EPISODES confirmed
restocks have been recorded, and are always labelled as history -- never as
predictions.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from statistics import mean
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import Event, EventType, Product

WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def _avg(values):
    values = [v for v in values if v is not None]
    return round(mean(values)) if values else None


def latency_metrics(session: Session, limit: int = 50) -> dict:
    events = list(session.scalars(
        select(Event).where(Event.event_type == EventType.RESTOCK_CONFIRMED.value)
        .order_by(Event.created_at.desc()).limit(limit)))
    last = events[0] if events else None
    keys = ["detection_latency_ms", "detection_window_ms", "verification_latency_ms",
            "notification_latency_ms", "total_latency_ms"]
    return {
        "samples": len(events),
        "average": {k: _avg(getattr(e, k) for e in events) for k in keys},
        "last": {k: getattr(last, k) for k in keys} if last else None,
    }


def restock_analytics(session: Session, settings: Settings, include_simulation: bool = False) -> dict:
    tz = ZoneInfo(settings.timezone)
    q = select(Event).where(Event.event_type.in_([EventType.RESTOCK_CONFIRMED.value, EventType.SOLD_OUT.value]))
    if not include_simulation:
        q = q.where(Event.is_simulation.is_(False))
    events = list(session.scalars(q.order_by(Event.created_at)))
    restocks = [e for e in events if e.event_type == EventType.RESTOCK_CONFIRMED.value]
    sellouts = [e for e in events if e.event_type == EventType.SOLD_OUT.value]
    n = len(restocks)
    result = {"episodes": n, "min_required": settings.analytics_min_episodes,
              "sufficient": n >= settings.analytics_min_episodes}
    if not result["sufficient"]:
        return result

    names = {p.id: p.product_name for p in session.scalars(select(Product))}
    by_product = defaultdict(list)
    for e in restocks:
        by_product[e.product_id].append(e.detected_at or e.created_at)
    gaps = []
    for times in by_product.values():
        gaps += [(b - a).total_seconds() / 86400 for a, b in zip(times, times[1:])]
    durations = defaultdict(list)
    for e in sellouts:
        secs = (e.details or {}).get("available_for_seconds")
        if secs is not None:
            durations[e.product_id].append(secs)
    all_durations = [d for v in durations.values() for d in v]
    stores = Counter(f"{e.retailer}:{e.store_id or 'online'}" for e in restocks)
    hours = Counter((e.detected_at or e.created_at).astimezone(tz).hour for e in restocks)
    days = Counter(WEEKDAYS[(e.detected_at or e.created_at).astimezone(tz).weekday()] for e in restocks)
    fastest = sorted(((names.get(pid, str(pid)), mean(v)) for pid, v in durations.items()), key=lambda x: x[1])
    result.update({
        "avg_days_between_restocks": round(mean(gaps), 2) if gaps else None,
        "avg_minutes_available": round(mean(all_durations) / 60, 1) if all_durations else None,
        "top_stores": stores.most_common(5),
        "fastest_sellouts": [(name, round(secs / 60, 1)) for name, secs in fastest[:5]],
        "restocks_by_hour": sorted(hours.items()),
        "restocks_by_weekday": [(d, days.get(d, 0)) for d in WEEKDAYS],
    })
    return result
