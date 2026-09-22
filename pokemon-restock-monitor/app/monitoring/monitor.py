"""The inventory monitor: check -> detect -> verify -> alert.

State machine per product (persisted in ``inventory_states``):

* ``status`` is the last *confirmed* status. ERROR responses never
  overwrite it, and an unverified AVAILABLE doesn't either.
* A restock *episode* opens when an alertable status is detected AND
  verified. While the episode is open, repeated AVAILABLE results never
  alert again. It closes only on a determinate non-alertable status
  (OUT_OF_STOCK, UNAVAILABLE, third-party-only ...), never on UNKNOWN/ERROR.
* Each episode has a number; the alert dedupe key includes it, so
  AVAILABLE -> OUT_OF_STOCK -> AVAILABLE produces exactly one new alert.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import select

from app.config import Settings
from app.database import Database, utcnow
from app.models import (
    AlertState,
    Event,
    EventType,
    InventoryState,
    InventoryStatus,
    PollMode,
    Product,
    Retailer,
    RetailerHealth,
    VerificationOutcome,
)
from app.monitoring.scheduler import PollingPolicy
from app.monitoring.verifier import VerificationResult, Verifier
from app.retailers.base import Observation, ProductRef, RetailerMonitor
from app.retailers.registry import RetailerManager
from app.services.inventory_service import (
    Evaluation,
    add_event,
    arrow,
    evaluate,
    get_or_create_state,
    log_check,
    ms_between,
    record_check,
    transition_kind,
)
from app.services.notification_service import NotificationService
from app.utils.logging import log_event

logger = logging.getLogger("monitor")


@dataclass
class Detection:
    event_id: int
    episode: int
    previous_status: str
    previous_seller: str | None
    kind: str
    observation: Observation
    previous_success_at: datetime | None


@dataclass
class CheckOutcome:
    product_id: int
    skipped: str | None = None
    observation: Observation | None = None
    evaluation: Evaluation | None = None
    previous_status: str | None = None
    status: str | None = None
    detection: Detection | None = None
    verification: VerificationResult | None = None
    confirmed_event_id: int | None = None
    notified_channels: list[str] = field(default_factory=list)


class InventoryMonitor:
    def __init__(self, settings: Settings, db: Database, retailers: RetailerManager,
                 notifications: NotificationService, verifier: Verifier | None = None,
                 policy: PollingPolicy | None = None, clock=utcnow, is_simulation: bool = False):
        self.settings = settings
        self.db = db
        self.retailers = retailers
        self.notifications = notifications
        self.verifier = verifier or Verifier(settings)
        self.policy = policy or PollingPolicy(settings)
        self.clock = clock
        self.is_simulation = is_simulation
        self.last_successful_check_at: datetime | None = None

    # ------------------------------------------------------------------------------
    async def check_product(self, product_id: int) -> CheckOutcome:
        outcome = CheckOutcome(product_id)

        # 1. Snapshot (no session held during network I/O)
        with self.db.session() as s:
            product = s.get(Product, product_id)
            if product is None or not product.enabled:
                outcome.skipped = "missing or disabled"
                return outcome
            if not product.retailer.enabled:
                outcome.skipped = "retailer disabled"
                return outcome
            ref = ProductRef.from_model(product, product.retailer.slug)
            state = get_or_create_state(s, product)
            prev_status = state.status
            prev_seller = state.seller_type
            prev_success_at = state.last_success_at
            if state.alert_state == AlertState.PENDING_VERIFICATION.value:
                outcome.skipped = "verification already in progress"
                return outcome

        retailer = self.retailers.get(ref.retailer)
        if retailer.limiter.is_paused():
            # Don't even build a request while the retailer is paused.
            with self.db.session() as s:
                state = s.scalar(select(InventoryState).where(InventoryState.product_id == product_id))
                state.next_check_at = self.clock() + timedelta(
                    seconds=max(retailer.limiter.seconds_until_available(), 1) + 1)
                self._sync_retailer_row(s, retailer)
            outcome.skipped = f"retailer paused: {retailer.limiter.pause_reason}"
            return outcome

        # 2. Ask the retailer
        obs = await retailer.safe_check(ref)
        ev = evaluate(obs, ref, self.settings)
        outcome.observation, outcome.evaluation, outcome.previous_status = obs, ev, prev_status

        # 3. Persist and decide
        system_alerts: list[tuple[str, str, str]] = []
        with self.db.session() as s:
            state = s.scalar(select(InventoryState).where(InventoryState.product_id == product_id))
            now = self.clock()
            record_check(s, ref, obs, prev_status, "POLL", ev)
            system_alerts += self._sync_retailer_row(s, retailer)
            state.last_checked_at = now
            if not obs.request_success:
                self._handle_error(s, ref, state, obs)
            else:
                self.last_successful_check_at = now
                outcome.detection = self._apply(s, ref, state, obs, ev, prev_status, prev_seller, prev_success_at)
            retailer_row = s.scalar(select(Retailer).where(Retailer.slug == ref.retailer))
            state.next_check_at = now + timedelta(seconds=self.policy.next_interval(
                state, ref, retailer_row, type(retailer), now, retry_after=obs.retry_after))
            outcome.status = state.status
        log_check(ref, obs, prev_status, ev.status.value if obs.request_success else InventoryStatus.ERROR.value,
                  "POLL", "PENDING" if outcome.detection else None)

        for key, title, text in system_alerts:
            await self.notifications.send_system_alert(key, title, text)

        # 4. Verify + alert
        if outcome.detection is not None:
            try:
                await self._verify_and_alert(ref, retailer, outcome)
            except Exception:
                logger.exception("Verification/alert failed for product %s", product_id)
                with self.db.session() as s:
                    state = s.scalar(select(InventoryState).where(InventoryState.product_id == product_id))
                    if state.alert_state == AlertState.PENDING_VERIFICATION.value:
                        state.alert_state = AlertState.INCONCLUSIVE.value
                raise
        return outcome

    # ------------------------------------------------------------------------------
    def _apply(self, s, ref: ProductRef, state: InventoryState, obs: Observation, ev: Evaluation,
               prev_status: str, prev_seller: str | None, prev_success_at: datetime | None) -> Detection | None:
        now = self.clock()
        state.consecutive_errors = 0
        state.last_error = None
        state.last_error_kind = None
        state.last_success_at = now
        state.poll_mode = PollMode.NORMAL.value
        new_status = ev.status.value

        # Ignored-listing bookkeeping (event once per change, not every poll)
        if ev.ignored_reason != state.last_ignored_reason and ev.ignored_reason:
            etype = (EventType.STORE_DATA_UNAVAILABLE if ev.ignored_reason == "STORE_DATA_UNAVAILABLE"
                     else EventType.THIRD_PARTY_IGNORED)
            add_event(s, etype, ref, previous_status=prev_status, new_status=new_status,
                      message=ev.note, is_simulation=self.is_simulation,
                      details={"seller_name": obs.seller_name, "seller_type": obs.seller_type.value})
        state.last_ignored_reason = ev.ignored_reason

        # --- Possible restock: start detection, don't touch confirmed status yet
        if ev.alertable and not state.episode_open:
            kind = transition_kind(prev_status, new_status, prev_seller)
            if prev_status == InventoryStatus.UNKNOWN.value and not self.settings.alert_on_unknown_to_available:
                self._set_observed(state, obs, ev, now, prev_status)
                state.episode_open = True
                state.alert_state = AlertState.SUPPRESSED.value
                state.restock_episode += 1
                state.episode_started_at = now
                add_event(s, EventType.STATUS_CHANGE, ref, previous_status=prev_status, new_status=new_status,
                          transition=kind, is_simulation=self.is_simulation,
                          message="First observation is available; alert suppressed "
                                  "(ALERT_ON_UNKNOWN_TO_AVAILABLE=false).")
                return None
            state.restock_episode += 1
            state.alert_state = AlertState.PENDING_VERIFICATION.value
            state.recently_active_until = now + timedelta(minutes=self.settings.recently_active_window_minutes)
            detected = add_event(
                s, EventType.RESTOCK_DETECTED, ref,
                previous_status=prev_status, new_status=new_status, transition=kind,
                restock_episode=state.restock_episode,
                message=f"{arrow(prev_status, new_status)} detected; verifying",
                is_simulation=self.is_simulation,
                inventory_changed_at=obs.inventory_changed_at,
                detected_at=obs.checked_at,
                detection_latency_ms=ms_between(obs.checked_at, obs.inventory_changed_at),
                detection_window_ms=ms_between(obs.checked_at, prev_success_at),
                details={"seller_name": obs.seller_name, "seller_type": obs.seller_type.value,
                         "price": obs.price, "quantity": obs.quantity, "source": obs.source},
            )
            return Detection(detected.id, state.restock_episode, prev_status, prev_seller, kind, obs,
                             prev_success_at)

        # --- Everything else updates the confirmed status directly
        if new_status != prev_status:
            add_event(s, EventType.STATUS_CHANGE, ref, previous_status=prev_status, new_status=new_status,
                      transition=f"{prev_status}_TO_{new_status}", is_simulation=self.is_simulation,
                      message=ev.note)
        self._set_observed(state, obs, ev, now, prev_status)

        if ev.closes_episode and state.episode_open:
            available_for = (now - state.episode_started_at).total_seconds() if state.episode_started_at else None
            add_event(s, EventType.SOLD_OUT, ref, previous_status=prev_status, new_status=new_status,
                      restock_episode=state.restock_episode, is_simulation=self.is_simulation,
                      message=f"{arrow(prev_status, new_status)}; restock episode {state.restock_episode} ended",
                      details={"available_for_seconds": available_for, "reason": ev.ignored_reason})
            state.episode_open = False
            state.alert_state = AlertState.NONE.value
            state.recently_active_until = now + timedelta(minutes=self.settings.recently_active_window_minutes)
        elif state.episode_open and state.alert_state == AlertState.CONFIRMED.value:
            state.poll_mode = PollMode.AVAILABLE_HOLD.value
        return None

    @staticmethod
    def _set_observed(state: InventoryState, obs: Observation, ev: Evaluation, now: datetime,
                      prev_status: str) -> None:
        if ev.status.value != prev_status:
            state.last_changed_at = now
        state.status = ev.status.value
        state.scope = ev.scope.value
        state.seller_type = obs.seller_type.value
        state.seller_name = obs.seller_name
        state.quantity = obs.quantity
        state.price = obs.price
        state.message = ev.note

    def _handle_error(self, s, ref: ProductRef, state: InventoryState, obs: Observation) -> None:
        new_streak = state.consecutive_errors == 0 or state.last_error_kind != obs.error_kind
        state.consecutive_errors += 1
        state.last_error = obs.error
        state.last_error_kind = obs.error_kind
        state.poll_mode = PollMode.ERROR.value
        # NOTE: state.status is deliberately left unchanged -- an error is not OUT_OF_STOCK.
        if obs.error_kind == "RATE_LIMITED":
            add_event(s, EventType.RATE_LIMITED, ref, message=obs.error, is_simulation=self.is_simulation,
                      details={"retry_after": obs.retry_after})
        elif new_streak and obs.error_kind not in ("RETAILER_PAUSED", "BOT_CHALLENGE"):
            add_event(s, EventType.CHECK_ERROR, ref, message=obs.error, is_simulation=self.is_simulation,
                      details={"error_kind": obs.error_kind, "http_status": obs.http_status})

    def _sync_retailer_row(self, s, retailer: RetailerMonitor) -> list[tuple[str, str, str]]:
        """Persist limiter state; returns system alerts to send for new blocks/suspensions."""
        row = s.scalar(select(Retailer).where(Retailer.slug == retailer.slug))
        if row is None:
            return []
        lim = retailer.limiter
        old_health = row.health
        now = self.clock()
        row.health = lim.health
        row.consecutive_failures = lim.consecutive_failures
        row.paused_until = lim.paused_until if lim.is_paused() else None
        row.pause_reason = lim.pause_reason
        if lim.health == RetailerHealth.OK.value:
            row.last_success_at = now
        elif lim.health != RetailerHealth.IDLE.value:
            row.last_error = lim.pause_reason or f"{lim.consecutive_failures} consecutive failures"
            row.last_error_at = now
        alerts = []
        if lim.health != old_health and lim.health in (RetailerHealth.BLOCKED.value, RetailerHealth.SUSPENDED.value):
            etype = EventType.RETAILER_BLOCKED if lim.health == RetailerHealth.BLOCKED.value \
                else EventType.RETAILER_SUSPENDED
            add_event(s, etype, None, retailer=retailer.slug, message=lim.pause_reason,
                      is_simulation=self.is_simulation,
                      details={"paused_until": lim.paused_until.isoformat() if lim.paused_until else None})
            title = f"{retailer.display_name} monitoring paused ({lim.health})"
            text = (f"{lim.pause_reason}. Monitoring for {retailer.display_name} is paused until "
                    f"{lim.paused_until.isoformat() if lim.paused_until else 'manually resumed'}. "
                    "No attempt is made to bypass retailer protections.")
            alerts.append((f"{retailer.slug}:{lim.health}:{now:%Y%m%d%H}", title, text))
        return alerts

    # ------------------------------------------------------------------------------
    async def _verify_and_alert(self, ref: ProductRef, retailer: RetailerMonitor, outcome: CheckOutcome) -> None:
        det = outcome.detection
        result = await self.verifier.verify(retailer, ref, det.observation,
                                            lambda o: evaluate(o, ref, self.settings))
        outcome.verification = result
        confirmed_id = None
        with self.db.session() as s:
            state = s.scalar(select(InventoryState).where(InventoryState.product_id == ref.id))
            now = self.clock()
            for vobs, vev in zip(result.observations, result.evaluations):
                record_check(s, ref, vobs, det.previous_status, "VERIFY", vev)
            self._sync_retailer_row(s, retailer)
            detected_event = s.get(Event, det.event_id)
            detected_event.verified_at = result.verified_at
            detected_event.verification_latency_ms = ms_between(result.verified_at, det.observation.checked_at)
            detected_event.details = {**(detected_event.details or {}), "verification": result.outcome.value,
                                      "verification_reason": result.reason}
            last, last_ev = result.last, (result.evaluations[-1] if result.evaluations else None)

            if result.outcome == VerificationOutcome.CONFIRMED:
                self._set_observed(state, last, last_ev, now, det.previous_status)
                state.episode_open = True
                state.alert_state = AlertState.CONFIRMED.value
                state.episode_started_at = det.observation.checked_at
                state.poll_mode = PollMode.AVAILABLE_HOLD.value
                store_key = ref.store_id or "online"
                retailer_obj = self.retailers.get(ref.retailer)
                confirmed = add_event(
                    s, EventType.RESTOCK_CONFIRMED, ref,
                    previous_status=det.previous_status, new_status=last_ev.status.value,
                    transition=det.kind, restock_episode=det.episode,
                    dedupe_key=f"restock:{ref.id}:{store_key}:ep{det.episode}",
                    message=f"{arrow(det.previous_status, last_ev.status.value)} VERIFIED ({result.reason})",
                    is_simulation=self.is_simulation,
                    inventory_changed_at=det.observation.inventory_changed_at,
                    detected_at=det.observation.checked_at,
                    verified_at=result.verified_at,
                    detection_latency_ms=ms_between(det.observation.checked_at, det.observation.inventory_changed_at),
                    detection_window_ms=ms_between(det.observation.checked_at, det.previous_success_at),
                    verification_latency_ms=ms_between(result.verified_at, det.observation.checked_at),
                    details={
                        "detected_event_id": det.event_id,
                        "seller_name": last.seller_name,
                        "seller_type": last.seller_type.value,
                        "price": last.price,
                        "quantity": last.quantity,
                        "source": last.source,
                        "product_url": retailer_obj.get_product_url(ref),
                    },
                )
                confirmed_id = confirmed.id
                retailer_row = s.scalar(select(Retailer).where(Retailer.slug == ref.retailer))
                state.next_check_at = now + timedelta(seconds=self.policy.next_interval(
                    state, ref, retailer_row, type(retailer), now))
            elif result.outcome == VerificationOutcome.FALSE_POSITIVE:
                if last is not None and last.request_success and last_ev.status != InventoryStatus.UNKNOWN:
                    if last_ev.status.value != det.previous_status:
                        add_event(s, EventType.STATUS_CHANGE, ref, previous_status=det.previous_status,
                                  new_status=last_ev.status.value, is_simulation=self.is_simulation,
                                  transition=f"{det.previous_status}_TO_{last_ev.status.value}")
                    self._set_observed(state, last, last_ev, now, det.previous_status)
                state.alert_state = AlertState.FALSE_POSITIVE.value
                state.episode_open = False
                add_event(s, EventType.FALSE_POSITIVE, ref, previous_status=det.previous_status,
                          new_status=last_ev.status.value if last_ev else None, transition=det.kind,
                          restock_episode=det.episode, is_simulation=self.is_simulation,
                          detected_at=det.observation.checked_at, verified_at=result.verified_at,
                          verification_latency_ms=ms_between(result.verified_at, det.observation.checked_at),
                          message=f"Initial {det.observation.status.value} not confirmed: {result.reason}")
            else:
                state.alert_state = AlertState.INCONCLUSIVE.value
                state.episode_open = False
                add_event(s, EventType.VERIFICATION_INCONCLUSIVE, ref, previous_status=det.previous_status,
                          new_status=det.observation.status.value, transition=det.kind,
                          restock_episode=det.episode, is_simulation=self.is_simulation,
                          message=f"Could not verify ({result.reason}); will re-check on the next poll. No alert sent.")
            outcome.status = state.status

        log_check(ref, result.last or det.observation, det.previous_status,
                  (result.evaluations[-1].status.value if result.evaluations else "?"), "VERIFY",
                  {"CONFIRMED": "VERIFIED"}.get(result.outcome.value, result.outcome.value))

        if confirmed_id is not None:
            outcome.confirmed_event_id = confirmed_id
            report = await self.notifications.send_restock_alert(confirmed_id)
            outcome.notified_channels = report.sent
            log_event(logger, "restock_confirmed", logging.WARNING, retailer=ref.retailer.upper(), sku=ref.sku,
                      store=ref.location_label, transition=det.kind, channels=",".join(report.sent) or "none",
                      failed=",".join(report.failed) or None)

    # ------------------------------------------------------------------------------
    async def recover(self) -> dict:
        """Run at startup: repair state left behind by a crash."""
        summary = {"reset_pending_verifications": 0, "resent_notifications": 0, "restored_pauses": 0}
        with self.db.session() as s:
            now = self.clock()
            for state in s.scalars(select(InventoryState).where(
                    InventoryState.alert_state == AlertState.PENDING_VERIFICATION.value)):
                # Status was never updated for an unverified detection, so a fresh
                # poll will re-detect and re-verify correctly.
                state.alert_state = AlertState.INCONCLUSIVE.value
                state.next_check_at = now
                summary["reset_pending_verifications"] += 1
            for row in s.scalars(select(Retailer)):
                if row.min_request_interval_seconds:
                    self.retailers.set_min_interval(row.slug, row.min_request_interval_seconds)
                if row.paused_until and row.paused_until > now:
                    try:
                        lim = self.retailers.get(row.slug).limiter
                    except KeyError:
                        continue
                    lim.restore(row.paused_until, row.health, row.pause_reason, row.consecutive_failures)
                    summary["restored_pauses"] += 1
            last = s.scalar(select(InventoryState.last_success_at).order_by(
                InventoryState.last_success_at.desc().nulls_last()).limit(1))
            self.last_successful_check_at = last
        resent = await self.notifications.resend_pending()
        summary["resent_notifications"] = len(resent)
        with self.db.session() as s:
            add_event(s, EventType.MONITOR_RECOVERED if any(summary.values()) else EventType.MONITOR_STARTED,
                      None, message="Monitor started", details=summary, is_simulation=self.is_simulation)
        log_event(logger, "monitor_recovered", **summary)
        return summary
