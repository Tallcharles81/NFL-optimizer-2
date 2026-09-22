"""Retailers and stores."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import get_runtime, get_session, require_auth, templates
from app.models import Product, Retailer
from app.retailers.registry import get_retailer_class
from app.runtime import Runtime
from app.services.product_service import add_store, list_stores

router = APIRouter(dependencies=[Depends(require_auth)])


def retailer_rows(session: Session, rt: Runtime) -> list[dict]:
    counts = dict(session.execute(
        select(Product.retailer_id, func.count(Product.id)).group_by(Product.retailer_id)).all())
    rows = []
    for r in session.scalars(select(Retailer).order_by(Retailer.name)):
        try:
            cls = get_retailer_class(r.slug)
        except KeyError:
            cls = None
        live = rt.retailers.active().get(r.slug)
        rows.append({
            "slug": r.slug,
            "name": r.name,
            "enabled": r.enabled,
            "health": r.health,
            "implementation_status": cls.implementation_status if cls else "unknown",
            "supports_store_inventory": cls.supports_store_inventory if cls else False,
            "products": counts.get(r.id, 0),
            "paused_until": r.paused_until,
            "pause_reason": r.pause_reason,
            "consecutive_failures": r.consecutive_failures,
            "last_success_at": r.last_success_at,
            "last_error": r.last_error,
            "last_error_at": r.last_error_at,
            "poll_interval_seconds": r.poll_interval_seconds,
            "min_request_interval_seconds": r.min_request_interval_seconds,
            "limiter": live.limiter.snapshot() if live else None,
        })
    return rows


@router.get("/api/retailers")
def api_list(session: Session = Depends(get_session), rt: Runtime = Depends(get_runtime)):
    rows = retailer_rows(session, rt)
    for row in rows:
        for k in ("paused_until", "last_success_at", "last_error_at"):
            row[k] = row[k].isoformat() if row[k] else None
    return rows


@router.get("/api/stores")
def api_stores(session: Session = Depends(get_session)):
    return [{"retailer": s.retailer.slug, "store_id": s.store_id, "name": s.name, "city": s.city,
             "state": s.state, "zip_code": s.zip_code, "source": s.source} for s in list_stores(session)]


@router.get("/retailers")
def page(request: Request, session: Session = Depends(get_session), rt: Runtime = Depends(get_runtime),
         msg: str | None = None):
    return templates.TemplateResponse(request, "retailers.html", {
        "retailers": retailer_rows(session, rt), "stores": list_stores(session), "msg": msg,
    })


@router.post("/retailers/{slug}/toggle")
def toggle(slug: str, session: Session = Depends(get_session)):
    r = session.scalar(select(Retailer).where(Retailer.slug == slug))
    if r is None:
        raise HTTPException(404)
    r.enabled = not r.enabled
    return RedirectResponse("/retailers", status_code=303)


@router.post("/retailers/{slug}/resume")
def resume(slug: str, session: Session = Depends(get_session), rt: Runtime = Depends(get_runtime)):
    """Manually clear a pause. Use only once you've fixed the cause (e.g. waited out a block)."""
    r = session.scalar(select(Retailer).where(Retailer.slug == slug))
    if r is None:
        raise HTTPException(404)
    if slug in rt.retailers.active():
        rt.retailers.get(slug).limiter.resume()
    r.paused_until = None
    r.pause_reason = None
    r.consecutive_failures = 0
    r.health = "IDLE"
    return RedirectResponse("/retailers?msg=Retailer+resumed.", status_code=303)


@router.post("/stores")
def form_add_store(session: Session = Depends(get_session), retailer: str = Form(...), store_id: str = Form(...),
                   name: str = Form(""), city: str = Form(""), state: str = Form(""), zip_code: str = Form("")):
    try:
        add_store(session, retailer, store_id.strip(), name.strip() or None, city.strip() or None,
                  state.strip() or None, zip_code.strip() or None)
    except KeyError as exc:
        raise HTTPException(400, str(exc)) from exc
    return RedirectResponse("/retailers?msg=Store+saved.", status_code=303)
