"""Independent logical-session handover regressions using synthetic devices only."""

import asyncio
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from cardputer_mcp.backend import DeviceOperationFailure, MockBackend
from cardputer_mcp.broker import Broker, BrokerError
from cardputer_mcp.config import Settings
from cardputer_mcp.models import (
    AcquireRequest,
    CancelDeviceRequest,
    CloseWorkSessionRequest,
    CreateWorkSessionRequest,
    ExecuteRequest,
    LeaseRequest,
    LogRequest,
    Origin,
    PrepareRequest,
    RenewRequest,
    RequestDeviceRequest,
    ResumeWorkSessionRequest,
)
from cardputer_mcp.models import TestName as Workflow
from cardputer_mcp.store import Store


class FakeClock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


class GatedBackend(MockBackend):
    """The test, rather than elapsed wall time, determines workflow completion."""

    def __init__(self) -> None:
        super().__init__(0.001)
        self.started = asyncio.Event()
        self.gate = asyncio.Event()

    async def run(self, test: Workflow, stage: Callable[[str], None]) -> None:
        self.started.set()
        await super().run(test, stage)


class UncertainCloseBackend(MockBackend):
    """A deterministic failure retaining the synthetic physical-device handle."""

    ownership_uncertain = False

    async def execute(self, plan: dict[str, Any], stage: Callable[[str], None]) -> str:
        self.calls += 1
        self.active = 1
        self.max_active = 1
        self.ownership_uncertain = True
        raise DeviceOperationFailure("serial_close_unconfirmed", step="close_serial")


def test_restart_quarantines_every_waiting_or_owning_request(tmp_path: Path) -> None:
    path = tmp_path / "history.sqlite3"
    store = Store(path)
    try:
        store.put_work_session(
            "logical_one", "a" * 32, "remote_mcp", "key_one", frozenset({"read", "control"})
        )
        store.put_work_session(
            "logical_two", "a" * 32, "remote_mcp", "key_two", frozenset({"read", "control"})
        )
        store.put_request("owning", "logical_one", 30, state="owning", lease_id="b" * 32)
        store.put_request("waiting", "logical_two", 30)
        store.put_request("cancelled", "logical_two", 30, state="cancelled")
    finally:
        store.close()

    recovered = Store(path)
    try:
        recovered.recover()
        assert recovered.needs_review()
        assert {row["id"] for row in recovered.recovery_requests()} == {"owning", "waiting"}
        for request_id in ("owning", "waiting"):
            row = recovered.request(request_id)
            assert row is not None
            assert row["state"] == "needs_review"
            assert row["reason"] == "broker_restart_no_replay"
        cancelled = recovered.request("cancelled")
        assert cancelled is not None
        assert cancelled["state"] == "cancelled"
        assert all(row["state"] == "needs_review" for row in recovered.session_history())
        recovered.review_request("owning")
        assert recovered.needs_review()
        recovered.review_request("waiting")
        assert not recovered.needs_review()
    finally:
        recovered.close()


def identity(principal_id: str = "shared_key") -> dict[str, Any]:
    return {"principal_id": principal_id, "scopes": frozenset({"read", "control"})}


def create_session(
    broker: Broker, principal_id: str = "shared_key", *, ttl_seconds: int = 3600
) -> dict[str, Any]:
    return broker.create_work_session(
        CreateWorkSessionRequest(owner=Origin.REMOTE, ttl_seconds=ttl_seconds),
        **identity(principal_id),
    )


def ask_device(
    broker: Broker,
    logical: dict[str, Any],
    principal_id: str = "shared_key",
    *,
    lease_seconds: int = 30,
    wait_seconds: int = 300,
) -> dict[str, Any]:
    return broker.request_device(
        RequestDeviceRequest(
            resume_token=logical["resume_token"],
            lease_seconds=lease_seconds,
            wait_seconds=wait_seconds,
        ),
        **identity(principal_id),
    )


