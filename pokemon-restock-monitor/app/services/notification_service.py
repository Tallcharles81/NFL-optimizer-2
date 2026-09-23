"""Notification delivery with persistent de-duplication.

Each alert has a ``dedupe_key`` (for restocks: product + store + restock
episode). A row in ``notifications`` with UNIQUE(dedupe_key, channel) is
claimed *before* sending, so the same alert can never be delivered twice to
a channel -- not by concurrent tasks, repeated polls, or a restart.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.config import Settings
from app.database import Database, utcnow
from app.models import Event, EventType, Notification, NotificationStatus, Product
from app.notifications import build_channels
from app.notifications.base import AlertMessage, NotificationError, Notifier
from app.retailers.registry import get_retailer_class
from app.services.inventory_service import ms_between, transition_label
from app.utils.logging import log_event
from app.utils.retry import retry_async

logger = logging.getLogger("notifications")

def google_buy_link(product_name: str, tcin: str) -> str:
    """Google AI Mode, pre-asked to buy this exact Target item.

    Target's authorized agent purchases run through Google (AI Mode / Gemini);
    Google asks you to confirm before it buys anything.
    """
    query = f"Buy {product_name} (Target TCIN {tcin}) from Target.com for me"
    return "https://www.google.com/search?" + urlencode({"q": query, "udm": "50"})


# A PENDING claim older than this is assumed to belong to a crashed process.
STALE_CLAIM_SECONDS = 60


@dataclass
class DeliveryReport:
    dedupe_key: str
    sent: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)

    @property
    def any_sent(self) -> bool:
        return bool(self.sent)


class NotificationService:
    def __init__(self, settings: Settings, db: Database, channels: list[Notifier] | None = None,
                 clock=utcnow, retry_sleep=asyncio.sleep):
        self.settings = settings
        self.db = db
        self.channels = channels if channels is not None else build_channels(settings)
        self.clock = clock
        self._retry_sleep = retry_sleep
        self._locks: dict[str, asyncio.Lock] = {}
        self.channel_status: dict[str, dict] = {c.name: {"last_sent_at": None, "last_error": None}
                                                for c in self.channels}

    def _tz(self) -> ZoneInfo:
        try:
            return ZoneInfo(self.settings.timezone)
        except Exception:  # noqa: BLE001
            return ZoneInfo("UTC")

    def format_time(self, dt: datetime | None) -> str | None:
        if dt is None:
            return None
        local = dt.astimezone(self._tz())
        return local.strftime("%-I:%M:%S %p %Z")

    # -- message building ----------------------------------------------------------
    def build_restock_message(self, event: Event, product: Product) -> AlertMessage:
        try:
            display = get_retailer_class(product.retailer.slug).display_name
        except KeyError:
            display = product.retailer.name
        details = event.details or {}
        # The monitor stores the retailer's canonical product URL on the event.
        url = details.get("product_url") or product.product_url
        extra: dict[str, str] = {}
        label = transition_label(event.transition)
        if label:
            extra["Change"] = label
        if details.get("seller_name"):
            extra["Seller"] = details["seller_name"]
        if details.get("price") is not None:
            extra["Price"] = f"${details['price']:.2f}"
        if details.get("quantity") is not None:
            extra["Quantity"] = str(details["quantity"])
        if product.max_quantity:
            extra["Max qty to buy"] = str(product.max_quantity)
        if product.dpci:
            extra["DPCI (in store)"] = product.dpci
        if product.upc:
            extra["UPC"] = product.upc
        extra["Verified"] = "Yes - confirmed by independent re-check"
        title = "🚨 POKÉMON RESTOCK DETECTED"
        if event.event_type == EventType.RESTOCK_REMINDER.value:
            title = f"🔁 STILL IN STOCK (reminder {details.get('reminder')} of {details.get('reminder_max')})"
            extra["Verified"] = "Still in stock on the latest check"
            extra.pop("Change", None)
            if details.get("since"):
                extra["In stock since"] = self.format_time(datetime.fromisoformat(details["since"]))
        store = "Online" if not product.store_id else ", ".join(
            p for p in (product.city, product.state) if p) or product.store_name or product.store_id
        if product.store_id and product.store_name:
            store = f"{store} ({product.store_name})"
        links = []
        if self.settings.buy_with_google_button and product.retailer.slug == "target":
            links.append(("BUY WITH GOOGLE", google_buy_link(product.product_name, product.sku)))
        return AlertMessage(
            kind="restock",
            links=links,
            title=title,
            product_name=product.product_name,
            retailer=display,
            store=store,
            status=event.new_status,
            sku=product.sku,
            detected=self.format_time(event.detected_at),
            url=url,
            image_url=product.image_url,
            extra=extra,
            is_simulation=event.is_simulation,
        )

    # -- claiming (dedupe) ---------------------------------------------------------------
    def _claim(self, dedupe_key: str, channel: str, event: Event | None) -> bool:
        now = self.clock()
        with self.db.session() as s:
            existing = s.scalar(select(Notification).where(
                Notification.dedupe_key == dedupe_key, Notification.channel == channel))
            if existing is not None:
                if existing.status == NotificationStatus.SENT.value:
                    return False
                if existing.status == NotificationStatus.PENDING.value and \
                        (now - existing.timestamp).total_seconds() < STALE_CLAIM_SECONDS:
                    return False
                existing.status = NotificationStatus.PENDING.value
                existing.timestamp = now
                return True
            s.add(Notification(
                event_id=event.id if event else None,
                product_id=event.product_id if event else None,
                store_id=event.store_id if event else None,
                event_type=event.event_type if event else "SYSTEM",
                channel=channel,
                dedupe_key=dedupe_key,
                status=NotificationStatus.PENDING.value,
                timestamp=now,
            ))
            try:
                s.flush()
            except IntegrityError:
                s.rollback()
                return False
        return True

    def _finish(self, dedupe_key: str, channel: str, ok: bool, error: str | None, attempts: int) -> None:
        with self.db.session() as s:
            row = s.scalar(select(Notification).where(
                Notification.dedupe_key == dedupe_key, Notification.channel == channel))
            if row is None:
                return
            row.attempts = (row.attempts or 0) + attempts
            row.status = (NotificationStatus.SENT if ok else NotificationStatus.FAILED).value
            row.error = error
            if ok:
                row.sent_at = self.clock()

    async def _deliver(self, dedupe_key: str, message: AlertMessage, event: Event | None) -> DeliveryReport:
        report = DeliveryReport(dedupe_key)
        lock = self._locks.setdefault(dedupe_key, asyncio.Lock())
        async with lock:
            for channel in self.channels:
                if not self._claim(dedupe_key, channel.name, event):
                    report.skipped.append(channel.name)
                    log_event(logger, "notification_duplicate_suppressed", channel=channel.name,
                              dedupe_key=dedupe_key)
                    continue
                attempts = 0

                async def send_once(ch=channel):
                    nonlocal attempts
                    attempts += 1
                    await ch.send(message)

                try:
                    await retry_async(
                        send_once, max_retries=2, base_delay=1, max_delay=5,
                        retry_on=lambda e: isinstance(e, NotificationError) and e.retryable,
                        sleep=self._retry_sleep,
                    )
                except Exception as exc:  # noqa: BLE001
                    err = str(exc) or type(exc).__name__
                    self._finish(dedupe_key, channel.name, False, err, attempts)
                    report.failed[channel.name] = err
                    self.channel_status[channel.name]["last_error"] = err
                    log_event(logger, "notification_failed", logging.ERROR, channel=channel.name,
                              dedupe_key=dedupe_key, error=err)
                    continue
                self._finish(dedupe_key, channel.name, True, None, attempts)
                report.sent.append(channel.name)
                self.channel_status[channel.name]["last_sent_at"] = self.clock()
                self.channel_status[channel.name]["last_error"] = None
                log_event(logger, "notification_sent", channel=channel.name, dedupe_key=dedupe_key)
        return report

    # -- public API ---------------------------------------------------------------------
    async def send_restock_alert(self, event_id: int) -> DeliveryReport:
        with self.db.session() as s:
            event = s.get(Event, event_id)
            if event is None or event.event_type not in (EventType.RESTOCK_CONFIRMED.value,
                                                         EventType.RESTOCK_REMINDER.value):
                raise ValueError(f"Event {event_id} is not a RESTOCK_CONFIRMED or RESTOCK_REMINDER event")
            product = s.get(Product, event.product_id)
            _ = product.retailer  # load relationship before session closes
            message = self.build_restock_message(event, product)
            dedupe_key = event.dedupe_key
        report = await self._deliver(dedupe_key, message, event)
        if report.any_sent:
            with self.db.session() as s:
                event = s.get(Event, event_id)
                if not event.notification_sent:
                    now = self.clock()
                    event.notification_sent = True
                    event.notified_at = now
                    event.notification_latency_ms = ms_between(now, event.verified_at)
                    event.total_latency_ms = ms_between(now, event.inventory_changed_at or event.detected_at)
                    log_event(
                        logger, "alert_latency",
                        product_id=event.product_id, sku=event.sku,
                        detection_latency_ms=event.detection_latency_ms,
                        detection_window_ms=event.detection_window_ms,
                        verification_latency_ms=event.verification_latency_ms,
                        notification_latency_ms=event.notification_latency_ms,
                        total_latency_ms=event.total_latency_ms,
                    )
        return report

    async def send_system_alert(self, dedupe_key: str, title: str, text: str) -> DeliveryReport:
        msg = AlertMessage(kind="system", title=f"⚠️ {title}", text=text)
        return await self._deliver(f"system:{dedupe_key}", msg, None)

    async def send_test_message(self, retailer: str = "target") -> DeliveryReport:
        now = self.clock()
        if retailer == "walmart":
            # Looks like a real Walmart alert; the button opens the ETB listing it would watch.
            msg = AlertMessage(
                kind="test",
                title="🚨 POKÉMON RESTOCK DETECTED",
                text="TEST NOTIFICATION - this is not a real restock. This is what a Walmart alert looks like.",
                product_name="Pokémon TCG: 30th Celebration Elite Trainer Box (TEST)",
                retailer="Walmart",
                store="Online",
                status="AVAILABLE",
                sku="20640569221",
                detected=self.format_time(now),
                url="https://www.walmart.com/ip/20640569221",
                extra={"Seller": "Walmart.com", "UPC": "196214158801",
                       "Verified": "Yes - confirmed by independent re-check"},
            )
            return await self._deliver(f"test:walmart:{now.isoformat()}", msg, None)
        msg = AlertMessage(
            kind="test",
            title="🚨 POKÉMON RESTOCK DETECTED",
            text="TEST NOTIFICATION - this is not a real restock. Your notification channel works.",
            product_name="Pokémon 30th Anniversary Collection (TEST)",
            retailer="Target",
            store="Athens, TN",
            status="AVAILABLE",
            sku="123456789",
            detected=self.format_time(now),
            url="https://www.target.com/",
        )
        return await self._deliver(f"test:{now.isoformat()}", msg, None)

    async def resend_pending(self) -> list[int]:
        """After a crash: deliver confirmed restocks whose alert never went out.

        Only recent events are resent -- an alert about a restock from hours ago
        is noise. Dedupe claims make this safe to run repeatedly.
        """
        cutoff = self.clock() - timedelta(minutes=self.settings.pending_notification_max_age_minutes)
        with self.db.session() as s:
            ids = list(s.scalars(select(Event.id).where(
                Event.event_type == EventType.RESTOCK_CONFIRMED.value,
                Event.notification_sent.is_(False),
                Event.created_at >= cutoff,
            )))
        resent = []
        for event_id in ids:
            report = await self.send_restock_alert(event_id)
            if report.any_sent:
                resent.append(event_id)
        return resent

    def last_notification_at(self) -> datetime | None:
        with self.db.session() as s:
            return s.scalar(select(func.max(Notification.sent_at)).where(
                Notification.status == NotificationStatus.SENT.value))

    def status(self) -> dict:
        return {
            "channels": [c.name for c in self.channels],
            "channel_status": {
                name: {
                    "last_sent_at": v["last_sent_at"].isoformat() if v["last_sent_at"] else None,
                    "last_error": v["last_error"],
                } for name, v in self.channel_status.items()
            },
        }

    async def aclose(self) -> None:
        for c in self.channels:
            await c.aclose()
