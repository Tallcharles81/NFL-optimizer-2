from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Boolean, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base, UTCDateTime, utcnow


class Event(Base):
    """Something noteworthy happened (restock, sell-out, error, ...)."""

    __tablename__ = "events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_type: Mapped[str] = mapped_column(String(40), index=True)
    product_id: Mapped[int | None] = mapped_column(
        ForeignKey("products.id", ondelete="SET NULL"), nullable=True, index=True
    )
    retailer: Mapped[str | None] = mapped_column(String(50), nullable=True)
    sku: Mapped[str | None] = mapped_column(String(64), nullable=True)
    store_id: Mapped[str | None] = mapped_column(String(50), nullable=True)
    previous_status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    new_status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    transition: Mapped[str | None] = mapped_column(String(60), nullable=True)
    restock_episode: Mapped[int | None] = mapped_column(Integer, nullable=True)
    dedupe_key: Mapped[str | None] = mapped_column(String(200), nullable=True, index=True)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    details: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    is_simulation: Mapped[bool] = mapped_column(Boolean, default=False)

    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)
    # Latency tracking (restock events)
    inventory_changed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    detected_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    verified_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    notified_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    detection_latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    detection_window_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    verification_latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    notification_latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    notification_sent: Mapped[bool] = mapped_column(Boolean, default=False)

    product: Mapped["Product | None"] = relationship()  # noqa: F821