def resume(
    broker: Broker, logical: dict[str, Any], principal_id: str = "shared_key"
) -> dict[str, Any]:
    return broker.resume_work_session(
        ResumeWorkSessionRequest(resume_token=logical["resume_token"]),
        **identity(principal_id),
    )


def prepare_job(broker: Broker, lease: dict[str, Any], job_id: str) -> ExecuteRequest:
    prepared = broker.prepare(
        PrepareRequest(
            session=lease["session"],
            job_id=job_id,
            test=Workflow.WORKFLOW,
        )
    )
    return ExecuteRequest(session=lease["session"], plan_hash=prepared["plan_hash"])


@pytest.fixture
async def session_broker(tmp_path: Path):
    clock = FakeClock()
    broker = Broker(
        Settings(state_dir=tmp_path / "state", allow_mock_execution=True),
        clock=clock,
        lock_path=tmp_path / "device.lockfile",
        principal_valid=lambda principal_id, origin, scopes: True,
    )
    await broker.start()
    try:
        yield broker, clock
    finally:
        await broker.close()


async def test_same_key_has_distinct_logical_sessions_and_fifo_leases(session_broker) -> None:
    broker, _ = session_broker
    first = create_session(broker)
    second = create_session(broker)
    third = create_session(broker)
    assert len({first["work_session_id"], second["work_session_id"], third["work_session_id"]}) == 3
    assert len({first["resume_token"], second["resume_token"], third["resume_token"]}) == 3
    owner = ask_device(broker, first)
    waiting_second = ask_device(broker, second)
    waiting_third = ask_device(broker, third)
    assert owner["lease"] is not None
    assert owner["lease"]["session"] != first["resume_token"]
    assert waiting_second["lease"] is None
    assert waiting_third["lease"] is None
    assert waiting_second["request"]["position"] == 1
    assert waiting_third["request"]["position"] == 2
    # Reconnect/poll never merges sessions sharing the same authentication key.
    assert resume(broker, second)["lease"] is None
    broker.release(LeaseRequest(session=owner["lease"]["session"]))
    with pytest.raises(BrokerError):
        broker.acquire(AcquireRequest(owner=Origin.LOCAL))
    broker.maintenance_tick()
    second_owner = resume(broker, second)
    assert second_owner["lease"] is not None
    assert resume(broker, third)["lease"] is None
    broker.release(LeaseRequest(session=second_owner["lease"]["session"]))
    assert resume(broker, third)["lease"] is not None


async def test_new_grant_invalidates_old_release_renew_and_plan(session_broker) -> None:
    broker, _ = session_broker
    logical = create_session(broker)
    old_lease = ask_device(broker, logical)["lease"]
    old_job = prepare_job(broker, old_lease, "old-plan")
    broker.release(LeaseRequest(session=old_lease["session"]))
    new_lease = ask_device(broker, logical)["lease"]
    assert new_lease["session"] != old_lease["session"]
    assert new_lease["lease_id"] != old_lease["lease_id"]
    for action in (
        lambda: broker.release(LeaseRequest(session=old_lease["session"])),
        lambda: broker.renew(RenewRequest(session=old_lease["session"])),
        lambda: broker.execute(old_job),
        lambda: broker.execute(
            ExecuteRequest(session=new_lease["session"], plan_hash=old_job.plan_hash)
        ),
        lambda: broker.release(LeaseRequest(session=logical["resume_token"])),
    ):
        with pytest.raises(BrokerError):
            action()
    assert resume(broker, logical)["lease"]["session"] == new_lease["session"]
    assert broker.backend.calls == 0
    assert broker.queue.empty()


