"""Logical sessions and one bounded physical FIFO, using synthetic state only."""

import asyncio
import json
from contextlib import asynccontextmanager

import pytest
from pydantic import ValidationError

from cardputer_mcp.backend import MockBackend
from cardputer_mcp.broker import Broker, BrokerError
from cardputer_mcp.config import Settings
from cardputer_mcp.models import (
    AcquireRequest,
    CancelDeviceRequest,
    CloseWorkSessionRequest,
    CreateWorkSessionRequest,
    ExecuteRequest,
    LeaseRequest,
    Origin,
    PrepareRequest,
    RenewRequest,
    RequestDeviceRequest,
    ResumeWorkSessionRequest,
)
from cardputer_mcp.store import Store

SCOPES = frozenset({"read", "control"})


class Clock:
    now = 100.0

    def __call__(self):
        return self.now


@asynccontextmanager
async def running(tmp_path, *, backend=None, clock=None, valid=None, **settings):
    broker = Broker(
        Settings(state_dir=tmp_path / "state", allow_mock_execution=True, **settings),
        backend=backend or MockBackend(0.001),
        clock=clock or Clock(),
        lock_path=tmp_path / "device.lockfile",
        principal_valid=valid or (lambda principal, owner, scopes: True),
    )
    await broker.start()
    try:
        yield broker
    finally:
        if getattr(broker.backend, "gate", None):
            broker.backend.gate.set()
        await broker.close()


def create(broker, principal="same-key", *, ttl=3600):
    return broker.create_work_session(
        CreateWorkSessionRequest(owner=Origin.REMOTE, ttl_seconds=ttl),
        principal_id=principal,
        scopes=SCOPES,
    )


def request(broker, work, principal="same-key", **kwargs):
    return broker.request_device(
        RequestDeviceRequest(resume_token=work["resume_token"], **kwargs),
        principal_id=principal,
        scopes=SCOPES,
    )


def resume(broker, work, principal="same-key", scopes=SCOPES):
    return broker.resume_work_session(
        ResumeWorkSessionRequest(resume_token=work["resume_token"]),
        principal_id=principal,
        scopes=scopes,
    )


def release(broker, state):
    return broker.release(LeaseRequest(session=state["lease"]["session"]))


def cancel(broker, work, state, principal="same-key"):
    return broker.cancel_device_request(
        CancelDeviceRequest(
            resume_token=work["resume_token"], request_id=state["request"]["request_id"]
        ),
        principal_id=principal,
        scopes=SCOPES,
    )


def enqueue(broker, state, job_id):
    plan = broker.prepare(PrepareRequest(session=state["lease"]["session"], job_id=job_id))
    broker.execute(ExecuteRequest(session=state["lease"]["session"], plan_hash=plan["plan_hash"]))
    return plan


async def test_same_key_distinct_logical_sessions_fifo_and_reconnect(tmp_path):
    async with running(tmp_path) as broker:
        first, second, third = [create(broker) for _ in range(3)]
        assert len({item["work_session_id"] for item in (first, second, third)}) == 3
        assert len({item["resume_token"] for item in (first, second, third)}) == 3
        one, two, three = [request(broker, item) for item in (first, second, third)]
        assert one["lease"] and one["request"]["state"] == "owning"
        assert two["request"]["position"] == 1
        assert three["request"]["position"] == 2
        repeated = request(broker, second, lease_seconds=200, wait_seconds=1)
        assert repeated["request"] == two["request"]
        assert resume(broker, first)["lease"] == one["lease"]
        with pytest.raises(BrokerError, match="device_busy"):
            broker.acquire(AcquireRequest(owner=Origin.LOCAL))
        release(broker, one)
        assert resume(broker, second)["request"]["state"] == "owning"
        assert resume(broker, third)["request"]["position"] == 1
        release(broker, resume(broker, second))
        assert resume(broker, third)["lease"]


