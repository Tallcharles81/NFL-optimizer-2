"""Simulation mode: exercise the whole pipeline without a real restock.

    python -m app.main --simulation [--keep-running] [--no-external-notify]

Uses a separate database (data/simulation.db, recreated each run) and the
network-free SimulatedRetailer, but the *real* monitor, verifier, rate
limiter, notification service (including Discord/Telegram/email if
configured -- messages are tagged [SIMULATION]) and dedupe logic. It then
simulates a crash/restart and checks that no duplicate alert is sent.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import func, select

from app.config import Settings
from app.database import Database
from app.models import (
    AvailabilityScope,
    Event,
    EventType,
    InventoryState,
    InventoryStatus,
    Notification,
    NotificationStatus,
    SellerType,
)
from app.monitoring.verifier import Verifier
from app.notifications import build_channels
from app.notifications.console import ConsoleNotifier
from app.retailers.base import Observation
from app.retailers.simulated import SimulatedRetailer
from app.runtime import Runtime, build_runtime
from app.services.product_service import ProductCreate, create_product
from app.utils.logging import log_event
from app.utils.rate_limit import HttpStatusError, RateLimitedError, RateLimiter

logger = logging.getLogger("simulation")
S = InventoryStatus
SIM_DB = "data/simulation.db"


@dataclass
class Check:
    name: str
    passed: bool
    detail: str = ""


class Simulation:
    def __init__(self, settings: Settings, external_notify: bool):
        self.base_settings = settings
        self.settings = settings.model_copy(update={
            "database_url": f"sqlite:///./{SIM_DB}",
            "verification_delay_seconds": 0.5,
            "min_request_interval_seconds": 0.05,
            "monitor_enabled": False,
            "alert_on_unknown_to_available": True,
            "accept_third_party": False,
        })
        self.external_notify = external_notify
        self.checks: list[Check] = []
        self.sim = SimulatedRetailer(self.settings, RateLimiter(
            "simulated", min_interval=self.settings.min_request_interval_seconds,
            circuit_threshold=self.settings.circuit_breaker_threshold, circuit_cooldown=5,
            backoff_base=1, backoff_max=5))
        self.rt: Runtime | None = None
        self.ids: dict[str, int] = {}

    # -- helpers -------------------------------------------------------------------
    def build(self) -> Runtime:
        channels = build_channels(self.settings) if self.external_notify else [ConsoleNotifier()]
        return build_runtime(self.settings, db=Database(self.settings.database_url),
                             retailer_overrides={"simulated": self.sim}, channels=channels,
                             verifier=Verifier(self.settings), is_simulation=True)

    def expect(self, name: str, cond: bool, detail: str = "") -> None:
        self.checks.append(Check(name, bool(cond), detail))
        log_event(logger, "simulation_check", logging.INFO if cond else logging.ERROR,
                  result="PASS" if cond else "FAIL", check=name, detail=detail or None)

    def sent_count(self, product_key: str | None = None) -> int:
        with self.rt.db.session() as s:
            q = select(func.count(Notification.id)).where(
                Notification.status == NotificationStatus.SENT.value, Notification.channel == "console",
                Notification.event_type == EventType.RESTOCK_CONFIRMED.value)
            if product_key:
                q = q.where(Notification.product_id == self.ids[product_key])
            return s.scalar(q) or 0

    def events(self, etype: EventType, product_key: str) -> int:
        with self.rt.db.session() as s:
            return s.scalar(select(func.count(Event.id)).where(
                Event.event_type == etype.value, Event.product_id == self.ids[product_key])) or 0

    def state(self, product_key: str) -> InventoryState:
        with self.rt.db.session() as s:
            return s.scalar(select(InventoryState).where(InventoryState.product_id == self.ids[product_key]))

    async def check(self, key: str):
        return await self.rt.monitor.check_product(self.ids[key])

    def banner(self, text: str) -> None:
        logger.info("\n----- %s -----", text)

    # -- scenario ------------------------------------------------------------------------
    async def run(self) -> bool:
        Path(SIM_DB).parent.mkdir(parents=True, exist_ok=True)
        for suffix in ("", "-wal", "-shm"):
            Path(SIM_DB + suffix).unlink(missing_ok=True)
        self.rt = self.build()

        products = {
            "A": dict(sku="SIM-30TH", product_name="Pokémon 30th Anniversary Collection (SIMULATED)", max_quantity=2),
            "B": dict(sku="SIM-ETB", product_name="Elite Trainer Box (SIMULATED false-positive test)"),
            "C": dict(sku="SIM-3P", product_name="Booster Bundle (SIMULATED third-party only)"),
            "D1": dict(sku="SIM-TIN", product_name="Collector Tin (SIMULATED)", store_id="SIM-ATH",
                       store_name="Athens Target (SIMULATED)", city="Athens", state="TN", zip_code="37303"),
            "D2": dict(sku="SIM-TIN", product_name="Collector Tin (SIMULATED)", store_id="SIM-CLE",
                       store_name="Cleveland Target (SIMULATED)", city="Cleveland", state="TN", zip_code="37311"),
            "E": dict(sku="SIM-ERR", product_name="Premium Collection (SIMULATED error handling)"),
        }
        with self.rt.db.session() as s:
            for key, fields in products.items():
                self.ids[key] = create_product(s, ProductCreate(retailer="simulated", **fields)).id

        self.banner("1. Baseline: everything OUT_OF_STOCK")
        for key, f in products.items():
            self.sim.set_status(f["sku"], S.OUT_OF_STOCK, f.get("store_id"))
            await self.check(key)
        self.expect("baseline sends no alerts", self.sent_count() == 0)

        self.banner("2. OUT_OF_STOCK -> AVAILABLE (restock)")
        self.sim.set_status("SIM-30TH", S.AVAILABLE, quantity=12)
        out = await self.check("A")
        self.expect("restock detected", out.detection is not None)
        self.expect("verification confirmed", out.verification and out.verification.outcome.value == "CONFIRMED",
                    out.verification.reason if out.verification else "")
        self.expect("RESTOCK_CONFIRMED event recorded", self.events(EventType.RESTOCK_CONFIRMED, "A") == 1)
        self.expect("notification fired exactly once", self.sent_count("A") == 1)

        self.banner("3. AVAILABLE -> AVAILABLE (no duplicate alerts)")
        for _ in range(3):
            await self.check("A")
        self.expect("no duplicate alerts while still available", self.sent_count("A") == 1)

        self.banner("4. Crash + restart while product is still AVAILABLE")
        await self.rt.aclose()
        self.rt.db.dispose()
        self.rt = self.build()
        summary = await self.rt.monitor.recover()
        await self.check("A")
        self.expect("state preserved across restart", self.state("A").status == S.AVAILABLE.value
                    and self.state("A").episode_open)
        self.expect("no duplicate alert after restart", self.sent_count("A") == 1, str(summary))

        self.banner("5. AVAILABLE -> OUT_OF_STOCK -> AVAILABLE (second restock)")
        self.sim.set_status("SIM-30TH", S.OUT_OF_STOCK)
        await self.check("A")
        self.expect("sell-out recorded", self.events(EventType.SOLD_OUT, "A") == 1)
        self.sim.set_status("SIM-30TH", S.AVAILABLE)
        await self.check("A")
        self.expect("second restock alerts once more", self.sent_count("A") == 2)

        self.banner("6. False positive: AVAILABLE blip, OUT_OF_STOCK on verification")
        self.sim.queue("SIM-ETB", [S.AVAILABLE])  # poll sees AVAILABLE; verify falls back to OOS
        out = await self.check("B")
        self.expect("false positive detected", out.verification and out.verification.outcome.value == "FALSE_POSITIVE")
        self.expect("no alert for false positive", self.sent_count("B") == 0)
        self.expect("status remains OUT_OF_STOCK", self.state("B").status == S.OUT_OF_STOCK.value)

        self.banner("7. Third-party marketplace listing (ACCEPT_THIRD_PARTY=false)")
        self.sim.set_status("SIM-3P", S.AVAILABLE, seller_type=SellerType.THIRD_PARTY)
        await self.check("C")
        self.expect("third-party listing ignored", self.sent_count("C") == 0
                    and self.events(EventType.THIRD_PARTY_IGNORED, "C") == 1)

        self.banner("8. Store-level: Athens in stock, Cleveland not; online != in-store")
        self.sim.set_status("SIM-TIN", S.AVAILABLE, store_id="SIM-ATH")
        await self.check("D1")
        self.sim.queue("SIM-TIN", [Observation(status=S.AVAILABLE, scope=AvailabilityScope.ONLINE_AVAILABLE,
                                                source="simulated")], store_id="SIM-CLE")
        await self.check("D2")
        self.expect("store restock alerts for Athens", self.sent_count("D1") == 1)
        self.expect("online availability not reported for Cleveland store", self.sent_count("D2") == 0
                    and self.state("D2").status == S.UNKNOWN.value)

        self.banner("9. HTTP error, rate limit (429), unknown status")
        self.sim.queue("SIM-ERR", [HttpStatusError("HTTP 500 (simulated)", 500)])
        await self.check("E")
        self.expect("HTTP error does not change status", self.state("E").status == S.OUT_OF_STOCK.value
                    and self.state("E").consecutive_errors == 1)
        self.sim.queue("SIM-ERR", [RateLimitedError("HTTP 429 (simulated)", retry_after=1.0)])
        await self.check("E")
        paused = self.sim.limiter.is_paused()
        out = await self.check("E")
        self.expect("429 pauses the retailer (Retry-After honored)", paused and out.skipped is not None,
                    out.skipped or "")
        await asyncio.sleep(1.1)
        self.sim.queue("SIM-ERR", [Observation(status=S.UNKNOWN, source="simulated")])
        await self.check("E")
        self.expect("unknown status is not treated as OUT_OF_STOCK", self.state("E").status == S.UNKNOWN.value
                    and self.sent_count("E") == 0)
        self.sim.set_status("SIM-ERR", S.AVAILABLE)
        out = await self.check("E")
        self.expect("UNKNOWN -> AVAILABLE alerts, marked as such",
                    self.sent_count("E") == 1 and out.detection and out.detection.kind == "UNKNOWN_TO_AVAILABLE")

        total = self.sent_count()
        self.expect("total restock alerts == 4 (A×2, Athens×1, E×1)", total == 4, f"got {total}")
        with self.rt.db.session() as s:
            ext = s.scalar(select(func.count(Notification.id)).where(
                Notification.channel != "console", Notification.status == NotificationStatus.SENT.value)) or 0
            failed = list(s.execute(select(Notification.channel, Notification.error).where(
                Notification.status == NotificationStatus.FAILED.value)))
        self.external_summary = (ext, failed)
        return all(c.passed for c in self.checks)

    def report(self) -> str:
        lines = ["", "=" * 70, "SIMULATION REPORT", "=" * 70]
        for c in self.checks:
            lines.append(f"  [{'PASS' if c.passed else 'FAIL'}] {c.name}" + (f"  ({c.detail})" if c.detail else ""))
        passed = sum(c.passed for c in self.checks)
        lines.append("-" * 70)
        lines.append(f"  {passed}/{len(self.checks)} checks passed")
        channels = [c.name for c in self.rt.notifications.channels]
        lines.append(f"  Notification channels used: {', '.join(channels)}")
        ext, failed = getattr(self, "external_summary", (0, []))
        if any(c != "console" for c in channels):
            lines.append(f"  External notifications delivered: {ext}")
        for ch, err in failed:
            lines.append(f"  FAILED on {ch}: {err}")
        lines.append(f"  Simulation database: {SIM_DB}")
        lines.append("=" * 70)
        return "\n".join(lines)


async def run_simulation(settings: Settings, keep_running: bool = False, external_notify: bool = True,
                         host: str | None = None, port: int | None = None) -> int:
    sim = Simulation(settings, external_notify)
    try:
        ok = await sim.run()
    finally:
        if sim.rt:
            print(sim.report())
            await sim.rt.aclose()
    if keep_running:
        import uvicorn

        from app.main import create_app

        rt = sim.build()
        app = create_app(sim.settings, runtime=rt)
        print(f"Serving simulation dashboard on http://{host or settings.host}:{port or settings.port}/dashboard")
        config = uvicorn.Config(app, host=host or settings.host, port=port or settings.port, log_config=None)
        await uvicorn.Server(config).serve()
    return 0 if ok else 1