async def test_resume_token_is_bound_to_principal_and_snapshot_never_discloses_it(
    session_broker,
) -> None:
    broker, _ = session_broker
    logical = create_session(broker, "key_a")
    lease = ask_device(broker, logical, "key_a")["lease"]
    with pytest.raises(BrokerError):
        resume(broker, logical, "key_b")
    with pytest.raises(BrokerError):
        ask_device(broker, logical, "key_b")
    assert resume(broker, logical, "key_a")["lease"]["session"] == lease["session"]
    exposed = json.dumps(
        [
            broker.status(),
            broker.operator_snapshot(),
            broker.read_logs(LogRequest()),
            broker._store().session_history(),
            broker._store().recovery_requests(),
        ]
    ) + "\n".join(broker._store().db.iterdump())
    for secret in (logical["resume_token"], lease["session"]):
        assert secret not in exposed
        assert secret.encode() not in (broker.settings.state_dir / "history.sqlite3").read_bytes()


async def test_cancelled_wait_cannot_reappear_or_cancel_new_request(session_broker) -> None:
    broker, _ = session_broker
    first, second, third = [create_session(broker) for _ in range(3)]
    owner = ask_device(broker, first)["lease"]
    cancelled = ask_device(broker, second)["request"]["request_id"]
    ask_device(broker, third)
    broker.cancel_device_request(
        CancelDeviceRequest(resume_token=second["resume_token"], request_id=cancelled),
        **identity(),
    )
    replacement = ask_device(broker, second)["request"]["request_id"]
    assert replacement != cancelled
    try:
        broker.cancel_device_request(
            CancelDeviceRequest(resume_token=second["resume_token"], request_id=cancelled),
            **identity(),
        )
    except BrokerError:
        pass
    broker.release(LeaseRequest(session=owner["session"]))
    assert resume(broker, third)["lease"] is not None
    assert resume(broker, second)["lease"] is None
    assert resume(broker, second)["request"]["request_id"] == replacement
    assert broker._store().request(cancelled)["state"] == "cancelled"


async def test_revoked_first_waiter_is_skipped_without_blocking_valid_successor(
    session_broker,
) -> None:
    broker, _ = session_broker
    valid = {"owner", "revoked", "next"}
    broker.principal_valid = lambda principal_id, origin, scopes: principal_id in valid
    first = create_session(broker, "owner")
    second = create_session(broker, "revoked")
    third = create_session(broker, "next")
    owner = ask_device(broker, first, "owner")["lease"]
    revoked_request = ask_device(broker, second, "revoked")["request"]["request_id"]
    ask_device(broker, third, "next")
    valid.remove("revoked")
    broker.release(LeaseRequest(session=owner["session"]))
    broker.maintenance_tick()
    assert resume(broker, third, "next")["lease"] is not None
    assert broker._store().request(revoked_request)["state"] != "owning"
    assert resume(broker, second, "revoked")["lease"] is None
    with pytest.raises(BrokerError):
        ask_device(broker, second, "revoked")


@pytest.mark.parametrize("validator", [None])
async def test_device_grant_fails_closed_without_current_auth(tmp_path: Path, validator) -> None:
    broker = Broker(
        Settings(state_dir=tmp_path / "state"),
        lock_path=tmp_path / "device.lockfile",
        principal_valid=validator,
    )
    await broker.start()
    try:
        logical = create_session(broker)
        with pytest.raises(BrokerError):
            ask_device(broker, logical)
        assert resume(broker, logical)["lease"] is None
        assert broker.session is None
        assert broker.backend.calls == 0
    finally:
        await broker.close()


async def test_auth_validator_failure_does_not_grant_or_leak_exception(session_broker) -> None:
    broker, _ = session_broker
    first, second = create_session(broker), create_session(broker, "failing")
    owner = ask_device(broker, first)["lease"]
    ask_device(broker, second, "failing")

    def broken(principal_id: str, origin: str, scopes: frozenset[str]) -> bool:
        if principal_id == "failing":
            raise RuntimeError("SYNTHETIC_AUTH_SECRET")
        return True

    broker.principal_valid = broken
    broker.release(LeaseRequest(session=owner["session"]))
    broker.maintenance_tick()
    assert broker.session is None
    exposed = json.dumps(
        [broker.status(), broker.operator_snapshot(), broker.read_logs(LogRequest())]
    )
    exposed += "\n".join(broker._store().db.iterdump())
    assert "SYNTHETIC_AUTH_SECRET" not in exposed