async def test_capabilities_and_principals_are_both_required(tmp_path):
    async with running(tmp_path) as broker:
        first = create(broker, "key-one")
        state = request(broker, first, "key-one")
        with pytest.raises(BrokerError, match="authenticated_owner_mismatch"):
            resume(broker, first, "key-two")
        with pytest.raises(BrokerError, match="invalid_work_session"):
            broker.resume_work_session(
                ResumeWorkSessionRequest(resume_token="0" * 64),
                principal_id="key-one",
                scopes=SCOPES,
            )
        with pytest.raises(ValidationError):
            ResumeWorkSessionRequest(resume_token=first["work_session_id"])
        with pytest.raises(BrokerError, match="invalid_session"):
            broker.prepare(PrepareRequest(session=first["resume_token"], job_id="wrong-secret"))
        with pytest.raises(BrokerError, match="authenticated_owner_mismatch"):
            broker.require_owner(state["lease"]["session"], "remote_mcp", principal_id="key-two")
        readonly = resume(broker, first, "key-one", frozenset({"read"}))
        assert readonly["lease"] is None
        assert readonly["request"]["state"] == "owning"
        with pytest.raises(BrokerError, match="insufficient_scope"):
            broker.request_device(
                RequestDeviceRequest(resume_token=first["resume_token"]),
                principal_id="key-one",
                scopes=frozenset({"read"}),
            )


async def test_wait_cancellation_expiry_and_session_expiry(tmp_path):
    clock = Clock()
    async with running(tmp_path, clock=clock) as broker:
        owner, cancelled, expired, valid = [create(broker) for _ in range(4)]
        own = request(broker, owner)
        waiting = request(broker, cancelled)
        short = request(broker, expired, wait_seconds=1)
        final = request(broker, valid)
        cancel(broker, cancelled, waiting)
        clock.now += 1
        broker.maintenance_tick()
        assert resume(broker, cancelled)["request"]["state"] == "cancelled"
        assert resume(broker, expired)["request"]["state"] == "expired"
        assert resume(broker, valid)["request"]["position"] == 1
        release(broker, own)
        assert resume(broker, valid)["lease"]
        assert resume(broker, cancelled)["lease"] is None
        assert resume(broker, expired)["lease"] is None
        assert broker._store().request(short["request"]["request_id"])["state"] == "expired"
        assert final["request"]["request_id"] == resume(broker, valid)["request"]["request_id"]


async def test_new_grant_invalidates_all_old_physical_capabilities_and_plans(tmp_path):
    async with running(tmp_path) as broker:
        work = create(broker)
        old = request(broker, work)
        plan = broker.prepare(PrepareRequest(session=old["lease"]["session"], job_id="old-plan"))
        release(broker, old)
        new = request(broker, work)
        assert new["lease"]["session"] != old["lease"]["session"]
        assert new["lease"]["lease_id"] != old["lease"]["lease_id"]
        with pytest.raises(BrokerError, match="invalid_session"):
            release(broker, old)
        with pytest.raises(BrokerError, match="invalid_session"):
            broker.renew(RenewRequest(session=old["lease"]["session"]))
        with pytest.raises(BrokerError, match="plan_session_mismatch"):
            broker.execute(
                ExecuteRequest(session=new["lease"]["session"], plan_hash=plan["plan_hash"])
            )
        with pytest.raises(BrokerError, match="only_waiting"):
            cancel(broker, work, old)


async def test_active_expiry_pins_owner_cancels_jobs_and_then_grants_fifo(tmp_path):
    clock = Clock()
    backend = MockBackend(0.001)
    backend.gate = asyncio.Event()
    async with running(tmp_path, backend=backend, clock=clock) as broker:
        owner, next_work = create(broker), create(broker)
        own = request(broker, owner, lease_seconds=1)
        enqueue(broker, own, "active")
        await asyncio.sleep(0)
        assert broker.active_job == "active"
        enqueue(broker, own, "pending")
        waiting = request(broker, next_work)
        clock.now += 2
        broker.maintenance_tick()
        snapshot = broker.operator_snapshot()
        assert snapshot["owner"]["session_id"] == owner["work_session_id"]
        assert snapshot["owner"]["lease_remaining_seconds"] == 0
        assert snapshot["owner"]["release_blocked"]
        assert broker.job("pending")["state"] == "cancelled"
        assert resume(broker, next_work)["request"]["state"] == "waiting"
        with pytest.raises(BrokerError, match="active_job_cannot_release"):
            broker.operator_release_idle(own["lease"]["lease_id"])
        with pytest.raises(BrokerError, match="active_job_cannot_release"):
            broker.close_work_session(
                CloseWorkSessionRequest(resume_token=owner["resume_token"]),
                principal_id="same-key",
                scopes=SCOPES,
            )
        backend.gate.set()
        await broker.queue.join()
        assert backend.max_active == 1 and backend.calls == 1
        assert resume(broker, next_work)["request"]["state"] == "owning"
        assert (
            resume(broker, next_work)["request"]["request_id"] == waiting["request"]["request_id"]
        )


