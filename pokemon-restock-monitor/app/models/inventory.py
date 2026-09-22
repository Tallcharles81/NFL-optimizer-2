from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base, UTCDateTime, utcnow
from app.models.enums import AlertState, AvailabilityScope, InventoryStatus, PollMode, SellerType


class InventoryState(Base):
    """Current (persisted) state for a product. Survives restarts."""

    __tablename__ = "inventory_states"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    product_id: Mapped[int] = mapped_column(
        ForeignKey("products.id", ondelete="CASCADE"), unique=True, index=True
    )
    # Last *determinate-or-unknown* status. ERROR responses never overwrite it.
    status: Mapped[str] = mapped_column(String(20), default=InventoryStatus.UNKNOWN.value)
    scope: Mapped[str] = mapped_column(String(30), default=AvailabilityScope.UNKNOWN.value)
    seller_type: Mapped[str] = mapped_column(String(30), default=SellerType.UNKNOWN.value)
    seller_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    quantity: Mapped[int | None] = mapped_column(Integer, nullable=True)
    price: Mapped[float | None] = mapped_column(Float, nullable=True)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)

    last_checked_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    last_success_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    last_changed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    consecutive_errors: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_error_kind: Mapped[str | None] = mapped_column(String(40), nullable=True)

    poll_mode: Mapped[str] = mapped_column(String(20), default=PollMode.NORMAL.value)
    next_check_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True, index=True)
    recently_active_until: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)

    # Restock episode tracking (drives alert de-duplication)
    restock_episode: Mapped[int] = mapped_column(Integer, default=0)
    episode_open: Mapped[bool] = mapped_column(Boolean, default=False)
    alert_state: Mapped[str] = mapped_column(String(30), default=AlertState.NONE.value)
    episode_started_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    last_ignored_reason: Mapped[str | None] = mapped_column(String(60), nullable=True)

    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)

    product: Mapped["Product"] = relationship(back_populates="state_row")  # noqa: F821


class InventoryCheck(Base):
    """Append-only log of every request made to a retailer (poll or verify)."""

    __tablename__ = "inventory_checks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"), index=True)
    checked_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)
    purpose: Mapped[str] = mapped_column(String(10), default="POLL")  # POLL | VERIFY
    retailer: Mapped[str] = mapped_column(String(50))
    sku: Mapped[str] = mapped_column(String(64))
    store_id: Mapped[str | None] = mapped_column(String(50), nullable=True)
    previous_status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    status: Mapped[str] = mapped_column(String(20))
    scope: Mapped[str] = mapped_column(String(30))
    seller_type: Mapped[str] = mapped_column(String(30))
    seller_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    quantity: Mapped[int | None] = mapped_column(Integer, nullable=True)
    price: Mapped[float | None] = mapped_column(Float, nullable=True)
    request_success: Mapped[bool] = mapped_column(Boolean)
    http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    response_time_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source: Mapped[str | None] = mapped_column(String(100), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_kind: Mapped[str | None] = mapped_column(String(40), nullable=True)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
