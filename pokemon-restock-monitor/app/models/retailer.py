from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base, UTCDateTime, utcnow
from app.models.enums import RetailerHealth


class Retailer(Base):
    __tablename__ = "retailers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    slug: Mapped[str] = mapped_column(String(50), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(100))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # Optional per-retailer overrides (NULL = use global settings)
    poll_interval_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    min_request_interval_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)

    health: Mapped[str] = mapped_column(String(30), default=RetailerHealth.IDLE.value)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    paused_until: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    pause_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_success_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_error_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)

    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)

    stores: Mapped[list["Store"]] = relationship(back_populates="retailer", cascade="all, delete-orphan")
    products: Mapped[list["Product"]] = relationship(back_populates="retailer")  # noqa: F821


class Store(Base):
    """A physical store the user wants monitored for a given retailer."""

    __tablename__ = "stores"
    __table_args__ = (UniqueConstraint("retailer_id", "store_id", name="uq_store_retailer_store"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    retailer_id: Mapped[int] = mapped_column(ForeignKey("retailers.id", ondelete="CASCADE"), index=True)
    store_id: Mapped[str] = mapped_column(String(50))
    name: Mapped[str] = mapped_column(String(200))
    city: Mapped[str | None] = mapped_column(String(100), nullable=True)
    state: Mapped[str | None] = mapped_column(String(50), nullable=True)
    zip_code: Mapped[str | None] = mapped_column(String(20), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    source: Mapped[str] = mapped_column(String(20), default="manual")  # manual | config
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    retailer: Mapped[Retailer] = relationship(back_populates="stores")

    @property
    def label(self) -> str:
        loc = ", ".join(p for p in (self.city, self.state) if p)
        return f"{self.name} ({loc})" if loc else self.name