@pytest.mark.parametrize("hold", ["active", "ownership_uncertain"])
async def test_unconfirmed_backend_hold_prevents_handover_even_without_job(tmp_path, hold):
    clock = Clock()
    backend = MockBackend(0.001)
    async with running(tmp_path, backend=backend, clock=clock) as broker:
        owner, next_work = create(broker), create(broker)
        own = request(broker, owner, lease_seconds=1)
        request(broker, next_work)
        setattr(backend, hold, 1)
        try:
            clock.now += 2
            broker.maintenance_tick()
            assert broker.operator_snapshot()["owner"]["lease_id"] == own["lease"]["lease_id"]
            with pytest.raises(BrokerError, match="active_job_cannot_release"):
                broker.operator_release_idle(own["lease"]["lease_id"])
            assert resume(broker, next_work)["lease"] is None
        finally:
            setattr(backend, hold, 0)
        broker.maintenance_tick()
        assert resume(broker, next_work)["lease"]


async def test_operator_wait_only_controls_and_idle_owner_release(tmp_path):
    async with running(tmp_path) as broker:
        owner, second, third = [create(broker) for _ in range(3)]
        one, two, three = [request(broker, work) for work in (owner, second, third)]
        with pytest.raises(BrokerError, match="only_waiting"):
            broker.operator_reorder_wait(
                one["request"]["request_id"],
                1,
                expected_revision=broker.operator_snapshot()["queue_revision"],
            )
        with pytest.raises(BrokerError, match="invalid_wait_position"):
            broker.operator_reorder_wait(
                two["request"]["request_id"],
                0,
                expected_revision=broker.operator_snapshot()["queue_revision"],
            )
        broker.operator_reorder_wait(
            three["request"]["request_id"],
            1,
            expected_revision=broker.operator_snapshot()["queue_revision"],
        )
        assert resume(broker, third)["request"]["position"] == 1
        broker.operator_cancel_wait(two["request"]["request_id"])
        with pytest.raises(BrokerError, match="lease_not_found"):
            broker.operator_release_idle("wrong-public-id")
        broker.operator_release_idle(one["lease"]["lease_id"])
        assert resume(broker, third)["lease"]
        assert resume(broker, second)["request"]["state"] == "cancelled"


async def test_close_waiting_and_idle_sessions_only(tmp_path):
    async with running(tmp_path) as broker:
        owner, waiting = create(broker), create(broker)
        request(broker, owner)
        request(broker, waiting)
        closed = broker.close_work_session(
            CloseWorkSessionRequest(resume_token=waiting["resume_token"]),
            principal_id="same-key",
            scopes=SCOPES,
        )
        assert closed["state"] == "closed"
        assert closed["request"]["state"] == "cancelled"
        broker.close_work_session(
            CloseWorkSessionRequest(resume_token=owner["resume_token"]),
            principal_id="same-key",
            scopes=SCOPES,
        )
        assert broker.session is None
        assert not broker.waiters
        assert resume(broker, owner)["lease"] is None
        with pytest.raises(BrokerError, match="work_session_not_open"):
            request(broker, waiting)


async def test_bounds_and_fixed_logical_ttl(tmp_path):
    clock = Clock()
    async with running(tmp_path, clock=clock, max_work_sessions=2, max_waiters=1) as broker:
        first, second = create(broker, ttl=1), create(broker)
        with pytest.raises(BrokerError, match="work_session_limit"):
            create(broker)
        own = request(broker, first, lease_seconds=300)
        assert own["lease"]["lease_seconds"] == 1
        request(broker, second)
        clock.now += 1
        broker.maintenance_tick()
        assert resume(broker, first)["state"] == "expired"
        assert resume(broker, second)["lease"]
        third = create(broker)
        request(broker, third)
        # Closing the expired logical session never touches the newer owner.
        broker.close_work_session(
            CloseWorkSessionRequest(resume_token=first["resume_token"]),
            principal_id="same-key",
            scopes=SCOPES,
        )
        assert broker.session.work_session_id == second["work_session_id"]


