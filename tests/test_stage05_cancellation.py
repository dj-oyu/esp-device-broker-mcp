"""Characterization (stage 0.5): cancellation entry points, job-lease ends and ownership.

Pins today's reason strings and refusals so a unified cancellation path and a
LeaseManager keep them (``docs/redesign/contracts/jobs.md`` sections 3 and 4).
Mock backend only; no hardware.
"""

import asyncio
from types import SimpleNamespace

import pytest
from test_continuation import Clock, request, running, work_session
from test_deferred_device import defer, fake_parent, hold_for_a, settle

from cardputer_mcp.broker import DEVICE_WAIT_SECONDS, BrokerError
from cardputer_mcp.models import (
    CloseWorkSessionRequest,
    ExecuteRequest,
    LeaseRequest,
    PrepareRequest,
)

# --- helpers -----------------------------------------------------------------------------


def promote_waiting_job(broker, principal="key-b"):
    """A deferred job whose parent succeeded; one tick grants it a job-only lease."""
    ticket = fake_parent(broker, principal, "b-build")
    defer(broker, principal, ticket, "b-test")
    broker._store().set_job("b-build", "succeeded", "ok")
    broker.maintenance_tick()
    lease = broker.session
    assert lease is not None and lease.job_id == "b-test" and lease.principal_id == principal
    return lease


def queue_job(broker, lease, job_id="queued-job"):
    plan = broker.prepare(PrepareRequest(session=lease, job_id=job_id))
    broker.execute(ExecuteRequest(session=lease, plan_hash=plan["plan_hash"]))
    assert broker._store().job(job_id)["state"] == "queued"
    return job_id


async def until_running(broker, job_id):
    for _ in range(2000):
        job = broker._store().job(job_id)
        if job is not None and job["state"] == "running":
            return
        await asyncio.sleep(0.005)
    raise AssertionError(f"{job_id} did not start")


def request_row(broker, request_id):
    row = broker._store().request(request_id)
    return row["state"], row["reason"]


# --- cancel_job_only (cancel without a session) ------------------------------------------


@pytest.mark.parametrize(
    ("job_id", "principal", "cancels"),
    [
        pytest.param("b-test", "key-b", True, id="owner"),
        pytest.param("b-test", "key-a", False, id="other-principal"),
        pytest.param("other-job", "key-b", False, id="other-job"),
    ],
)
async def test_cancel_job_only_goes_through_the_job_lease(tmp_path, job_id, principal, cancels):
    clock = Clock()
    async with running(tmp_path, clock) as broker:
        lease = promote_waiting_job(broker)
        assert broker._store().job("b-test")["state"] == "queued"
        result = broker.cancel_job_only(job_id, principal)
        job = broker._store().job("b-test")
        if cancels:
            assert result is not None
            assert (result["state"], result["result"]) == ("cancelled", "client_cancelled_queued")
            assert (job["state"], job["result"]) == ("cancelled", "client_cancelled_queued")
        else:
            assert result is None
            assert job["state"] == "queued"
        assert broker.session is lease  # cancelling never ends ownership by itself
        await broker.queue.join()
        if cancels:
            assert broker.backend.calls == 0


async def test_cancel_job_only_without_any_lease_returns_none(tmp_path):
    clock = Clock()
    async with running(tmp_path, clock) as broker:
        assert broker.session is None
        assert broker.cancel_job_only("b-test", "key-b") is None
        # A work-session lease is not a job lease either.
        work = work_session(broker, "key-a")
        lease = request(broker, work, "key-a")["lease"]["session"]
        queue_job(broker, lease, "ws-job")
        assert broker.cancel_job_only("ws-job", "key-a") is None
        assert broker._store().job("ws-job")["state"] == "queued"


