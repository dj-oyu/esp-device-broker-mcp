"""A running job keeps its lease; only uncertain failures become needs_review.

Two fixes, tested lowest layer first:

1. Lease expiry no longer revokes a job that was admitted and is running
   (DESIGN-CONTINUATION-TICKETS.md section 5.3). Revocation, cancellation, closing and
   epoch changes still stop it; an expired lease still blocks admission.
2. A device-job failure with a confirmed close and no device write ends `failed`
   (device_failure_outcome.py); it no longer cancels queued work.

Mock backend, synthetic ROM driver and fake clocks only; never a real serial port.
"""

import asyncio
import threading
from types import SimpleNamespace

import pytest
from test_continuation import Clock, request, running, work_session
from test_flash_lease_renewal import setup as flash_setup
from test_flash_policy import env as env
from test_stage05_cancellation import promote_waiting_job, until_running

from cardputer_mcp.backend import DeviceOperationFailure, MockBackend
from cardputer_mcp.broker import BrokerError, ExecutionIntent, WorkSession
from cardputer_mcp.device_failure_outcome import (
    HOME_RECOVERY_SOURCE_FAILURES,
    UNCERTAIN_CODES,
    FailureEvidence,
    classify,
)
from cardputer_mcp.flash_lifecycle import FlashFailure
from cardputer_mcp.home_recovery import CAPTURE_FAILURES
from cardputer_mcp.lease_manager import LeaseManager, Session
from cardputer_mcp.models import (
    AcquireRequest,
    CancelRequest,
    ExecuteRequest,
    Origin,
    PrepareRequest,
)
from cardputer_mcp.models import TestName as Action

# --- 1. classification table (pure) ------------------------------------------------------


@pytest.mark.parametrize("known", [True, False])
@pytest.mark.parametrize("closed", [True, False])
@pytest.mark.parametrize("write", [True, False])
@pytest.mark.parametrize("home", [True, False])
@pytest.mark.parametrize("code", ["serial_marker_timeout", *sorted(UNCERTAIN_CODES)])
def test_classification_table(known, closed, write, home, code):
    evidence = FailureEvidence(code, known, closed, write, home)
    certain = known and closed and not write and not home and code not in UNCERTAIN_CODES
    assert classify(evidence) == ("failed" if certain else "needs_review")


def test_home_recovery_sources_match_home_recovery():
    assert HOME_RECOVERY_SOURCE_FAILURES == CAPTURE_FAILURES


# --- 2. lease revocation flag ------------------------------------------------------------


@pytest.mark.parametrize(
    ("deadline", "until", "expected"), [(100.0, 50.0, 50.0), (100.0, 150.0, 100.0)]
)
def test_revoke_marks_and_never_extends(deadline, until, expected):
    session = Session("c" * 64, "lease-id", "local_cli", deadline)
    LeaseManager.revoke(session, until)
    assert (session.revoked, session.deadline) == (True, expected)
    LeaseManager.renew(session, 500.0)
    assert session.revoked  # a renewal never revives a revoked lease


# --- 3. the running-job lease binding ----------------------------------------------------


def binding(tmp_path, *, work_state=None):
    from cardputer_mcp.broker import Broker
    from cardputer_mcp.config import Settings

    clock = Clock()
    broker = Broker(
        Settings(state_dir=tmp_path / "state", allow_mock_execution=True),
        clock=clock,
        lock_path=tmp_path / "device.lockfile",
    )
    work = None
    if work_state is not None:
        work = WorkSession(
            "r" * 32, "work-1", "local_cli", "alice", frozenset({"read", "control"}), 150.0
        )
        work.state = work_state
        broker.work_sessions[work.id] = work
    lease = Session("c" * 64, "lease-1", "local_cli", 110.0, "alice", work and work.id)
    broker.session = lease
    intent = ExecutionIntent(
        broker.epoch,
        "p" * 64,
        "job-1",
        lease.id,
        "alice",
        "local_cli",
        work and work.id,
        1000.0,
        lease,
        work,
    )
    return broker, clock, lease, intent


