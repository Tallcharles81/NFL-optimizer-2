"""A fake retailer for simulation mode and tests. Makes no network requests.

Drive it with ``set_status`` (persistent state, records when inventory
"changed" so detection latency can be measured) and ``queue`` (one-shot
responses consumed before the persistent state, e.g. a flicker for a false
positive or an injected HTTP error).
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone

from app.models.enums import AvailabilityScope, InventoryStatus, SellerType
from app.retailers.base import Observation, ProductRef, RetailerMonitor
from app.utils.rate_limit import BotChallengeError, MonitorRequestError, RateLimitedError

PURCHASABLE = {InventoryStatus.AVAILABLE, InventoryStatus.LIMITED, InventoryStatus.PREORDER}


@dataclass
class SimState:
    status: InventoryStatus
    seller_type: SellerType = SellerType.FIRST_PARTY_RETAILER
    quantity: int | None = None
    price: float | None = 49.99
    changed_at: datetime | None = None


class SimulatedRetailer(RetailerMonitor):
    slug = "simulated"
    display_name = "Simulated Retailer"
    implementation_status = "simulation only (no network)"
    supports_store_inventory = True
    exempt_from_poll_floor = True
    first_party_seller_names = ("simulated retailer",)

    def __init__(self, settings, limiter, http=None):
        super().__init__(settings, limiter, http)
        self._state: dict[tuple[str, str | None], SimState] = {}
        self._queue: dict[tuple[str, str | None], deque] = {}
        self.request_count = 0

    # -- control ---------------------------------------------------------------
    def set_status(self, sku: str, status: InventoryStatus, store_id: str | None = None,
                   seller_type: SellerType = SellerType.FIRST_PARTY_RETAILER,
                   quantity: int | None = None, price: float | None = 49.99) -> None:
        key = (sku, store_id)
        prev = self._state.get(key)
        changed = datetime.now(timezone.utc)
        if prev and prev.status == status and prev.seller_type == seller_type:
            changed = prev.changed_at
        self._state[key] = SimState(status, seller_type, quantity, price, changed)

    def queue(self, sku: str, responses: list, store_id: str | None = None) -> None:
        self._queue.setdefault((sku, store_id), deque()).extend(responses)

    # -- interface ---------------------------------------------------------------
    async def _respond(self, product: ProductRef) -> Observation:
        await self.limiter.acquire()
        self.request_count += 1
        key = (product.sku, product.store_id)
        item = None
        q = self._queue.get(key)
        if q:
            item = q.popleft()
        if isinstance(item, BaseException):
            if isinstance(item, RateLimitedError):
                self.limiter.record_rate_limited(item.retry_after)
            elif isinstance(item, BotChallengeError):
                self.limiter.record_blocked(str(item))
            elif isinstance(item, MonitorRequestError):
                self.limiter.record_failure(str(item))
            raise item
        if isinstance(item, Observation):
            self.limiter.record_success()
            return item
        if isinstance(item, InventoryStatus):
            state = SimState(item, changed_at=datetime.now(timezone.utc))
        else:
            state = self._state.get(key) or SimState(InventoryStatus.UNKNOWN)
        self.limiter.record_success()

        store = product.is_store_level
        if state.status in PURCHASABLE:
            scope = AvailabilityScope.STORE_AVAILABLE if store else AvailabilityScope.ONLINE_AVAILABLE
        elif state.status == InventoryStatus.UNKNOWN:
            scope = AvailabilityScope.UNKNOWN
        else:
            scope = AvailabilityScope.STORE_UNAVAILABLE if store else AvailabilityScope.ONLINE_UNAVAILABLE
        return Observation(
            status=state.status,
            scope=scope,
            seller_type=state.seller_type,
            seller_name="Simulated Retailer" if state.seller_type == SellerType.FIRST_PARTY_RETAILER
            else ("Marketplace Seller" if state.seller_type == SellerType.THIRD_PARTY else None),
            quantity=state.quantity,
            price=state.price,
            source="simulated",
            http_status=200,
            response_time_ms=1,
            inventory_changed_at=state.changed_at,
        )

    async def get_product_status(self, product: ProductRef) -> Observation:
        return await self._respond(product)

    async def get_store_inventory(self, product: ProductRef) -> Observation:
        return await self._respond(product)

    def get_product_url(self, product: ProductRef) -> str | None:
        return product.product_url or f"https://example.com/simulated/{product.sku}"