@pytest.mark.parametrize("expiry", ["session_ttl", "lease_ttl", "revocation"])
async def test_invalidation_pins_complete_async_workflow_and_cancels_pending(
    tmp_path: Path, expiry: str
) -> None:
    clock = FakeClock()
    backend = GatedBackend()
    valid = {"first", "second"}
    broker = Broker(
        Settings(state_dir=tmp_path / "state", allow_mock_execution=True),
        clock=clock,
        backend=backend,
        lock_path=tmp_path / "device.lockfile",
        principal_valid=lambda principal_id, origin, scopes: principal_id in valid,
    )
    await broker.start()
    try:
        first = create_session(broker, "first", ttl_seconds=1 if expiry == "session_ttl" else 3600)
        second = create_session(broker, "second")
        lease = ask_device(
            broker, first, "first", lease_seconds=1 if expiry == "lease_ttl" else 30
        )["lease"]
        broker.execute(prepare_job(broker, lease, "active"))
        await asyncio.wait_for(backend.started.wait(), timeout=1)
        broker.execute(prepare_job(broker, lease, "pending"))
        ask_device(broker, second, "second")
        if expiry in {"session_ttl", "lease_ttl"}:
            clock.now += 2
        else:
            valid.remove("first")
        broker.maintenance_tick()
        assert broker.active_job == "active"
        assert backend.active == 1
        assert resume(broker, second, "second")["lease"] is None
        assert broker.job("pending")["state"] == "cancelled"
        with pytest.raises(BrokerError):
            broker.acquire(AcquireRequest(owner=Origin.LOCAL))
        with pytest.raises(BrokerError):
            broker.renew(RenewRequest(session=lease["session"]))
        backend.gate.set()
        await asyncio.wait_for(broker.queue.join(), timeout=1)
        broker.maintenance_tick()
        assert broker.active_job is None
        assert backend.calls == 1
        assert backend.max_active == 1
        assert broker.job("active")["state"] == "succeeded"
        assert resume(broker, second, "second")["lease"] is not None
    finally:
        backend.gate.set()
        await broker.close()


async def test_uncertain_backend_close_never_hands_device_to_waiter(tmp_path: Path) -> None:
    clock = FakeClock()
    backend = UncertainCloseBackend(0.001)
    broker = Broker(
        Settings(state_dir=tmp_path / "state", allow_mock_execution=True),
        clock=clock,
        backend=backend,
        lock_path=tmp_path / "device.lockfile",
        principal_valid=lambda principal_id, origin, scopes: True,
    )
    await broker.start()
    try:
        first, second = create_session(broker), create_session(broker)
        lease = ask_device(broker, first, lease_seconds=1)["lease"]
        ask_device(broker, second)
        broker.execute(prepare_job(broker, lease, "uncertain"))
        await asyncio.wait_for(broker.queue.join(), timeout=1)
        clock.now += 2
        broker.maintenance_tick()
        assert broker.job("uncertain")["state"] == "needs_review"
        assert backend.active == 1
        assert broker.active_job == "uncertain"
        assert resume(broker, second)["lease"] is None
        with pytest.raises(BrokerError):
            broker.acquire(AcquireRequest(owner=Origin.LOCAL))
        with pytest.raises(BrokerError):
            broker.operator_release_idle(lease["lease_id"])
        with pytest.raises(BrokerError, match="device_close_unconfirmed"):
            await broker.close()
        assert broker.store is not None
        assert broker._device_lock.is_locked
        assert broker._state_lock.is_locked
        assert backend.calls == 1
    finally:
        # Synthetic teardown only: no real handle was ever opened by this test.
        backend.active = 0
        backend.ownership_uncertain = False
        broker.active_job = None
        await broker.close()


