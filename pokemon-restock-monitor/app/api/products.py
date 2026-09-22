"""Product management: JSON API under /api/products and the /products page."""

from __future__ import annotations

import asyncio
from datetime import timedelta

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.api.deps import get_runtime, get_session, require_auth, templates
from app.database import utcnow
from app.models import Product
from app.retailers.registry import RETAILER_CLASSES
from app.runtime import Runtime
from app.services.product_service import (
    ProductCreate,
    ProductUpdate,
    create_product,
    delete_product,
    list_products,
    list_stores,
    update_product,
    validation_warnings,
)

router = APIRouter(dependencies=[Depends(require_auth)])

MANUAL_CHECK_COOLDOWN_SECONDS = 15


def product_dict(p: Product) -> dict:
    st = p.state_row
    return {
        "id": p.id,
        "retailer": p.retailer.slug,
        "sku": p.sku,
        "product_name": p.product_name,
        "product_url": p.product_url,
        "image_url": p.image_url,
        "category": p.category,
        "enabled": p.enabled,
        "max_quantity": p.max_quantity,
        "upc": p.upc,
        "dpci": p.dpci,
        "store_id": p.store_id,
        "store_name": p.store_name,
        "city": p.city,
        "state": p.state,
        "zip_code": p.zip_code,
        "poll_interval_seconds": p.poll_interval_seconds,
        "accept_third_party": p.accept_third_party,
        "created_at": p.created_at.isoformat() if p.created_at else None,
        "updated_at": p.updated_at.isoformat() if p.updated_at else None,
        "inventory": None if st is None else {
            "status": st.status,
            "scope": st.scope,
            "seller_type": st.seller_type,
            "alert_state": st.alert_state,
            "episode_open": st.episode_open,
            "restock_episode": st.restock_episode,
            "last_checked_at": st.last_checked_at.isoformat() if st.last_checked_at else None,
            "last_success_at": st.last_success_at.isoformat() if st.last_success_at else None,
            "next_check_at": st.next_check_at.isoformat() if st.next_check_at else None,
            "poll_mode": st.poll_mode,
            "consecutive_errors": st.consecutive_errors,
            "last_error": st.last_error,
            "message": st.message,
        },
    }


# -- JSON API -----------------------------------------------------------------------
@router.get("/api/products")
def api_list(session: Session = Depends(get_session)):
    return [product_dict(p) for p in list_products(session)]


@router.post("/api/products", status_code=201)
def api_create(data: ProductCreate, session: Session = Depends(get_session)):
    try:
        product = create_product(session, data)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    session.refresh(product)
    return {"product": product_dict(product), "warnings": validation_warnings(data)}


@router.patch("/api/products/{product_id}")
def api_update(product_id: int, data: ProductUpdate, session: Session = Depends(get_session)):
    try:
        product = update_product(session, product_id, data)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    session.flush()
    return product_dict(product)


@router.delete("/api/products/{product_id}", status_code=204)
def api_delete(product_id: int, session: Session = Depends(get_session)):
    try:
        delete_product(session, product_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc


def _schedule_check(rt: Runtime, product_id: int) -> str:
    with rt.db.session() as s:
        p = s.get(Product, product_id)
        if p is None:
            raise HTTPException(404, "Product not found")
        st = p.state_row
        if st and st.last_checked_at and utcnow() - st.last_checked_at < timedelta(
                seconds=MANUAL_CHECK_COOLDOWN_SECONDS):
            return f"Checked less than {MANUAL_CHECK_COOLDOWN_SECONDS}s ago; please wait."
        if st:
            st.next_check_at = utcnow()
    if not rt.settings.monitor_enabled:
        asyncio.get_running_loop().create_task(rt.monitor.check_product(product_id))
    return "Check scheduled (subject to the retailer rate limit)."


@router.post("/api/products/{product_id}/check")
async def api_check(product_id: int, rt: Runtime = Depends(get_runtime)):
    return {"message": _schedule_check(rt, product_id)}


# -- HTML ------------------------------------------------------------------------------
@router.get("/products")
def page(request: Request, session: Session = Depends(get_session), msg: str | None = None,
         error: str | None = None):
    return templates.TemplateResponse(request, "products.html", {
        "products": list_products(session),
        "retailers": [(slug, cls.display_name) for slug, cls in RETAILER_CLASSES.items() if slug != "simulated"],
        "stores": list_stores(session),
        "msg": msg,
        "error": error,
    })


@router.post("/products")
def form_create(
    session: Session = Depends(get_session),
    retailer: str = Form(...), sku: str = Form(...), product_name: str = Form(...),
    product_url: str = Form(""), image_url: str = Form(""), store_id: str = Form(""),
    store_name: str = Form(""), city: str = Form(""), state: str = Form(""), zip_code: str = Form(""),
    max_quantity: str = Form(""), enabled: str = Form("on"),
):
    try:
        data = ProductCreate(
            retailer=retailer, sku=sku, product_name=product_name, product_url=product_url,
            image_url=image_url, store_id=store_id, store_name=store_name, city=city, state=state,
            zip_code=zip_code, max_quantity=int(max_quantity) if max_quantity.strip() else None,
            enabled=enabled == "on",
        )
        create_product(session, data)
        warnings = validation_warnings(data)
    except (ValidationError, ValueError, KeyError) as exc:
        session.rollback()
        return RedirectResponse(f"/products?error={_q(str(exc))}", status_code=303)
    note = "Product added." + (" Warning: " + " ".join(warnings) if warnings else "")
    return RedirectResponse(f"/products?msg={_q(note)}", status_code=303)


@router.post("/products/{product_id}/toggle")
def form_toggle(product_id: int, session: Session = Depends(get_session)):
    p = session.get(Product, product_id)
    if p is None:
        raise HTTPException(404)
    update_product(session, product_id, ProductUpdate(enabled=not p.enabled))
    return RedirectResponse("/products", status_code=303)


@router.post("/products/{product_id}/delete")
def form_delete(product_id: int, session: Session = Depends(get_session)):
    delete_product(session, product_id)
    return RedirectResponse("/products?msg=Product+deleted.", status_code=303)


@router.post("/products/{product_id}/check")
async def form_check(product_id: int, rt: Runtime = Depends(get_runtime)):
    return RedirectResponse(f"/products?msg={_q(_schedule_check(rt, product_id))}", status_code=303)


def _q(text: str) -> str:
    from urllib.parse import quote_plus

    return quote_plus(text[:500])