async def test_wait_capacity(tmp_path):
    async with running(tmp_path, max_waiters=1) as broker:
        first, second, third = [create(broker) for _ in range(3)]
        request(broker, first)
        request(broker, second)
        with pytest.raises(BrokerError, match="wait_queue_full"):
            request(broker, third)


async def test_revoked_waiter_and_owner_are_failclosed(tmp_path):
    allowed = {"one", "two", "three"}
    async with running(tmp_path, valid=lambda p, o, s: p in allowed) as broker:
        first, second, third = [create(broker, key) for key in ("one", "two", "three")]
        request(broker, first, "one")
        request(broker, second, "two")
        request(broker, third, "three")
        allowed.remove("two")
        broker.maintenance_tick()
        assert resume(broker, second, "two")["request"]["state"] == "cancelled"
        allowed.remove("one")
        broker.maintenance_tick()
        assert resume(broker, first, "one")["lease"] is None
        assert resume(broker, third, "three")["lease"]


@pytest.mark.parametrize("callback", [None, lambda p, o, s: False])
async def test_missing_or_invalid_principal_callback_never_grants(tmp_path, callback):
    async with running(tmp_path) as broker:
        work = create(broker)
        broker.principal_valid = callback
        with pytest.raises(BrokerError, match="principal_not_authorized"):
            request(broker, work)
        assert broker.session is None
        assert not broker.waiters


async def test_callback_errors_are_not_reflected_or_granted(tmp_path):
    def fail(*args):
        raise RuntimeError("SYNTHETIC_AUTH_SECRET")

    async with running(tmp_path) as broker:
        work = create(broker)
        broker.principal_valid = fail
        with pytest.raises(BrokerError, match="^principal_not_authorized$"):
            request(broker, work)
        assert broker.session is None


async def test_resume_and_physical_secrets_never_persist_or_reach_operator(tmp_path):
    async with running(tmp_path) as broker:
        first, second = create(broker), create(broker)
        own = request(broker, first)
        request(broker, second)
        data = "\n".join(broker._store().db.iterdump()) + json.dumps(broker.operator_snapshot())
        data += json.dumps(broker.status())
        for secret in (first["resume_token"], second["resume_token"], own["lease"]["session"]):
            assert secret not in data
            assert secret not in repr(list(broker.work_sessions.values()))
            assert secret not in repr(broker.session)


async def test_restart_invalidates_secrets_and_marks_ownership_and_waiting_review(tmp_path):
    # Copy SQLite while the synthetic broker runs to model an abrupt process loss.
    async with running(tmp_path / "first") as first:
        owner, waiting = create(first), create(first)
        own, wait = request(first, owner), request(first, waiting)
        restart_dir = tmp_path / "restart"
        restart_dir.mkdir()
        snapshot = Store(restart_dir / "history.sqlite3")
        first._store().db.backup(snapshot.db)
        snapshot.close()
    backend = MockBackend(0.001)
    second = Broker(
        Settings(state_dir=restart_dir),
        backend=backend,
        lock_path=tmp_path / "restart.lockfile",
        principal_valid=lambda *args: True,
    )
    await second.start()
    try:
        assert second.session is None and not second.waiters
        assert second.operator_snapshot()["needs_review"]
        recovery = second._store().recovery_requests()
        assert {item["id"] for item in recovery} == {
            own["request"]["request_id"],
            wait["request"]["request_id"],
        }
        assert all(item["state"] == "needs_review" for item in recovery)
        with pytest.raises(BrokerError, match="invalid_work_session"):
            resume(second, owner)
        with pytest.raises(BrokerError, match="invalid_session"):
            release(second, own)
        with pytest.raises(BrokerError, match="recovery_review_required"):
            second.acquire(AcquireRequest(owner=Origin.LOCAL))
        new = create(second)
        with pytest.raises(BrokerError, match="recovery_review_required"):
            request(second, new)
        assert backend.calls == 0
    finally:
        await second.close()
    offline = Store(restart_dir / "history.sqlite3")
    for item in offline.recovery_requests():
        offline.review_request(item["id"])
    assert not offline.needs_review()
    offline.close()