@pytest.mark.parametrize("busy_state", ["backend_active", "ownership_uncertain"])
async def test_scheduler_checks_backend_even_without_active_job(
    session_broker, busy_state: str
) -> None:
    broker, clock = session_broker
    first, second = create_session(broker), create_session(broker)
    lease = ask_device(broker, first, lease_seconds=1)["lease"]
    ask_device(broker, second)
    if busy_state == "backend_active":
        broker.backend.active = 1
    else:
        broker.backend.ownership_uncertain = True
    try:
        assert broker.active_job is None
        clock.now += 2
        broker.maintenance_tick()
        assert resume(broker, second)["lease"] is None
        with pytest.raises(BrokerError):
            broker.operator_release_idle(lease["lease_id"])
        with pytest.raises(BrokerError):
            broker.acquire(AcquireRequest(owner=Origin.LOCAL))
    finally:
        broker.backend.active = 0
        broker.backend.ownership_uncertain = False


async def test_waiter_and_live_session_limits_are_enforced(tmp_path: Path) -> None:
    broker = Broker(
        Settings(state_dir=tmp_path / "state", max_work_sessions=3, max_waiters=1),
        lock_path=tmp_path / "device.lockfile",
        principal_valid=lambda principal_id, origin, scopes: True,
    )
    await broker.start()
    try:
        first, second, third = [create_session(broker) for _ in range(3)]
        with pytest.raises(BrokerError):
            create_session(broker)
        ask_device(broker, first)
        ask_device(broker, second)
        with pytest.raises(BrokerError):
            ask_device(broker, third)
        # Repeated polls and requests must not multiply records or queue entries.
        request_id = resume(broker, second)["request"]["request_id"]
        for _ in range(8):
            try:
                result = ask_device(broker, second)
                assert result["request"]["request_id"] == request_id
            except BrokerError:
                pass
        assert broker._store().record_count("work_sessions") == 3
        assert broker._store().record_count("device_requests") == 2
        assert len(broker.operator_snapshot()["waiters"]) == 1
    finally:
        await broker.close()


async def test_late_wait_cancel_cannot_release_promoted_lease(session_broker) -> None:
    broker, clock = session_broker
    first, second, third = [create_session(broker) for _ in range(3)]
    ask_device(broker, first, lease_seconds=1)
    request_id = ask_device(broker, second)["request"]["request_id"]
    ask_device(broker, third)
    clock.now += 2
    # Cancellation observes expiry and grant in the same synchronous transition.
    with pytest.raises(BrokerError, match="only_waiting_requests_can_be_cancelled"):
        broker.cancel_device_request(
            CancelDeviceRequest(resume_token=second["resume_token"], request_id=request_id),
            **identity(),
        )
    second_lease = resume(broker, second)["lease"]
    assert second_lease is not None
    assert resume(broker, third)["lease"] is None
    # Repeating the stale action cannot release or change this grant.
    with pytest.raises(BrokerError):
        broker.operator_cancel_wait(request_id)
    assert resume(broker, second)["lease"] == second_lease


async def test_active_session_close_is_rejected_without_interrupting_worker(tmp_path: Path) -> None:
    backend = GatedBackend()
    broker = Broker(
        Settings(state_dir=tmp_path / "state", allow_mock_execution=True),
        backend=backend,
        lock_path=tmp_path / "device.lockfile",
        principal_valid=lambda principal_id, origin, scopes: True,
    )
    await broker.start()
    try:
        first, second = create_session(broker), create_session(broker)
        owner = ask_device(broker, first)["lease"]
        ask_device(broker, second)
        broker.execute(prepare_job(broker, owner, "close-active"))
        await asyncio.wait_for(backend.started.wait(), timeout=1)
        with pytest.raises(BrokerError, match="active_job_cannot_release"):
            broker.close_work_session(
                CloseWorkSessionRequest(resume_token=first["resume_token"]), **identity()
            )
        assert broker.active_job == "close-active"
        assert backend.active == 1
        assert resume(broker, first)["lease"]["session"] == owner["session"]
        assert resume(broker, second)["lease"] is None
        backend.gate.set()
        await asyncio.wait_for(broker.queue.join(), timeout=1)
        broker.close_work_session(
            CloseWorkSessionRequest(resume_token=first["resume_token"]), **identity()
        )
        assert resume(broker, first)["lease"] is None
        assert resume(broker, second)["lease"] is not None
        assert backend.calls == 1
    finally:
        backend.gate.set()
        await broker.close()