@pytest.mark.parametrize(
    ("change", "admission", "running"),
    [
        pytest.param(None, True, True, id="live"),
        pytest.param("deadline", False, True, id="lease-deadline-passed"),
        pytest.param("revoked", False, False, id="revoked-before-deadline"),
        pytest.param("deadline+revoked", False, False, id="revoked-after-deadline"),
        pytest.param("closing", False, False, id="closing"),
        pytest.param("epoch", False, False, id="epoch-changed"),
        pytest.param("replaced", False, False, id="lease-replaced"),
        pytest.param("nonfinite", False, False, id="nonfinite-deadline"),
    ],
)
def test_lease_binding_running_vs_admission(tmp_path, change, admission, running):
    broker, clock, lease, intent = binding(tmp_path)
    if change and "deadline" in change:
        clock.now = 200.0
    if change and "revoked" in change:
        LeaseManager.revoke(lease, clock.now)
    if change == "closing":
        broker.closing = True
    elif change == "epoch":
        broker.epoch = "f" * 32
    elif change == "replaced":
        broker.session = Session("c" * 64, "lease-1", "local_cli", 110.0, "alice")
    elif change == "nonfinite":
        lease.deadline = float("inf")
    assert broker._execution_lease_binding_live(intent) is admission
    assert broker._execution_lease_binding_live(intent, running=True) is running


@pytest.mark.parametrize(
    ("state", "late", "admission", "running"),
    [
        pytest.param("open", False, True, True, id="open"),
        pytest.param("open", True, False, True, id="work-deadline-passed"),
        pytest.param("expired", True, False, True, id="work-expired-by-clock"),
        pytest.param("closed", False, False, False, id="work-closed"),
    ],
)
def test_work_session_clock_does_not_revoke_a_running_job(
    tmp_path, state, late, admission, running
):
    broker, clock, _, intent = binding(tmp_path, work_state=state)
    if late:
        clock.now = 160.0
        broker.session.deadline = 200.0
    assert broker._execution_lease_binding_live(intent) is admission
    assert broker._execution_lease_binding_live(intent, running=True) is running


async def test_maintenance_revokes_on_principal_loss_but_only_shortens_on_clock(tmp_path):
    clock = Clock()
    async with running(tmp_path, clock) as broker:
        work = work_session(broker, "key-a")
        request(broker, work, "key-a")
        lease = broker.session
        clock.now += 4000  # the work session expires by clock
        broker.maintenance_tick()
        assert not lease.revoked
        lease_b = promote_waiting_job(broker)
        broker.principal_valid = lambda *args: False
        broker.maintenance_tick()
        assert lease_b.revoked


# --- 4. running mock job under a job-only lease ------------------------------------------


@pytest.fixture
async def gated(tmp_path):
    clock = Clock()
    backend = MockBackend(0.001)
    backend.gate = asyncio.Event()
    from cardputer_mcp.broker import Broker
    from cardputer_mcp.config import Settings

    broker = Broker(
        Settings(state_dir=tmp_path / "state", allow_mock_execution=True),
        backend=backend,
        clock=clock,
        lock_path=tmp_path / "device.lockfile",
        principal_valid=lambda principal, owner, scopes: True,
    )
    await broker.start()
    try:
        yield SimpleNamespace(broker=broker, clock=clock, backend=backend)
    finally:
        backend.gate.set()
        await broker.close()


def capture_intent(broker, job_id):
    intent = broker._execution_intents[job_id]
    plan = broker._store().plan(intent.plan_hash)
    return intent, plan