async def test_clean_shutdown_does_not_leave_ownership_review(tmp_path):
    async with running(tmp_path) as broker:
        request(broker, create(broker))
        request(broker, create(broker))
    store = Store(tmp_path / "state" / "history.sqlite3")
    try:
        store.recover()
        assert not store.needs_review()
        assert not store.recovery_requests()
    finally:
        store.close()


@pytest.mark.parametrize(
    "model,kwargs",
    [
        (CreateWorkSessionRequest, {"owner": "remote_mcp", "ttl_seconds": 86401}),
        (RequestDeviceRequest, {"resume_token": "a" * 64, "lease_seconds": 301}),
        (RequestDeviceRequest, {"resume_token": "a" * 64, "wait_seconds": 3601}),
        (RequestDeviceRequest, {"resume_token": "a" * 64, "principal_id": "takeover"}),
    ],
)
def test_models_bound_wait_lease_and_session_lifetimes(model, kwargs):
    with pytest.raises(ValidationError):
        model(**kwargs)


@pytest.mark.parametrize("transition", ["cancel", "expire", "grant", "enqueue"])
async def test_stale_operator_reorder_never_moves_any_waiter(tmp_path, transition):
    clock = Clock()
    async with running(tmp_path, clock=clock) as broker:
        owner, first, second, third = [create(broker) for _ in range(4)]
        own = request(broker, owner)
        one = request(broker, first, wait_seconds=1)
        two = request(broker, second)
        snapshot = broker.operator_snapshot()
        if transition == "cancel":
            cancel(broker, first, one)
        elif transition == "expire":
            clock.now += 1
        elif transition == "grant":
            release(broker, own)
        else:
            request(broker, third)
        # The expiry is intentionally first noticed inside the mutating API.
        expected_order = [two["request"]["request_id"]]
        if transition == "enqueue":
            expected_order = [
                one["request"]["request_id"],
                two["request"]["request_id"],
                resume(broker, third)["request"]["request_id"],
            ]
        with pytest.raises(BrokerError, match="^stale_operator_snapshot$"):
            broker.operator_reorder_wait(
                two["request"]["request_id"], 1, expected_revision=snapshot["queue_revision"]
            )
        assert [
            row["request_id"] for row in broker.operator_snapshot()["waiters"]
        ] == expected_order


async def test_reorder_revision_detects_aba_and_restart_epoch(tmp_path):
    async with running(tmp_path) as broker:
        first, second, third = [create(broker) for _ in range(3)]
        request(broker, first)
        request(broker, second)
        last = request(broker, third)
        old = broker.operator_snapshot()
        broker.operator_reorder_wait(
            last["request"]["request_id"], 1, expected_revision=old["queue_revision"]
        )
        broker.operator_reorder_wait(
            last["request"]["request_id"],
            2,
            expected_revision=broker.operator_snapshot()["queue_revision"],
        )
        fresh = broker.operator_snapshot()
        assert old["waiters"] == fresh["waiters"]
        assert old["queue_revision"] != fresh["queue_revision"]
        with pytest.raises(BrokerError, match="stale_operator_snapshot"):
            broker.operator_reorder_wait(
                last["request"]["request_id"], 1, expected_revision=old["queue_revision"]
            )
        assert old["waiters"] == broker.operator_snapshot()["waiters"]
    async with running(tmp_path / "new") as other:
        assert other.operator_snapshot()["queue_revision"] != old["queue_revision"]


async def test_periodic_maintenance_advances_without_client_poll(tmp_path):
    clock = Clock()
    async with running(tmp_path, clock=clock) as broker:
        owner, next_work = create(broker), create(broker)
        request(broker, owner, lease_seconds=1)
        request(broker, next_work)
        clock.now += 2
        # Test the actual resident maintenance task; all other TTL tests call tick.
        await asyncio.sleep(0.3)
        assert broker.session.work_session_id == next_work["work_session_id"]
        assert broker.maintenance is not None and not broker.maintenance.done()
    assert broker.maintenance is None