@pytest.mark.parametrize("auth_change", ["key_expiry", "lost_control", "lost_read"])
async def test_current_key_expiry_and_scopes_are_rechecked_before_grant(
    session_broker, auth_change: str
) -> None:
    broker, clock = session_broker
    granted_scopes = {"read", "control"}
    key_deadline = clock.now + 1 if auth_change == "key_expiry" else clock.now + 100

    def live(principal_id: str, origin: str, required: frozenset[str]) -> bool:
        if principal_id != "changing_key":
            return True
        return clock.now < key_deadline and required <= granted_scopes

    broker.principal_valid = live
    owner = create_session(broker, "owner_key")
    affected = create_session(broker, "changing_key")
    successor = create_session(broker, "next_key")
    lease = ask_device(broker, owner, "owner_key")["lease"]
    affected_request = ask_device(broker, affected, "changing_key")["request"]["request_id"]
    ask_device(broker, successor, "next_key")
    if auth_change == "key_expiry":
        clock.now += 2
    elif auth_change == "lost_control":
        granted_scopes.remove("control")
    else:
        granted_scopes.remove("read")
    broker.release(LeaseRequest(session=lease["session"]))
    broker.maintenance_tick()
    assert broker._store().request(affected_request)["state"] == "cancelled"
    assert resume(broker, affected, "changing_key")["lease"] is None
    assert resume(broker, successor, "next_key")["lease"] is not None
    with pytest.raises(BrokerError):
        ask_device(broker, affected, "changing_key")
    assert broker.backend.calls == 0


async def test_read_scope_resume_never_returns_physical_control_secret(session_broker) -> None:
    broker, _ = session_broker
    logical = create_session(broker)
    lease = ask_device(broker, logical)["lease"]
    read_view = broker.resume_work_session(
        ResumeWorkSessionRequest(resume_token=logical["resume_token"]),
        principal_id="shared_key",
        scopes=frozenset({"read"}),
    )
    assert read_view["lease"] is None
    assert lease["session"] not in json.dumps(read_view)
    with pytest.raises(BrokerError):
        broker.request_device(
            RequestDeviceRequest(resume_token=logical["resume_token"]),
            principal_id="shared_key",
            scopes=frozenset({"read"}),
        )


@pytest.mark.parametrize("invalidation", ["revocation"])
async def test_invalidation_before_worker_starts_cancels_without_replay(
    session_broker, invalidation: str
) -> None:
    broker, clock = session_broker
    valid = {"first", "next"}
    broker.principal_valid = lambda principal_id, origin, scopes: principal_id in valid
    first = create_session(
        broker, "first", ttl_seconds=1 if invalidation == "session_expiry" else 3600
    )
    successor = create_session(broker, "next")
    lease = ask_device(
        broker, first, "first", lease_seconds=1 if invalidation == "lease_expiry" else 30
    )["lease"]
    ask_device(broker, successor, "next")
    request = prepare_job(broker, lease, "never-start")
    broker.execute(request)
    if invalidation == "revocation":
        valid.remove("first")
    else:
        clock.now += 2
    # No event-loop yield has occurred since enqueue: the worker has not started.
    await asyncio.wait_for(broker.queue.join(), timeout=1)
    assert broker.job("never-start")["state"] == "cancelled"
    assert broker.backend.calls == 0
    assert resume(broker, successor, "next")["lease"] is not None
    with pytest.raises(BrokerError):
        broker.execute(request)
    await asyncio.sleep(0)
    assert broker.backend.calls == 0


