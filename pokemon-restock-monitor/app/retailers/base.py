"""Retailer abstraction.

Every retailer implements ``RetailerMonitor``. The rest of the application
only talks to this interface -- no retailer-specific logic lives outside
``app/retailers/<retailer>.py``.

Retailer implementations receive a ``ProductRef`` (a plain, immutable
snapshot) rather than an ORM object so no database session is held open
while network I/O is in flight.
"""

from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import ClassVar

from app.config import Settings
from app.models.enums import AvailabilityScope, InventoryStatus, SellerType
from app.utils.rate_limit import MonitorRequestError, RateLimiter

logger = logging.getLogger("retailers")


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class ProductRef:
    id: int
    retailer: str
    sku: str
    product_name: str
    product_url: str | None = None
    image_url: str | None = None
    store_id: str | None = None
    store_name: str | None = None
    city: str | None = None
    state: str | None = None
    zip_code: str | None = None
    max_quantity: int | None = None
    accept_third_party: bool | None = None
    poll_interval_seconds: float | None = None

    @property
    def is_store_level(self) -> bool:
        return bool(self.store_id)

    @property
    def location_label(self) -> str:
        if not self.store_id:
            return "Online"
        loc = ", ".join(p for p in (self.city, self.state) if p)
        return loc or self.store_name or f"Store {self.store_id}"

    @classmethod
    def from_model(cls, product, retailer_slug: str) -> "ProductRef":
        return cls(
            id=product.id,
            retailer=retailer_slug,
            sku=product.sku,
            product_name=product.product_name,
            product_url=product.product_url,
            image_url=product.image_url,
            store_id=product.store_id,
            store_name=product.store_name,
            city=product.city,
            state=product.state,
            zip_code=product.zip_code,
            max_quantity=product.max_quantity,
            accept_third_party=product.accept_third_party,
            poll_interval_seconds=product.poll_interval_seconds,
        )


@dataclass
class Observation:
    """A single normalized answer from a retailer."""

    status: InventoryStatus
    scope: AvailabilityScope = AvailabilityScope.UNKNOWN
    seller_type: SellerType = SellerType.UNKNOWN
    seller_name: str | None = None
    quantity: int | None = None  # only when the retailer legitimately exposes it
    price: float | None = None
    source: str | None = None
    http_status: int | None = None
    response_time_ms: int | None = None
    error: str | None = None
    error_kind: str | None = None
    retry_after: float | None = None
    message: str | None = None
    # When the source itself says when inventory changed (e.g. simulation).
    inventory_changed_at: datetime | None = None
    checked_at: datetime = field(default_factory=_now)

    @property
    def request_success(self) -> bool:
        return self.error is None and self.status != InventoryStatus.ERROR

    def with_(self, **changes) -> "Observation":
        return replace(self, **changes)

    @classmethod
    def from_error(cls, exc: BaseException, source: str, elapsed_ms: int | None = None) -> "Observation":
        kind = getattr(exc, "kind", type(exc).__name__)
        return cls(
            status=InventoryStatus.ERROR,
            source=source,
            error=str(exc) or type(exc).__name__,
            error_kind=kind,
            http_status=getattr(exc, "status_code", None),
            retry_after=getattr(exc, "retry_after", None),
            response_time_ms=elapsed_ms,
        )


class RetailerMonitor(ABC):
    """Common interface for all retailers."""

    slug: ClassVar[str]
    display_name: ClassVar[str]
    # Human readable state of the integration, shown on the dashboard.
    implementation_status: ClassVar[str] = "stub"
    # Only set True when the retailer offers a legitimate store-level data source.
    supports_store_inventory: ClassVar[bool] = False
    # Seller names (lower-case) that mean "sold by the retailer itself".
    first_party_seller_names: ClassVar[tuple[str, ...]] = ()
    # Retailers used only for simulation/tests may poll faster than the floor.
    exempt_from_poll_floor: ClassVar[bool] = False

    def __init__(self, settings: Settings, limiter: RateLimiter, http=None):
        self.settings = settings
        self.limiter = limiter
        self.http = http

    # -- the required interface ------------------------------------------------
    @abstractmethod
    async def get_product_status(self, product: ProductRef) -> Observation:
        """Online availability for the product."""

    async def get_store_inventory(self, product: ProductRef) -> Observation:
        """Store-level availability. Default: not supported -> UNKNOWN.

        A product being available online must never be reported as available
        at a store, so the default is an honest UNKNOWN rather than a guess.
        """
        return Observation(
            status=InventoryStatus.UNKNOWN,
            scope=AvailabilityScope.UNKNOWN,
            source=f"{self.slug}:store",
            message=(
                f"{self.display_name} store-level inventory is not implemented: no permitted "
                "store inventory data source is configured for this retailer."
            ),
        )

    async def verify_availability(self, product: ProductRef) -> Observation:
        """Independent re-check used for false-positive protection."""
        return await self.check(product)

    def get_product_url(self, product: ProductRef) -> str | None:
        return product.product_url

    # -- helpers -----------------------------------------------------------------
    async def check(self, product: ProductRef) -> Observation:
        if product.is_store_level:
            return await self.get_store_inventory(product)
        return await self.get_product_status(product)

    async def safe_check(self, product: ProductRef, verify: bool = False) -> Observation:
        """Run a check and convert any exception into an ERROR observation."""
        start = time.perf_counter()
        try:
            if verify:
                obs = await self.verify_availability(product)
            else:
                obs = await self.check(product)
        except MonitorRequestError as exc:
            return Observation.from_error(exc, self.slug, int((time.perf_counter() - start) * 1000))
        except Exception as exc:  # noqa: BLE001 - never let one retailer crash the loop
            logger.exception("Unexpected error checking %s %s", self.slug, product.sku)
            obs = Observation.from_error(exc, self.slug, int((time.perf_counter() - start) * 1000))
            obs.error_kind = "UNEXPECTED"
            return obs
        if obs.response_time_ms is None:
            obs.response_time_ms = int((time.perf_counter() - start) * 1000)
        return obs

    def classify_seller(self, seller_name: str | None) -> SellerType:
        if not seller_name:
            return SellerType.UNKNOWN
        if seller_name.strip().lower() in self.first_party_seller_names:
            return SellerType.FIRST_PARTY_RETAILER
        return SellerType.THIRD_PARTY

    async def aclose(self) -> None:
        if self.http is not None:
            await self.http.aclose()