@pytest.mark.parametrize("cause", ["lease", "logical", "revocation"])
async def test_prestart_invalidation_cancels_popped_job_and_hands_over(tmp_path, cause):
    clock = Clock()
    authorized = {"owner", "next"}
    async with running(tmp_path, clock=clock, valid=lambda p, o, s: p in authorized) as broker:
        owner = create(broker, "owner", ttl=1 if cause == "logical" else 3600)
        own = request(broker, owner, "owner", lease_seconds=1 if cause == "lease" else 300)
        next_work = create(broker, "next")
        request(broker, next_work, "next")
        enqueue(broker, own, "never-start")
        if cause == "revocation":
            authorized.remove("owner")
        else:
            clock.now += 2
        # First maintenance must occur after the worker has popped this job.
        await broker.queue.join()
        assert broker.job("never-start")["state"] == "cancelled"
        assert broker.backend.calls == 0
        assert not broker.status()["needs_review"]
        assert resume(broker, next_work, "next")["lease"]


@pytest.mark.parametrize("deadline", ["wait", "work_session"])
async def test_slow_identity_lookup_cannot_grant_an_expired_wait(tmp_path, deadline):
    clock = Clock()
    backend = MockBackend(0.001)
    async with running(tmp_path, backend=backend, clock=clock) as broker:
        work = create(broker, ttl=1 if deadline == "work_session" else 3600)
        backend.active = 1
        try:
            state = request(broker, work, wait_seconds=1 if deadline == "wait" else 300)
        finally:
            backend.active = 0
        assert state["request"]["state"] == "waiting"

        def delayed_metadata_lookup(principal, owner, scopes):
            clock.now = 102.0
            return True

        broker.principal_valid = delayed_metadata_lookup
        broker.maintenance_tick()
        assert broker.session is None
        stored = broker._store().request(state["request"]["request_id"])
        assert stored["state"] == "expired"
        assert stored["lease_id"] is None


async def test_operator_session_view_keeps_all_open_plus_twenty_recent_terminal(tmp_path):
    async with running(tmp_path) as broker:
        closed = []
        for _ in range(25):
            work = create(broker)
            closed.append(work["work_session_id"])
            broker.close_work_session(
                CloseWorkSessionRequest(resume_token=work["resume_token"]),
                principal_id="same-key",
                scopes=SCOPES,
            )
        active = [create(broker), create(broker)]
        snapshot = broker.operator_snapshot()
        ids = {row["session_id"] for row in snapshot["sessions"]}
        assert len(ids) == 22
        assert all(work["work_session_id"] in ids for work in active)
        assert set(closed[-20:]) <= ids
        assert not set(closed[:5]) & ids
        assert snapshot["session_history_omitted"] == 5
        assert broker._store().record_count("work_sessions") == 27


async def test_operator_bounded_view_retains_expired_active_owner(tmp_path):
    clock = Clock()
    backend = MockBackend(0.001)
    backend.gate = asyncio.Event()
    async with running(tmp_path, clock=clock, backend=backend) as broker:
        owner = create(broker, ttl=1)
        own = request(broker, owner)
        enqueue(broker, own, "pinned-owner")
        await asyncio.sleep(0)
        assert broker.active_job == "pinned-owner"
        for _ in range(25):
            work = create(broker)
            broker.close_work_session(
                CloseWorkSessionRequest(resume_token=work["resume_token"]),
                principal_id="same-key",
                scopes=SCOPES,
            )
        open_work = create(broker)
        clock.now += 2
        snapshot = broker.operator_snapshot()
        visible = {row["session_id"]: row for row in snapshot["sessions"]}
        assert len(visible) == 22
        assert visible[owner["work_session_id"]]["state"] == "expired"
        assert visible[open_work["work_session_id"]]["state"] == "open"
        assert snapshot["owner"]["session_id"] == owner["work_session_id"]
        assert snapshot["owner"]["release_blocked"]
        assert snapshot["session_history_omitted"] == 5
        assert broker._store().record_count("work_sessions") == 27