async def test_restart_has_no_resume_no_grant_and_no_automatic_replay(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    store = Store(state / "history.sqlite3")
    try:
        store.put_work_session(
            "old_work", "a" * 32, "remote_mcp", "synthetic_key", frozenset({"read", "control"})
        )
        store.put_request("old_owning", "old_work", 30, state="owning", lease_id="b" * 32)
        store.put_request("old_waiting", "old_work", 30)
        store.add_job("old_running", "c" * 64)
        store.set_job("old_running", "running")
        store.add_job("old_queued", "d" * 64)
    finally:
        store.close()
    backend = MockBackend(0.001)
    broker = Broker(
        Settings(state_dir=state, allow_mock_execution=True),
        backend=backend,
        lock_path=tmp_path / "device.lockfile",
        principal_valid=lambda principal_id, origin, scopes: True,
    )
    await broker.start()
    try:
        assert broker.status()["needs_review"]
        assert broker.operator_snapshot()["owner"] is None
        assert broker.operator_snapshot()["waiters"] == []
        assert broker.work_sessions == {}
        assert broker.device_requests == {}
        assert broker.job("old_running")["state"] == "needs_review"
        assert broker.job("old_queued")["state"] == "cancelled"
        logical = create_session(broker)
        ask_device(broker, logical)
        with pytest.raises(BrokerError, match="device_busy"):
            broker.acquire(AcquireRequest(owner=Origin.LOCAL))
        with pytest.raises(BrokerError, match="invalid_work_session"):
            broker.resume_work_session(
                ResumeWorkSessionRequest(resume_token="e" * 64), **identity("synthetic_key")
            )
        broker.maintenance_tick()
        await asyncio.sleep(0)
        assert backend.calls == 0
        assert broker.queue.empty()
    finally:
        await broker.close()


@pytest.mark.parametrize("table", ["device_requests"])
async def test_history_limit_rejects_before_allocating_live_state(
    session_broker, monkeypatch: pytest.MonkeyPatch, table: str
) -> None:
    broker, _ = session_broker
    logical = create_session(broker)
    original_count = broker._store().record_count
    monkeypatch.setattr(
        broker._store(),
        "record_count",
        lambda target: 10000 if target == table else original_count(target),
    )
    with pytest.raises(BrokerError, match="history_full_archive_offline"):
        if table == "work_sessions":
            create_session(broker)
        else:
            ask_device(broker, logical)
    assert len(broker.work_sessions) == 1
    assert broker.device_requests == {}
    assert broker.waiters == []
    assert broker.session is None


async def test_stale_operator_reorder_snapshot_cannot_change_fifo(session_broker) -> None:
    broker, _ = session_broker
    first, second, third, fourth = [create_session(broker) for _ in range(4)]
    ask_device(broker, first)
    second_request = ask_device(broker, second)["request"]["request_id"]
    third_request = ask_device(broker, third)["request"]["request_id"]
    observed = broker.operator_snapshot()
    fourth_request = ask_device(broker, fourth)["request"]["request_id"]
    with pytest.raises(BrokerError, match="stale_operator_snapshot"):
        broker.operator_reorder_wait(third_request, 1, expected_revision=observed["queue_revision"])
    current = broker.operator_snapshot()
    assert [row["request_id"] for row in current["waiters"]] == [
        second_request,
        third_request,
        fourth_request,
    ]
    assert current["queue_revision"] != observed["queue_revision"]
    broker.operator_reorder_wait(third_request, 1, expected_revision=current["queue_revision"])
    reordered = broker.operator_snapshot()
    assert [row["request_id"] for row in reordered["waiters"]] == [
        third_request,
        second_request,
        fourth_request,
    ]
    assert reordered["queue_revision"] != current["queue_revision"]
    # A delayed duplicate button click must not apply a second positional edit.
    with pytest.raises(BrokerError, match="stale_operator_snapshot"):
        broker.operator_reorder_wait(second_request, 3, expected_revision=current["queue_revision"])
    assert broker.operator_snapshot()["waiters"] == reordered["waiters"]