@pytest.mark.parametrize("stop", [None, "revoked", "closing", "epoch"])
async def test_running_job_only_lease_survives_its_deadline(gated, stop):
    broker, clock = gated.broker, gated.clock
    lease = promote_waiting_job(broker)
    intent, plan = capture_intent(broker, "b-test")
    await until_running(broker, "b-test")
    clock.now = lease.deadline + 120  # the 60 s job lease has long passed
    broker.maintenance_tick()
    assert broker.session is lease and not lease.revoked
    assert broker._store().job("b-test")["state"] == "running"
    assert broker._running_job_binding_live(plan, intent)
    # The owner can still reach the running job through its (internal) lease.
    with pytest.raises(BrokerError, match="only_queued_jobs_can_be_cancelled"):
        broker.cancel_job_only("b-test", "key-b")
    if stop == "revoked":
        broker.principal_valid = lambda *args: False
        broker.maintenance_tick()
        assert lease.revoked
    elif stop == "closing":
        broker.closing = True
    elif stop == "epoch":
        broker.epoch = "f" * 32
    assert broker._running_job_binding_live(plan, intent) is (stop is None)
    if stop in ("closing", "epoch"):
        broker.closing = False
        return
    gated.backend.gate.set()
    for _ in range(400):
        if broker._store().job("b-test")["state"] != "running":
            break
        await asyncio.sleep(0.005)
    # The mock kind has no running authority callback; its outcome is unchanged.
    assert broker._store().job("b-test")["state"] == "succeeded"
    broker.maintenance_tick()
    assert broker.session is None  # the lease ends with the job


async def test_expired_lease_before_admission_still_prevents_start(gated):
    broker, clock = gated.broker, gated.clock
    token = broker.acquire(AcquireRequest(owner=Origin.LOCAL, lease_seconds=30), principal_id="a")[
        "session"
    ]
    prepared = broker.prepare(PrepareRequest(session=token, job_id="late"))
    broker.execute(ExecuteRequest(session=token, plan_hash=prepared["plan_hash"]))
    clock.now += 31  # expires before the worker admits it
    gated.backend.gate.set()
    await broker.queue.join()
    assert broker.job("late")["state"] == "cancelled"
    assert gated.backend.calls == 0


# --- 5. running flash past its deadline ---------------------------------------------------


def paused_at(env, phase, clock):
    """Pause the synthetic ROM worker thread at `phase`, past the lease deadline."""
    reached, resume = threading.Event(), threading.Event()

    def callback(name):
        if name == phase and not reached.is_set():
            clock.now = 31.0  # the lease deadline (30.0) has passed
            reached.set()
            assert resume.wait(10)

    env.driver.callback = callback
    return reached, resume


async def wait_for(event):
    for _ in range(2000):
        if event.is_set():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("worker did not reach the phase")


@pytest.mark.parametrize(
    ("phase", "stop", "state"),
    [
        pytest.param("write", None, "succeeded", id="deadline-only-commits"),
        pytest.param("write", "revoked", "needs_review", id="revoked-after-write"),
        pytest.param("write", "cancel", "needs_review", id="cancel-after-write"),
        pytest.param("write", "closing", "needs_review", id="closing-after-write"),
        pytest.param("write", "epoch", "needs_review", id="epoch-after-write"),
        pytest.param("probe", "revoked", "failed", id="revoked-before-write"),
        pytest.param("probe", "cancel", "failed", id="cancel-before-write"),
    ],
)
async def test_running_flash_after_deadline(env, phase, stop, state):
    clock = flash_setup(env)
    prepared = env.prepare("late-flash", build_job_id="build-one")
    previous = env.store.firmware_current("fixture")
    before = env.driver.writes
    reached, resume = paused_at(env, phase, clock)
    env.execute(prepared)
    token = env.broker.session
    await wait_for(reached)
    if stop == "revoked":
        env.broker.principal_valid = lambda *args: False
    elif stop == "cancel":
        # The lease is past its deadline, but its running job can still be stopped.
        cancelled = env.broker.cancel(CancelRequest(session=token.capability, job_id="late-flash"))
        assert cancelled["stop_requested"] is True
    elif stop == "closing":
        env.broker.closing = True
    elif stop == "epoch":
        env.broker.epoch = "f" * 32
    resume.set()
    await env.broker.queue.join()
    env.broker.closing = False
    job = env.broker.job("late-flash")
    assert job["state"] == state, job
    assert env.driver.closed
    if state == "succeeded":
        assert env.store.firmware_current("fixture")["completed_job"] == "late-flash"
    else:
        assert env.store.firmware_current("fixture") == previous
        assert env.driver.writes - before == (1 if phase == "write" else 0)
    assert env.store.needs_review() is (state == "needs_review")


# --- 6. failure classification through the broker ---------------------------------------


