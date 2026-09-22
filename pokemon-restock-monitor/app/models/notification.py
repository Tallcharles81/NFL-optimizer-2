from __future__ import annotations

from datetime import datetime

from sqlalchemy import ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base, UTCDateTime, utcnow
from app.models.enums import NotificationStatus


class Notification(Base):
    """One delivery attempt record per (dedupe_key, channel).

    The unique constraint is what makes alerts idempotent -- including across
    crashes and restarts.
    """

    __tablename__ = "notifications"
    __table_args__ = (UniqueConstraint("dedupe_key", "channel", name="uq_notification_dedupe_channel"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_id: Mapped[int | None] = mapped_column(ForeignKey("events.id", ondelete="SET NULL"), nullable=True)
    product_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    store_id: Mapped[str | None] = mapped_column(String(50), nullable=True)
    event_type: Mapped[str] = mapped_column(String(40))
    channel: Mapped[str] = mapped_column(String(20))
    dedupe_key: Mapped[str] = mapped_column(String(200), index=True)
    status: Mapped[str] = mapped_column(String(20), default=NotificationStatus.PENDING.value)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    timestamp: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)
    sent_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)

    @property
    def notification_sent(self) -> bool:
        return self.status == NotificationStatus.SENT.value
