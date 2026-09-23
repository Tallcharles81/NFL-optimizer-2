"""Wires the application's long-lived components together."""

from __future__ import annotations

from dataclasses import dataclass, field

from app.config import Settings
from app.database import Database
from app.monitoring.monitor import InventoryMonitor
from app.monitoring.scheduler import MonitorScheduler
from app.monitoring.verifier import Verifier
from app.notifications.base import Notifier
from app.retailers.base import RetailerMonitor
from app.retailers.registry import RetailerManager
from app.services.notification_service import NotificationService
from app.services.product_service import ensure_retailers, sync_config_stores


@dataclass
class Runtime:
    settings: Settings
    db: Database
    retailers: RetailerManager
    notifications: NotificationService
    monitor: InventoryMonitor
    scheduler: MonitorScheduler
    # One-shot mode: consecutive rounds per retailer that returned no stock status.
    unreadable_streak: dict[str, int] = field(default_factory=dict)

    async def aclose(self) -> None:
        await self.scheduler.shutdown()
        await self.retailers.aclose()
        await self.notifications.aclose()


def build_runtime(settings: Settings, db: Database | None = None,
                  retailer_overrides: dict[str, RetailerMonitor] | None = None,
                  channels: list[Notifier] | None = None, verifier: Verifier | None = None,
                  is_simulation: bool = False) -> Runtime:
    db = db or Database(settings.database_url)
    db.create_all()
    with db.session() as s:
        ensure_retailers(s, include_simulated=is_simulation)
        sync_config_stores(s, settings)
    retailers = RetailerManager(settings, overrides=retailer_overrides)
    notifications = NotificationService(settings, db, channels=channels)
    monitor = InventoryMonitor(settings, db, retailers, notifications, verifier=verifier,
                               is_simulation=is_simulation)
    scheduler = MonitorScheduler(settings, db, monitor)
    return Runtime(settings, db, retailers, notifications, monitor, scheduler)
