"""Shared fixtures. No test touches a live retailer website."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import Settings  # noqa: E402
from app.database import Database  # noqa: E402
from app.monitoring.verifier import Verifier  # noqa: E402
from app.notifications.console import ConsoleNotifier  # noqa: E402
from app.retailers.simulated import SimulatedRetailer  # noqa: E402
from app.runtime import build_runtime  # noqa: E402
from app.services.product_service import ProductCreate, create_product  # noqa: E402
from app.utils.rate_limit import RateLimiter  # noqa: E402


async def _no_sleep(_seconds):
    return None


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        _env_file=None,
        database_url=f"sqlite:///{tmp_path}/test.db",
        verification_delay_seconds=0,
        min_request_interval_seconds=0,
        monitor_enabled=False,
        notify_console=True,
        timezone="America/New_York",
    )


class Harness:
    """A runtime wired to the simulated retailer and a console notifier."""

    def __init__(self, settings: Settings, **overrides):
        self.settings = settings.model_copy(update=overrides) if overrides else settings
        self.sim = SimulatedRetailer(self.settings, RateLimiter("simulated", min_interval=0))
        self.console = ConsoleNotifier()
        self.rt = self._build()

    def _build(self):
        return build_runtime(
            self.settings, db=Database(self.settings.database_url),
            retailer_overrides={"simulated": self.sim}, channels=[self.console],
            verifier=Verifier(self.settings, sleep=_no_sleep), is_simulation=True,
        )

    def restart(self):
        """Simulate a crash: throw away every in-memory object except the DB file."""
        self.rt.db.dispose()
        self.console = ConsoleNotifier()
        self.rt = self._build()
        return self.rt

    def add(self, sku="SKU1", **fields) -> int:
        with self.rt.db.session() as s:
            return create_product(s, ProductCreate(retailer="simulated", sku=sku,
                                                   product_name=fields.pop("product_name", f"Product {sku}"),
                                                   **fields)).id

    async def check(self, pid):
        return await self.rt.monitor.check_product(pid)

    @property
    def alerts(self):
        return [m for m in self.console.sent if m.kind == "restock"]


@pytest.fixture
def harness(settings):
    h = Harness(settings)
    yield h
    h.rt.db.dispose()


@pytest.fixture
def make_harness(settings):
    made = []

    def factory(**overrides):
        h = Harness(settings, **overrides)
        made.append(h)
        return h

    yield factory
    for h in made:
        h.rt.db.dispose()