async def test_cancel_job_only_on_a_running_diagnostic_is_refused(tmp_path):
    clock = Clock()
    async with running(tmp_path, clock) as broker:
        gate = broker.backend.gate = asyncio.Event()
        try:
            promote_waiting_job(broker)
            await until_running(broker, "b-test")
            with pytest.raises(BrokerError, match="^only_queued_jobs_can_be_cancelled$"):
                broker.cancel_job_only("b-test", "key-b")
        finally:
            gate.set()  # never leave the worker parked, or close() waits forever
        assert (await settle(broker, "b-test"))["state"] == "succeeded"


# --- job-lease endings: job_complete / promotion_failed / wait_cancelled -----------------


@pytest.mark.parametrize("ending", ["succeeded", "cancelled"])
async def test_job_lease_ends_with_job_complete(tmp_path, ending):
    clock = Clock()
    async with running(tmp_path, clock) as broker:
        lease = promote_waiting_job(broker)
        if ending == "cancelled":
            broker.cancel_job_only("b-test", "key-b")
            broker.maintenance_tick()
        else:
            await settle(broker, "b-test")
            await broker.queue.join()
            broker.maintenance_tick()
        assert broker._store().job("b-test")["state"] == ending
        assert broker.session is None
        assert request_row(broker, lease.request_id) == ("released", "job_complete")


@pytest.mark.parametrize(
    ("failure", "deferred_reason"),
    [
        pytest.param(RuntimeError("SYNTHETIC_PRIVATE"), "promotion_failed", id="unexpected"),
        pytest.param(BrokerError("queue_full"), "queue_full", id="broker-error"),
    ],
)
async def test_promotion_failure_cancels_the_waiting_job_and_releases(
    tmp_path, failure, deferred_reason
):
    clock = Clock()
    async with running(tmp_path, clock) as broker:

        def fail(*args, **kwargs):
            raise failure

        broker.prepare = fail
        ticket = fake_parent(broker, "key-b", "b-build")
        defer(broker, "key-b", ticket, "b-test")
        broker._store().set_job("b-build", "succeeded", "ok")
        broker.maintenance_tick()
        row = broker.deferred.get("b-test")
        wait_id = row["wait_id"]
        assert (row["state"], row["reason"]) == ("cancelled", deferred_reason)
        assert broker._store().job("b-test") is None
        assert broker.session is None
        # The ownership reason is always promotion_failed, whatever the deferred reason.
        assert request_row(broker, wait_id) == ("released", "promotion_failed")


async def test_promotion_failure_while_busy_keeps_the_job_lease_until_a_later_tick(tmp_path):
    clock = Clock()
    async with running(tmp_path, clock) as broker:

        def fail(*args, **kwargs):
            broker.active_job = "synthetic-busy"
            raise RuntimeError("SYNTHETIC_PRIVATE")

        broker.prepare = fail
        ticket = fake_parent(broker, "key-b", "b-build")
        defer(broker, "key-b", ticket, "b-test")
        broker._store().set_job("b-build", "succeeded", "ok")
        broker.maintenance_tick()
        lease = broker.session
        # _end_ownership refused (busy) and the error was swallowed.
        assert lease is not None and lease.job_id == "b-test"
        assert broker.deferred.get("b-test")["reason"] == "promotion_failed"
        broker.active_job = None
        broker.maintenance_tick()
        assert broker.session is None
        assert request_row(broker, lease.request_id) == ("released", "job_complete")


def _operator_cancel(broker, clock, wait_id):
    broker.operator_cancel_wait(wait_id)
    return ("cancelled", "operator_cancelled_wait")


def _wait_expiry(broker, clock, wait_id):
    clock.now += DEVICE_WAIT_SECONDS
    broker.maintenance_tick()
    return ("expired", "wait_expired")


def _owner_revoked(broker, clock, wait_id):
    broker.principal_valid = lambda principal, owner, scopes: principal != "key-b"
    broker.maintenance_tick()
    return ("cancelled", "principal_not_authorized")


