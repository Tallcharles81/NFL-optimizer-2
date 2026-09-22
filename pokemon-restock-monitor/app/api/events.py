"""Event history."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_session, require_auth, templates
from app.models import Event, EventType

router = APIRouter(dependencies=[Depends(require_auth)])


def query_events(session: Session, event_type: str | None, product_id: int | None, limit: int) -> list[Event]:
    q = select(Event).order_by(Event.created_at.desc(), Event.id.desc()).limit(min(limit, 1000))
    if event_type:
        q = q.where(Event.event_type == event_type)
    if product_id:
        q = q.where(Event.product_id == product_id)
    return list(session.scalars(q))


def event_dict(e: Event) -> dict:
    return {c.name: (v.isoformat() if hasattr(v, "isoformat") else v)
            for c in Event.__table__.columns for v in [getattr(e, c.name)]}


@router.get("/api/events")
def api_events(session: Session = Depends(get_session), type: str | None = None,
               product_id: int | None = None, limit: int = 100):
    return [event_dict(e) for e in query_events(session, type, product_id, limit)]


@router.get("/events")
def page(request: Request, session: Session = Depends(get_session), type: str | None = None,
         product_id: int | None = None, limit: int = 200):
    return templates.TemplateResponse(request, "events.html", {
        "events": query_events(session, type or None, product_id, limit),
        "types": [t.value for t in EventType],
        "selected": type or "",
    })
