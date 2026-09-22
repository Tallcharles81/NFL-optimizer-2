from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base, UTCDateTime, utcnow


class Product(Base):
    """One monitored (retailer, SKU, location) combination.

    ``store_id`` NULL means the online listing. The same SKU can be monitored
    at many stores by adding one row per store.
    """

    __tablename__ = "products"
    __table_args__ = (
        UniqueConstraint("retailer_id", "sku", "store_id", name="uq_product_retailer_sku_store"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    retailer_id: Mapped[int] = mapped_column(ForeignKey("retailers.id"), index=True)
    sku: Mapped[str] = mapped_column(String(64), index=True)
    product_name: Mapped[str] = mapped_column(String(300))
    product_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    image_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    category: Mapped[str | None] = mapped_column(String(100), nullable=True, default="pokemon-tcg")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    max_quantity: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Extra identifiers, shown in alerts (DPCI/UPC help when buying in store)
    upc: Mapped[str | None] = mapped_column(String(14), nullable=True)
    dpci: Mapped[str | None] = mapped_column(String(12), nullable=True)

    store_id: Mapped[str | None] = mapped_column(String(50), nullable=True)
    store_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    city: Mapped[str | None] = mapped_column(String(100), nullable=True)
    state: Mapped[str | None] = mapped_column(String(50), nullable=True)
    zip_code: Mapped[str | None] = mapped_column(String(20), nullable=True)

    # Optional per-product overrides (NULL = retailer / global default)
    poll_interval_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    accept_third_party: Mapped[bool | None] = mapped_column(Boolean, nullable=True)

    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)

    retailer: Mapped["Retailer"] = relationship(back_populates="products")  # noqa: F821
    state_row: Mapped["InventoryState | None"] = relationship(  # noqa: F821
        back_populates="product", uselist=False, cascade="all, delete-orphan"
    )

    @property
    def is_store_level(self) -> bool:
        return bool(self.store_id)

    @property
    def location_label(self) -> str:
        if not self.store_id:
            return "Online"
        loc = ", ".join(p for p in (self.city, self.state) if p)
        name = self.store_name or f"Store {self.store_id}"
        return f"{name} ({loc})" if loc else name