@pytest.mark.parametrize("end_wait", [_operator_cancel, _wait_expiry, _owner_revoked])
async def test_ended_device_wait_cancels_the_waiting_job_with_wait_cancelled(tmp_path, end_wait):
    clock = Clock()
    async with running(tmp_path, clock) as broker:
        await hold_for_a(broker)  # A's reservation keeps B waiting for the device
        ticket = fake_parent(broker, "key-b", "b-build")
        defer(broker, "key-b", ticket, "b-test")
        broker._store().set_job("b-build", "succeeded", "ok")
        broker.maintenance_tick()
        wait_id = broker.deferred.get("b-test")["wait_id"]
        assert wait_id in broker.waiters
        expected_wait = end_wait(broker, clock, wait_id)
        broker.maintenance_tick()
        row = broker.deferred.get("b-test")
        assert (row["state"], row["reason"]) == ("cancelled", "wait_cancelled")
        assert request_row(broker, wait_id) == expected_wait
        assert wait_id not in broker.waiters
        assert broker._store().job("b-test") is None
        broker.principal_valid = lambda principal, owner, scopes: True


# --- release reasons: session_released / work_session_closed / operator_released_idle ----


def _release(broker, work, lease):
    return broker.release(LeaseRequest(session=lease))


def _close(broker, work, lease):
    return broker.close_work_session(
        CloseWorkSessionRequest(resume_token=work["resume_token"]),
        principal_id="key-a",
        scopes=frozenset({"read", "control"}),
    )


def _operator_release(broker, work, lease):
    return broker.operator_release_idle(broker.session.id)


RELEASES = [
    pytest.param(_release, "session_released", id="session_released"),
    pytest.param(_close, "work_session_closed", id="work_session_closed"),
    pytest.param(_operator_release, "operator_released_idle", id="operator_released_idle"),
]


@pytest.mark.parametrize(("action", "reason"), RELEASES)
async def test_release_cancels_queued_work_and_ends_ownership_with_one_reason(
    tmp_path, action, reason
):
    clock = Clock()
    async with running(tmp_path, clock) as broker:
        work = work_session(broker, "key-a")
        lease = request(broker, work, "key-a")["lease"]["session"]
        request_id = broker.session.request_id
        job_id = queue_job(broker, lease)
        action(broker, work, lease)
        job = broker._store().job(job_id)
        assert (job["state"], job["result"]) == ("cancelled", reason)
        assert broker.session is None
        assert request_row(broker, request_id) == ("released", reason)
        assert not broker._execution_intents
        await broker.queue.join()
        assert broker.backend.calls == 0


@pytest.mark.parametrize(("action", "reason"), RELEASES)
async def test_release_is_refused_while_physically_busy(tmp_path, action, reason):
    clock = Clock()
    async with running(tmp_path, clock) as broker:
        work = work_session(broker, "key-a")
        lease = request(broker, work, "key-a")["lease"]["session"]
        owner = broker.session
        job_id = queue_job(broker, lease)
        broker.active_job = "synthetic-busy"
        try:
            with pytest.raises(BrokerError, match="^active_job_cannot_release$"):
                action(broker, work, lease)
            assert broker.session is owner
            assert broker._store().job(job_id)["state"] == "queued"
            assert job_id in broker._execution_intents
        finally:
            broker.active_job = None


# --- _end_ownership and _physical_busy ---------------------------------------------------

ENGINE_ACTIVE = SimpleNamespace(active=True, uncertain=False, request_stop=lambda job: True)
ENGINE_UNCERTAIN = SimpleNamespace(active=False, uncertain=True, request_stop=lambda job: True)

BUSY = [
    ("active_job", [("", "active_job", "synthetic")]),
    ("hardware_connected", [("backend", "hardware_connected", True)]),
    ("backend_active", [("backend", "active", 1)]),
    ("ownership_uncertain", [("backend", "ownership_uncertain", True)]),
    ("unowned_source", [("firmware_source", "active", True)]),
    ("unowned_build", [("firmware_build.process", "active", True)]),
    ("build_process_uncertain", [("firmware_build.process", "uncertain", True)]),
    ("build_output_uncertain", [("firmware_build", "output_uncertain", True)]),
    ("profile_uncertain", [("profile_jobs", "uncertain", True)]),
    ("flash_active", [("firmware_flash", "engine", ENGINE_ACTIVE)]),
    ("flash_uncertain", [("firmware_flash", "engine", ENGINE_UNCERTAIN)]),
]

