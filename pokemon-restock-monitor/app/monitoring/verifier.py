"""False-positive protection.

A single AVAILABLE response never triggers an alert. After an initial
detection the verifier waits ``VERIFICATION_DELAY_SECONDS`` and re-checks
(``VERIFICATION_CHECKS`` times). Every re-check goes through the retailer's
rate limiter, so verification can never exceed the retailer's limits.

Outcomes:
* CONFIRMED       -- every re-check still shows alertable availability
* FALSE_POSITIVE  -- a re-check shows a determinate non-alertable status
* INCONCLUSIVE    -- a re-check failed (error / rate limited / unknown);
                     no alert, the next scheduled poll will try again
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

from app.config import Settings
from app.database import utcnow
from app.models.enums import InventoryStatus, VerificationOutcome
from app.retailers.base import Observation, ProductRef, RetailerMonitor
from app.services.inventory_service import Evaluation


@dataclass
class VerificationResult:
    outcome: VerificationOutcome
    reason: str
    started_at: datetime
    verified_at: datetime
    observations: list[Observation] = field(default_factory=list)
    evaluations: list[Evaluation] = field(default_factory=list)

    @property
    def last(self) -> Observation | None:
        return self.observations[-1] if self.observations else None


class Verifier:
    def __init__(self, settings: Settings, sleep=asyncio.sleep, clock=utcnow):
        self.settings = settings
        self._sleep = sleep
        self._clock = clock

    async def verify(self, retailer: RetailerMonitor, product: ProductRef, initial: Observation,
                     evaluate_fn: Callable[[Observation], Evaluation]) -> VerificationResult:
        started = self._clock()
        observations: list[Observation] = []
        evaluations: list[Evaluation] = []

        def result(outcome: VerificationOutcome, reason: str) -> VerificationResult:
            return VerificationResult(outcome, reason, started, self._clock(), observations, evaluations)

        for i in range(max(1, self.settings.verification_checks)):
            await self._sleep(self.settings.verification_delay_seconds)
            obs = await retailer.safe_check(product, verify=True)
            observations.append(obs)
            ev = evaluate_fn(obs)
            evaluations.append(ev)
            if not obs.request_success:
                return result(VerificationOutcome.INCONCLUSIVE,
                              f"verification request {i + 1} failed: {obs.error_kind or ''} {obs.error}".strip())
            if ev.alertable:
                continue
            if ev.status == InventoryStatus.UNKNOWN:
                return result(VerificationOutcome.INCONCLUSIVE,
                              f"verification request {i + 1} returned UNKNOWN")
            why = f"re-check {i + 1} returned {ev.status.value}"
            if ev.ignored_reason:
                why += f" ({ev.ignored_reason})"
            return result(VerificationOutcome.FALSE_POSITIVE, why)
        return result(VerificationOutcome.CONFIRMED,
                      f"{len(observations)} independent re-check(s) confirmed availability")