class FailingBackend(MockBackend):
    def __init__(self, code, close):
        super().__init__(0.001)
        self.code, self.close_mode = code, close
        self.fail_job = "first"
        self.ownership_uncertain = False

    async def execute(self, plan, stage):
        if plan["job_id"] != self.fail_job:
            return await super().execute(plan, stage)
        self.calls += 1
        if self.close_mode == "open":
            self.hardware_connected = True
        elif self.close_mode == "uncertain":
            self.ownership_uncertain = True
        raise DeviceOperationFailure(self.code, step="operation")


@pytest.mark.parametrize("close", ["confirmed", "open", "uncertain"])
@pytest.mark.parametrize("kind", [Action.DIAGNOSTICS, Action.CAPTURE, Action.WORKFLOW])
@pytest.mark.parametrize(
    "code",
    [
        "serial_marker_timeout",
        "serial_read_failed",
        "serial_capture_timeout",
        *sorted(UNCERTAIN_CODES & DeviceOperationFailure.CODES),
    ],
)
async def test_device_failure_outcome_through_the_broker(tmp_path, close, kind, code):
    from cardputer_mcp.broker import Broker
    from cardputer_mcp.config import Settings

    backend = FailingBackend(code, close)
    broker = Broker(
        Settings(state_dir=tmp_path / "state", allow_mock_execution=True),
        backend=backend,
        lock_path=tmp_path / "device.lockfile",
        principal_valid=lambda *args: True,
    )
    await broker.start()
    try:
        token = broker.acquire(AcquireRequest(owner=Origin.LOCAL), principal_id="a")["session"]
        for job_id, test in (("first", kind), ("second", Action.DIAGNOSTICS)):
            prepared = broker.prepare(PrepareRequest(session=token, job_id=job_id, test=test))
            broker.execute(ExecuteRequest(session=token, plan_hash=prepared["plan_hash"]))
        await broker.queue.join()
        first, second = broker.job("first"), broker.job("second")
        certain = close == "confirmed" and kind != Action.WORKFLOW and code not in UNCERTAIN_CODES
        assert first["result"] == code
        if certain:
            assert first["state"] == "failed"
            assert not broker._store().needs_review()
            assert not broker.execution_uncertain
            assert second["state"] == "succeeded"  # queued work is not cancelled
        else:
            assert first["state"] == "needs_review"
            assert (second["state"], second["result"]) == (
                "cancelled",
                "recovery_review_required",
            )
    finally:
        backend.hardware_connected = False
        backend.ownership_uncertain = False
        await broker.close()


@pytest.mark.parametrize("close", ["confirmed", "uncertain"])
@pytest.mark.parametrize(
    ("phase", "write"),
    [("probe", False), ("read", False), ("write", True), ("boot", True)],
)
async def test_flash_failure_outcome_through_the_broker(env, close, phase, write):
    env.configure()
    previous = env.store.firmware_current("fixture")
    first = env.prepare("first", build_job_id="build-one")
    second = env.prepare("second", Action.BACKUP)
    seen = set()

    def callback(name):
        # The pre-write fence read is the first `read`; readback comes after the write.
        if name == phase and name not in seen:
            seen.add(name)
            if close == "uncertain":
                env.driver.uncertain = True
            raise FlashFailure("flash_native_failed_needs_review")

    env.driver.callback = callback
    env.execute(first)
    env.execute(second)
    await env.broker.queue.join()
    env.driver.callback = lambda name: None
    job = env.broker.job("first")
    assert job["result"] == (
        "flash_shutdown_uncertain_needs_review"
        if close == "uncertain"
        else "flash_native_failed_needs_review"
    )
    assert env.store.firmware_current("fixture") == previous
    if close == "confirmed" and not write:
        assert job["state"] == "failed"
        assert not env.store.needs_review()
        assert env.broker.job("second")["state"] == "succeeded"
    else:
        assert job["state"] == "needs_review"
        assert env.broker.job("second")["result"] == "recovery_review_required"
    # Teardown: clear the synthetic uncertainty so the fixture can close the broker.
    env.driver.uncertain = env.driver.active = False
    engine = env.broker.firmware_flash.engine
    engine.uncertain = engine.active = False
    env.broker.execution_uncertain = False
    env.broker.active_job = None