NOT_BUSY = [
    # Profile-owned source/build activity holds no USB, so it is deliberately not busy.
    (
        "profile_owned_source",
        [("profile_jobs", "active", "profile-job"), ("firmware_source", "active", True)],
    ),
    (
        "profile_owned_build",
        [("profile_jobs", "active", "profile-job"), ("firmware_build.process", "active", True)],
    ),
]


MISSING = object()


def _target(broker, path):
    target = broker
    for part in filter(None, path.split(".")):
        target = getattr(target, part)
    return target


def apply(broker, patches):
    saved = []
    for path, name, value in patches:
        target = _target(broker, path)
        saved.append((target, name, getattr(target, name, MISSING)))
        setattr(target, name, value)
    return saved


def restore(saved):
    for target, name, value in reversed(saved):
        if value is MISSING:
            delattr(target, name)
        else:
            setattr(target, name, value)


@pytest.mark.parametrize(
    ("patches", "busy"),
    [pytest.param(p, True, id=i) for i, p in BUSY]
    + [pytest.param(p, False, id=i) for i, p in NOT_BUSY],
)
async def test_end_ownership_refuses_only_while_physically_busy(tmp_path, patches, busy):
    clock = Clock()
    async with running(tmp_path, clock) as broker:
        work = work_session(broker, "key-a")
        lease = request(broker, work, "key-a")["lease"]["session"]
        owner = broker.session
        job_id = queue_job(broker, lease)
        saved = apply(broker, patches)
        try:
            assert broker._physical_busy() is busy
            if busy:
                with pytest.raises(BrokerError, match="^active_job_cannot_release$"):
                    broker._end_ownership("released", "synthetic_release")
                assert broker.session is owner
                assert job_id in broker._execution_intents
                assert request_row(broker, owner.request_id) == ("owning", None)
            else:
                broker._end_ownership("released", "synthetic_release")
                assert broker.session is None
                # Every pending intent is cancelled with the release reason.
                job = broker._store().job(job_id)
                assert (job["state"], job["result"]) == ("cancelled", "synthetic_release")
                assert request_row(broker, owner.request_id) == ("released", "synthetic_release")
        finally:
            restore(saved)
        await broker.queue.join()


async def test_end_ownership_does_not_touch_the_hold_or_the_queue(tmp_path):
    clock = Clock()
    async with running(tmp_path, clock) as broker:
        work = work_session(broker, "key-a")
        lease = request(broker, work, "key-a")["lease"]["session"]
        job_id = queue_job(broker, lease)
        hold = object()
        broker.hold = hold
        broker._end_ownership("released", "synthetic_release")
        assert broker.hold is hold
        # The asyncio queue still holds the cancelled id; the worker skips it later.
        assert broker.queue.qsize() == 1
        broker.hold = None
        await broker.queue.join()
        assert broker._store().job(job_id)["result"] == "synthetic_release"
        assert broker.backend.calls == 0


async def test_lease_expiry_while_busy_cancels_queued_but_pins_the_owner(tmp_path):
    clock = Clock()
    async with running(tmp_path, clock) as broker:
        work = work_session(broker, "key-a")
        lease = request(broker, work, "key-a")["lease"]["session"]
        owner = broker.session
        job_id = queue_job(broker, lease)
        broker.active_job = "synthetic-busy"
        try:
            clock.now = owner.deadline
            broker.maintenance_tick()
            assert broker.session is owner
            job = broker._store().job(job_id)
            assert (job["state"], job["result"]) == ("cancelled", "lease_expired")
        finally:
            broker.active_job = None
        broker.maintenance_tick()
        assert broker.session is None
        assert request_row(broker, owner.request_id) == ("expired", "lease_expired")
