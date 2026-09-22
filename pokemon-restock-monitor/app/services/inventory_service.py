"""Inventory evaluation rules and persistence helpers.

``evaluate`` is a pure function that applies every safety rule to a raw
retailer observation:

* errors / unknown responses are never treated as OUT_OF_STOCK;
* store-level products only count STORE_* answers -- online availability is
  never reported as in-store availability;
* third-party marketplace listings don't alert unless ACCEPT_THIRD_PARTY=true;
* LIMITED / PREORDER alert only when enabled.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.database import utcnow
from app.models import (
    AvailabilityScope,
    Event,
    EventType,
    InventoryCheck,
    InventoryState,
    InventoryStatus,
    Product,
    SellerType,
)
from app.retailers.base import Observation, ProductRef
from app.utils.logging import log_event

logger = logging.getLogger("inventory")

STORE_SCOPES = {AvailabilityScope.STORE_AVAILABLE, AvailabilityScope.STORE_UNAVAILABLE}
ONLINE_SCOPES = {AvailabilityScope.ONLINE_AVAILABLE, AvailabilityScope.ONLINE_UNAVAILABLE}


@dataclass(frozen=True)
class Evaluation:
    status: InventoryStatus  # normalized status for this location
    scope: AvailabilityScope
    alertable: bool  # would justify a restock alert (before verification)
    closes_episode: bool  # definitively not purchasable -> ends an open restock episode
    ignored_reason: str | None = None  # THIRD_PARTY | UNKNOWN_SELLER | STORE_DATA_UNAVAILABLE
    note: str | None = None


def is_alert_status(status: InventoryStatus, settings: Settings) -> bool:
    if status == InventoryStatus.AVAILABLE:
        return True
    if status == InventoryStatus.LIMITED:
        return settings.alert_on_limited
    if status == InventoryStatus.PREORDER:
        return settings.alert_on_preorder
    return False


def evaluate(obs: Observation, product: ProductRef, settings: Settings) -> Evaluation:
    if not obs.request_success:
        return Evaluation(InventoryStatus.ERROR, obs.scope, alertable=False, closes_episode=False,
                          ignored_reason=None, note=obs.error)

    status, scope = obs.status, obs.scope

    # Store-level products only accept store-scoped answers.
    if product.is_store_level and scope not in STORE_SCOPES:
        note = obs.message or (
            "Retailer response did not contain store-level inventory for this store; "
            "online availability is not treated as in-store availability."
        )
        return Evaluation(InventoryStatus.UNKNOWN, AvailabilityScope.UNKNOWN, False, False,
                          "STORE_DATA_UNAVAILABLE", note)
    if not product.is_store_level and scope in STORE_SCOPES:
        return Evaluation(InventoryStatus.UNKNOWN, AvailabilityScope.UNKNOWN, False, False,
                          None, "Got a store-level answer for an online product; ignoring.")

    if status in (InventoryStatus.UNKNOWN, InventoryStatus.ERROR):
        return Evaluation(InventoryStatus.UNKNOWN, scope, False, False, None, obs.message)

    if is_alert_status(status, settings):
        accept_3p = product.accept_third_party if product.accept_third_party is not None else settings.accept_third_party
        if obs.seller_type == SellerType.THIRD_PARTY and not accept_3p:
            return Evaluation(status, scope, False, True, "THIRD_PARTY",
                              f"Listing sold by third-party seller {obs.seller_name or ''}".strip())
        if obs.seller_type == SellerType.UNKNOWN and not settings.accept_unknown_seller:
            return Evaluation(status, scope, False, True, "UNKNOWN_SELLER",
                              "Seller could not be determined and ACCEPT_UNKNOWN_SELLER=false")
        return Evaluation(status, scope, True, False, None, obs.message)

    # Determinate, not something we alert on (OUT_OF_STOCK, UNAVAILABLE, or
    # LIMITED/PREORDER with alerts disabled).
    return Evaluation(status, scope, False, True, None, obs.message)


def transition_kind(prev: str, new: str, prev_seller: str | None = None) -> str:
    if prev == new and prev_seller == SellerType.THIRD_PARTY.value:
        return "THIRD_PARTY_TO_FIRST_PARTY"
    return f"{prev}_TO_{new}"


TRANSITION_LABELS = {
    "OUT_OF_STOCK_TO_AVAILABLE": "Restock: was out of stock",
    "OUT_OF_STOCK_TO_LIMITED": "Restock (limited stock): was out of stock",
    "UNAVAILABLE_TO_AVAILABLE": "Listing became available",
    "LIMITED_TO_AVAILABLE": "Stock increased: was limited",
    "UNKNOWN_TO_AVAILABLE": "Available (previous status unknown -- first observation)",
    "UNKNOWN_TO_LIMITED": "Limited stock (previous status unknown -- first observation)",
    "THIRD_PARTY_TO_FIRST_PARTY": "Now sold by the retailer (previously third-party only)",
}


def transition_label(kind: str | None) -> str:
    if not kind:
        return ""
    return TRANSITION_LABELS.get(kind, kind.replace("_TO_", " → ").replace("_", " ").title())


def arrow(prev: str | None, new: str | None) -> str:
    return f"{prev or '?'} → {new or '?'}"


def ms_between(later: datetime | None, earlier: datetime | None) -> int | None:
    if later is None or earlier is None:
        return None
    return max(0, int((later - earlier).total_seconds() * 1000))


# -- persistence -------------------------------------------------------------------

def get_or_create_state(session: Session, product: Product) -> InventoryState:
    state = session.scalar(select(InventoryState).where(InventoryState.product_id == product.id))
    if state is None:
        state = InventoryState(product_id=product.id, next_check_at=utcnow())
        session.add(state)
        session.flush()
    return state


def record_check(session: Session, product: ProductRef, obs: Observation, previous_status: str | None,
                 purpose: str, evaluation: Evaluation | None = None) -> InventoryCheck:
    row = InventoryCheck(
        product_id=product.id,
        checked_at=obs.checked_at,
        purpose=purpose,
        retailer=product.retailer,
        sku=product.sku,
        store_id=product.store_id,
        previous_status=previous_status,
        status=(evaluation.status if evaluation else obs.status).value,
        scope=(evaluation.scope if evaluation else obs.scope).value,
        seller_type=obs.seller_type.value,
        seller_name=obs.seller_name,
        quantity=obs.quantity,
        price=obs.price,
        request_success=obs.request_success,
        http_status=obs.http_status,
        response_time_ms=obs.response_time_ms,
        source=obs.source,
        error=obs.error,
        error_kind=obs.error_kind,
        message=(evaluation.note if evaluation and evaluation.note else obs.message),
    )
    session.add(row)
    return row


def add_event(session: Session, event_type: EventType, product: ProductRef | None = None, **fields) -> Event:
    ev = Event(event_type=event_type.value, **fields)
    if product is not None:
        ev.product_id = product.id
        ev.retailer = product.retailer
        ev.sku = product.sku
        ev.store_id = product.store_id
    session.add(ev)
    session.flush()
    return ev


def log_check(product: ProductRef, obs: Observation, previous_status: str | None, new_status: str,
              purpose: str, verified: str | None = None) -> None:
    log_event(
        logger,
        "inventory_check",
        logging.INFO if obs.request_success else logging.WARNING,
        timestamp=obs.checked_at.isoformat(timespec="seconds"),
        retailer=product.retailer.upper(),
        sku=product.sku,
        store=product.location_label,
        previous_status=previous_status,
        new_status=new_status,
        transition=(arrow(previous_status, new_status)
                    if obs.request_success and previous_status != new_status else None),
        purpose=purpose,
        verification=verified,
        request_success=obs.request_success,
        response_time_ms=obs.response_time_ms,
        error=obs.error,
    )
