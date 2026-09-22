"""False-positive protection (case 4) and verification behaviour."""

from sqlalchemy import func, select

from app.models import Event, EventType, InventoryCheck, InventoryState, InventoryStatus as S
from app.utils.rate_limit import HttpStatusError, RateLimitedError


async def test_false_positive_does_not_alert(harness):
    pid = harness.add("A")
    harness.sim.set_status("A", S.OUT_OF_STOCK)
    await harness.check(pid)
    harness.sim.queue("A", [S.AVAILABLE])  # one flicker; the verification re-check sees OOS
    out = await harness.check(pid)

    assert out.detection is not None
    assert out.verification.outcome.value == "FALSE_POSITIVE"
    assert harness.alerts == []
    with harness.rt.db.session() as s:
        assert s.scalar(select(func.count(Event.id)).where(Event.event_type == EventType.FALSE_POSITIVE.value)) == 1
        assert s.scalar(select(func.count(Event.id)).where(
            Event.event_type == EventType.RESTOCK_CONFIRMED.value)) == 0
        st = s.scalar(select(InventoryState).where(InventoryState.product_id == pid))
        assert st.status == "OUT_OF_STOCK" and not st.episode_open
        purposes = list(s.scalars(select(InventoryCheck.purpose).where(InventoryCheck.product_id == pid)))
    assert purposes.count("VERIFY") == 1


async def test_verification_error_is_inconclusive_and_retried(harness):
    pid = harness.add("A")
    harness.sim.set_status("A", S.OUT_OF_STOCK)
    await harness.check(pid)
    harness.sim.set_status("A", S.AVAILABLE)
    harness.sim.queue("A", [S.AVAILABLE, HttpStatusError("HTTP 503", 503)])
    out = await harness.check(pid)
    assert out.verification.outcome.value == "INCONCLUSIVE"
    assert harness.alerts == []
    with harness.rt.db.session() as s:
        st = s.scalar(select(InventoryState).where(InventoryState.product_id == pid))
        assert st.status == "OUT_OF_STOCK"  # unverified change never becomes the confirmed status
    # next poll re-detects and now verifies
    out = await harness.check(pid)
    assert out.verification.outcome.value == "CONFIRMED"
    assert out.detection.kind == "OUT_OF_STOCK_TO_AVAILABLE"
    assert len(harness.alerts) == 1


async def test_verification_respects_rate_limit(harness):
    pid = harness.add("A")
    harness.sim.set_status("A", S.OUT_OF_STOCK)
    await harness.check(pid)
    harness.sim.set_status("A", S.AVAILABLE)
    harness.sim.queue("A", [S.AVAILABLE, RateLimitedError("HTTP 429", retry_after=60)])
    out = await harness.check(pid)
    assert out.verification.outcome.value == "INCONCLUSIVE"
    assert harness.sim.limiter.is_paused()
    before = harness.sim.request_count
    out = await harness.check(pid)  # retailer paused -> no request at all
    assert out.skipped and "paused" in out.skipped
    assert harness.sim.request_count == before
    assert harness.alerts == []


async def test_multiple_verification_checks(make_harness):
    h = make_harness(verification_checks=3)
    pid = h.add("A")
    h.sim.set_status("A", S.OUT_OF_STOCK)
    await h.check(pid)
    h.sim.set_status("A", S.AVAILABLE)
    h.sim.queue("A", [S.AVAILABLE, S.AVAILABLE, S.AVAILABLE, S.OUT_OF_STOCK])
    out = await h.check(pid)
    assert out.verification.outcome.value == "FALSE_POSITIVE"
    assert len(out.verification.observations) == 3
    assert h.alerts == []


async def test_pending_verification_recovered_after_crash(harness):
    pid = harness.add("A")
    harness.sim.set_status("A", S.OUT_OF_STOCK)
    await harness.check(pid)
    # Simulate a crash mid-verification
    with harness.rt.db.session() as s:
        st = s.scalar(select(InventoryState).where(InventoryState.product_id == pid))
        st.alert_state = "PENDING_VERIFICATION"
    rt = harness.restart()
    summary = await rt.monitor.recover()
    assert summary["reset_pending_verifications"] == 1
    harness.sim.set_status("A", S.AVAILABLE)
    out = await harness.check(pid)
    assert out.verification.outcome.value == "CONFIRMED"
    assert len(harness.alerts) == 1
