"""Submitted intents execute only after exact local review; synthetic backends only."""

import asyncio
import json
import sqlite3
from contextlib import asynccontextmanager

import pytest

from cardputer_mcp.backend import DeviceOperationFailure, MockBackend
from cardputer_mcp.broker import Broker, BrokerError, LocalHomeDiagnostic
from cardputer_mcp.config import Settings
from cardputer_mcp.models import (
    AcquireRequest,
    CancelRequest,
    CloseWorkSessionRequest,
    CreateWorkSessionRequest,
    ExecuteRequest,
    LeaseRequest,
    LogRequest,
    Origin,
    PrepareRequest,
    RenewRequest,
    RequestDeviceRequest,
)
from cardputer_mcp.models import TestName as Action
from cardputer_mcp.plan_approval import PlanApprovals
from cardputer_mcp.store import Store


class Clock:
    now = 100.0

    def __call__(self):
        return self.now


@asynccontextmanager
async def running(tmp_path, *, max_queue=8, work_session=False, backend=None):
    clock = Clock()
    broker = Broker(
        Settings(state_dir=tmp_path / "state", max_queue=max_queue, mock_stage_seconds=0.001),
        clock=clock,
        backend=backend,
        lock_path=tmp_path / "device.lockfile",
        principal_valid=lambda principal, owner, scopes: True,
    )
    await broker.start()
    if work_session:
        work = broker.create_work_session(
            CreateWorkSessionRequest(owner=Origin.REMOTE),
            principal_id="synthetic-client",
            scopes=frozenset({"read", "control"}),
        )
        broker.request_device(
            RequestDeviceRequest(resume_token=work["resume_token"]),
            principal_id="synthetic-client",
            scopes=frozenset({"read", "control"}),
        )
    else:
        broker.acquire(AcquireRequest(owner=Origin.REMOTE), principal_id="synthetic-client")
    try:
        yield broker, PlanApprovals(broker), clock
    finally:
        broker.principal_valid = lambda *args: True
        broker.closing = False
        broker.active_job = None
        broker.backend.active = 0
        broker.backend.hardware_connected = False
        if hasattr(broker.backend, "ownership_uncertain"):
            broker.backend.ownership_uncertain = False
        if broker.backend.gate is not None:
            broker.backend.gate.set()
        await broker.close()


def prepare(broker, job="submitted", test=Action.DIAGNOSTICS):
    return broker.prepare(PrepareRequest(session=broker.session.capability, job_id=job, test=test))[
        "plan_hash"
    ]


def submit(broker, digest, *, waiting=True):
    return broker.execute(
        ExecuteRequest(
            session=broker.session.capability, plan_hash=digest, wait_for_approval=waiting
        )
    )


def approve(service, digest):
    return service.approve(digest, service.detail(digest)["review_revision"], "")


def approval_count(broker):
    return broker._store().db.execute("SELECT count(*) FROM approvals").fetchone()[0]


async def test_preparation_and_legacy_execute_never_submit_work(tmp_path):
    async with running(tmp_path) as (broker, service, _):
        digest = prepare(broker)
        detail = service.detail(digest)
        assert detail["execution_submitted"] is False
        assert detail["intent_remaining_seconds"] is None
        with pytest.raises(BrokerError, match="^operator_approval_required$"):
            submit(broker, digest, waiting=False)
        assert broker._store().record_count("jobs") == 0
        assert approve(service, digest)["execution_submitted"] is False
        assert broker._store().record_count("jobs") == 0
        assert broker.queue.empty() and broker.backend.calls == 0
        assert submit(broker, digest, waiting=False)["state"] == "queued"
        await broker.queue.join()
        assert broker.backend.calls == 1


async def test_submitted_exact_review_dispatches_once_without_client_poll(tmp_path):
    async with running(tmp_path) as (broker, service, _):
        digest = prepare(broker)
        prepared_review = service.detail(digest)["review_revision"]
        result = submit(broker, digest)
        assert result["state"] == "awaiting_approval" and not result["deduplicated"]
        assert result["finished"] is None and broker.queue.empty()
        assert submit(broker, digest)["deduplicated"]
        assert submit(broker, digest, waiting=False)["state"] == "awaiting_approval"
        assert broker.backend.calls == 0 and approval_count(broker) == 0
        with pytest.raises(BrokerError, match="^stale_plan_review$"):
            service.approve(digest, prepared_review, "")
        detail = service.detail(digest)
        assert detail["execution_submitted"] is True
        assert detail["intent_remaining_seconds"] == 30
        assert detail["review_revision"] != prepared_review
        assert service.list_pending()["plans"][0]["execution_submitted"] is True
        assert service.approve(digest, detail["review_revision"], "") == {
            "approved": True,
            "already_approved": False,
            "plan_hash": digest,
            "execution_submitted": True,
            "job_state": "queued",
        }
        assert broker.queue.qsize() == 1 and broker.backend.calls == 0
        assert approval_count(broker) == 0
        assert submit(broker, digest)["deduplicated"]
        with pytest.raises(BrokerError):
            service.approve(digest, detail["review_revision"], "")
        await broker.queue.join()
        assert broker.job("submitted")["state"] == "succeeded"
        assert broker.backend.calls == 1 and not broker._execution_intents


@pytest.mark.parametrize("when", ["before_approval", "before_worker"])
async def test_original_deadline_is_never_extended_by_explicit_lease_renewal(tmp_path, when):
    async with running(tmp_path) as (broker, service, clock):
        digest = prepare(broker)
        submit(broker, digest)
        clock.now += 10
        broker.renew(RenewRequest(session=broker.session.capability, lease_seconds=300))
        assert service.detail(digest)["intent_remaining_seconds"] == 20
        assert service.detail(digest)["lease_remaining_seconds"] == 300
        assert submit(broker, digest)["deduplicated"]
        if when == "before_worker":
            approve(service, digest)
        clock.now += 20
        broker.maintenance_tick()
        assert broker.session is not None
        assert broker.job("submitted")["state"] == "cancelled"
        assert broker.job("submitted")["result"] == "execution_intent_expired"
        assert not broker._execution_intents
        assert submit(broker, digest)["state"] == "cancelled"
        if when == "before_approval":
            assert service.list_pending()["plans"] == []
            with pytest.raises(BrokerError, match="^plan_job_already_exists$"):
                service.detail(digest)
        await broker.queue.join()
        assert broker.backend.calls == 0


@pytest.mark.parametrize("transition", ["expire", "revoke", "scope", "exception"])
async def test_expired_or_unauthorized_intent_is_cancelled_before_approval(tmp_path, transition):
    async with running(tmp_path) as (broker, service, clock):
        digest = prepare(broker)
        submit(broker, digest)
        revision = service.detail(digest)["review_revision"]
        if transition == "expire":
            clock.now += 30
        elif transition == "revoke":
            broker.principal_valid = lambda *args: False
        elif transition == "scope":
            broker.principal_valid = lambda principal, owner, scopes: "control" not in scopes
        else:

            def fail(*args):
                raise RuntimeError("SYNTHETIC_PRIVATE_EXCEPTION")

            broker.principal_valid = fail
        broker.maintenance_tick()
        assert broker.job("submitted")["state"] == "cancelled"
        with pytest.raises(BrokerError):
            service.approve(digest, revision, "")
        assert broker.backend.calls == 0 and approval_count(broker) == 0


async def test_cancel_awaiting_and_release_reacquire_cannot_resubmit_old_job(tmp_path):
    async with running(tmp_path) as (broker, service, _):
        first = prepare(broker, "cancelled")
        submit(broker, first)
        broker.cancel(CancelRequest(session=broker.session.capability, job_id="cancelled"))
        assert submit(broker, first)["state"] == "cancelled"
        assert not broker._execution_intents
        second = prepare(broker, "released")
        submit(broker, second)
        lease = broker.session
        broker.release(LeaseRequest(session=lease.capability))
        assert broker.job("released")["state"] == "cancelled"
        broker.acquire(AcquireRequest(owner=Origin.REMOTE), principal_id="synthetic-client")
        assert broker.session.id != lease.id
        with pytest.raises(BrokerError, match="^plan_session_mismatch$"):
            submit(broker, second)
        assert service.list_pending()["plans"] == []
        assert broker.backend.calls == 0


@pytest.mark.parametrize("transition", ["close", "expire", "scope", "missing"])
async def test_intent_is_bound_to_work_session(tmp_path, transition):
    async with running(tmp_path, work_session=True) as (broker, service, clock):
        digest = prepare(broker)
        submit(broker, digest)
        work = broker.work_sessions[broker.session.work_session_id]
        if transition == "close":
            broker.close_work_session(
                CloseWorkSessionRequest(resume_token=work.resume_token),
                principal_id="synthetic-client",
                scopes=frozenset({"read", "control"}),
            )
        elif transition == "expire":
            work.deadline = clock.now
        elif transition == "scope":
            work.scopes = frozenset({"read"})
        else:
            broker.work_sessions.clear()
        broker.maintenance_tick()
        assert broker.job("submitted")["state"] == "cancelled"
        assert not broker._execution_intents
        assert broker.backend.calls == 0


async def test_pending_and_queued_share_capacity_without_consuming_approval(tmp_path):
    async with running(tmp_path, max_queue=2) as (broker, service, _):
        first = prepare(broker, "first")
        second = prepare(broker, "second")
        third = prepare(broker, "third")
        submit(broker, first)
        submit(broker, second)
        assert submit(broker, first)["deduplicated"]
        with pytest.raises(BrokerError, match="^queue_full$"):
            submit(broker, third)
        broker._store().approve(third)
        with pytest.raises(BrokerError, match="^queue_full$"):
            submit(broker, third, waiting=False)
        assert approval_count(broker) == 1
        approve(service, first)
        assert broker.queue.qsize() == 1
        with pytest.raises(BrokerError, match="^queue_full$"):
            submit(broker, third)
        await broker.queue.join()
        assert submit(broker, third)["state"] == "queued"
        await broker.queue.join()
        approve(service, second)
        await broker.queue.join()
        assert broker.backend.calls == 3 and broker.backend.max_active == 1


async def test_new_auto_execution_setting_never_picks_up_pending_intent(tmp_path):
    async with running(tmp_path) as (broker, service, _):
        digest = prepare(broker)
        submit(broker, digest)
        broker.settings = broker.settings.model_copy(update={"allow_mock_execution": True})
        assert submit(broker, digest)["state"] == "awaiting_approval"
        assert [row["plan_hash"] for row in service.list_pending()["plans"]] == [digest]
        assert broker.queue.empty() and approval_count(broker) == 0
        approve(service, digest)
        await broker.queue.join()
        assert broker.backend.calls == 1


async def test_new_capture_policy_never_automatically_dispatches_existing_intent(tmp_path):
    class Policies:
        available = False
        used = 0

        def authorize_capture(self, plan, *, record_use=True):
            if record_use:
                self.used += 1
            return object() if self.available else None

        def validate_capture(self, authorization, plan):
            return self.available

        def invalidate_session(self):
            pass

    async with running(tmp_path) as (broker, service, _):
        policies = Policies()
        broker.capture_policies = policies
        digest = prepare(broker, test=Action.CAPTURE)
        submit(broker, digest)
        used = policies.used
        policies.available = True
        assert submit(broker, digest)["state"] == "awaiting_approval"
        assert [row["plan_hash"] for row in service.list_pending()["plans"]] == [digest]
        assert policies.used == used
        assert broker.queue.empty()
        approve(service, digest)
        assert broker.policy_jobs == {}
        await broker.queue.join()
        assert broker.job("submitted")["state"] == "succeeded"


@pytest.mark.parametrize("change", ["deadline", "principal", "owner", "lease", "epoch", "plan"])
async def test_dispatch_rechecks_binding_after_final_principal_callback(tmp_path, change):
    async with running(tmp_path) as (broker, service, clock):
        digest = prepare(broker)
        submit(broker, digest)
        broker._store().approve(digest)

        def mutate(principal, owner, scopes):
            if change == "deadline":
                clock.now += 30
            elif change == "principal":
                broker.session.principal_id = "replacement"
            elif change == "owner":
                broker.session.owner = "local_cli"
            elif change == "lease":
                broker.session.id = "a" * 32
            elif change == "epoch":
                broker.epoch = "a" * 32
            else:
                broker._store().db.execute("UPDATE plans SET payload='{}' WHERE hash=?", (digest,))
                broker._store().db.commit()
            return True

        broker.principal_valid = mutate
        with pytest.raises(BrokerError, match="^execution_intent_not_available$"):
            broker.dispatch_pending_execution(digest)
        assert broker.job("submitted")["state"] == "cancelled"
        assert broker.queue.empty() and broker.backend.calls == 0
        assert approval_count(broker) == 1


@pytest.mark.parametrize("hold", ["active", "uncertain", "connected", "lock", "closing", "review"])
async def test_dispatch_never_bypasses_physical_ownership_or_shutdown(tmp_path, hold):
    async with running(tmp_path) as (broker, service, _):
        digest = prepare(broker)
        submit(broker, digest)
        broker._store().approve(digest)
        if hold == "active":
            broker.backend.active = 1
        elif hold == "uncertain":
            broker.backend.ownership_uncertain = True
        elif hold == "connected":
            broker.backend.hardware_connected = True
        elif hold == "lock":
            broker._device_lock.release()
        elif hold == "closing":
            broker.closing = True
        else:
            broker._store().add_job("uncertain-other", "a" * 64)
            broker._store().set_job("uncertain-other", "needs_review")
        try:
            with pytest.raises(BrokerError):
                broker.dispatch_pending_execution(digest)
            assert broker.queue.empty() and broker.backend.calls == 0
            assert approval_count(broker) == 1
            if hold in ("active", "uncertain"):
                with pytest.raises(BrokerError, match="^active_job_cannot_release$"):
                    broker.release(LeaseRequest(session=broker.session.capability))
                with pytest.raises(BrokerError, match="^device_busy$"):
                    broker.acquire(AcquireRequest(owner=Origin.LOCAL))
                assert broker._device_lock.is_locked
        finally:
            if hold == "lock":
                broker._device_lock.acquire()


async def test_cancel_cannot_interrupt_started_submitted_work(tmp_path):
    async with running(tmp_path) as (broker, service, _):
        broker.backend.gate = asyncio.Event()
        digest = prepare(broker)
        submit(broker, digest)
        approve(service, digest)
        await asyncio.sleep(0)
        assert broker.job("submitted")["state"] == "running"
        with pytest.raises(BrokerError, match="^only_queued_jobs_can_be_cancelled$"):
            broker.cancel(CancelRequest(session=broker.session.capability, job_id="submitted"))
        assert broker.active_job == "submitted"
        broker.backend.gate.set()
        await broker.queue.join()
        assert broker.backend.calls == 1


async def test_worker_preserves_cancellation_during_principal_recheck(tmp_path):
    async with running(tmp_path) as (broker, service, _):
        digest = prepare(broker)
        submit(broker, digest)
        approve(service, digest)
        calls = 0

        def cancel_in_callback(*args):
            nonlocal calls
            calls += 1
            if calls == 2:
                broker._cancel_execution_intent(
                    broker._execution_intents["submitted"], "client_cancelled_awaiting_approval"
                )
            return True

        broker.principal_valid = cancel_in_callback
        await broker.queue.join()
        assert calls >= 2
        assert broker.job("submitted")["state"] == "cancelled"
        assert broker.backend.calls == 0


async def test_worker_rechecks_original_deadline_after_local_home_pruning(tmp_path):
    async with running(tmp_path) as (broker, service, clock):
        digest = prepare(broker)
        submit(broker, digest)
        approve(service, digest)
        broker.local_home_diagnostic = LocalHomeDiagnostic(
            "synthetic",
            broker.epoch,
            "different-job",
            "a" * 64,
            "synthetic-client",
            "remote_mcp",
            broker.session.id,
            130.0,
        )
        calls = 0

        def expire_in_callback(*args):
            nonlocal calls
            calls += 1
            if calls == 4:
                clock.now = 500.0
            return True

        broker.principal_valid = expire_in_callback
        await broker.queue.join()
        assert calls >= 4
        assert broker.job("submitted")["state"] == "cancelled"
        assert broker.backend.calls == 0


@pytest.mark.parametrize(
    "mutation", ["cancel", "expire", "closing", "review", "physical", "revoke", "no-validator"]
)
async def test_worker_checks_every_callback_before_starting_backend(tmp_path, mutation):
    async with running(tmp_path) as (broker, service, clock):
        digest = prepare(broker)
        submit(broker, digest)
        approve(service, digest)

        class Policies:
            def target(self):
                if mutation == "cancel":
                    broker.cancel(
                        CancelRequest(session=broker.session.capability, job_id="submitted")
                    )
                elif mutation == "expire":
                    clock.now += 30
                elif mutation == "closing":
                    broker.closing = True
                elif mutation == "review":
                    broker._store().add_job("other", "b" * 64)
                    broker._store().set_job("other", "needs_review")
                elif mutation == "revoke":
                    broker.principal_valid = lambda *args: False
                elif mutation == "no-validator":
                    broker.principal_valid = None
                else:
                    broker.backend.ownership_uncertain = True
                return "synthetic-target"

            def invalidate_session(self):
                pass

        broker.capture_policies = Policies()
        await broker.queue.join()
        assert broker.job("submitted")["state"] == "cancelled"
        assert broker.backend.calls == 0


def test_restart_cancels_inert_waits_and_never_replays_queued_or_running(tmp_path):
    path = tmp_path / "history.sqlite3"
    store = Store(path)
    store.add_awaiting_job("awaiting", "a" * 64)
    store.add_job("queued", "b" * 64)
    store.add_job("running", "c" * 64)
    store.set_job("running", "running")
    store.close()
    reopened = Store(path)
    try:
        reopened.recover()
        assert reopened.job("awaiting")["state"] == "cancelled"
        assert reopened.job("awaiting")["result"] == "broker_restart_no_replay"
        assert reopened.job("queued")["state"] == "needs_review"
        assert reopened.job("running")["state"] == "needs_review"
        assert reopened.needs_review()
    finally:
        reopened.close()


async def test_orphan_awaiting_row_cannot_be_locally_approved(tmp_path):
    async with running(tmp_path) as (broker, service, _):
        digest = prepare(broker)
        broker._store().add_awaiting_job("submitted", digest)
        assert service.list_pending()["plans"] == []
        with pytest.raises(BrokerError, match="^plan_job_already_exists$"):
            service.detail(digest)
        assert approval_count(broker) == 0


async def test_intent_never_persists_or_logs_capabilities_or_deadline(tmp_path):
    async with running(tmp_path, work_session=True) as (broker, service, _):
        lease = broker.session
        work = broker.work_sessions[lease.work_session_id]
        digest = prepare(broker)
        submit(broker, digest)
        visible = json.dumps(
            [
                service.detail(digest),
                service.list_pending(),
                broker.status(),
                broker.read_logs(LogRequest()),
                broker.job("submitted"),
            ]
        )
        persisted = "\n".join(broker._store().db.iterdump())
        for secret in (lease.capability, work.resume_token):
            assert secret not in visible and secret not in persisted
            assert secret not in repr(broker._execution_intents)
        assert "deadline" not in persisted
        approve(service, digest)
        await broker.queue.join()
        assert not broker._execution_intents


async def test_shutdown_cancels_pending_without_replay_on_restart(tmp_path):
    async with running(tmp_path) as (broker, service, _):
        digest = prepare(broker)
        submit(broker, digest)
        await broker.close()
        assert not broker._execution_intents and broker.backend.calls == 0
        assert not broker._device_lock.is_locked
    async with running(tmp_path) as (broker, service, _):
        assert broker.job("submitted")["state"] == "cancelled"
        assert broker.job("submitted")["result"] == "broker_shutdown"
        assert broker.backend.calls == 0 and broker.queue.empty()
        assert not broker._execution_intents


async def test_uncertain_close_after_submitted_execution_never_hands_over(tmp_path):
    class UncertainClose(MockBackend):
        ownership_uncertain = False

        async def execute(self, plan, stage):
            self.calls += 1
            self.active = 1
            self.ownership_uncertain = True
            raise DeviceOperationFailure("serial_close_unconfirmed", step="close_serial")

    backend = UncertainClose(0.001)
    async with running(tmp_path, backend=backend) as (broker, service, clock):
        digest = prepare(broker)
        other = prepare(broker, "pending-other")
        submit(broker, digest)
        submit(broker, other)
        owner_lease = broker.session
        waiting = broker.create_work_session(
            CreateWorkSessionRequest(owner=Origin.LOCAL),
            principal_id="other-client",
            scopes=frozenset({"read", "control"}),
        )
        broker.request_device(
            RequestDeviceRequest(resume_token=waiting["resume_token"]),
            principal_id="other-client",
            scopes=frozenset({"read", "control"}),
        )
        approve(service, digest)
        await broker.queue.join()
        assert broker.job("submitted")["state"] == "needs_review"
        assert broker.job("pending-other")["state"] == "cancelled"
        assert not broker._execution_intents
        clock.now += 30
        broker.maintenance_tick()
        assert broker.session is owner_lease
        assert len(broker.waiters) == 1
        with pytest.raises(BrokerError, match="^device_close_unconfirmed$"):
            await broker.close()
        assert broker._device_lock.is_locked and broker._state_lock.is_locked
        assert broker.store is not None and backend.calls == 1


def test_waiting_transition_consumes_manual_approval_in_one_transaction(tmp_path):
    store = Store(tmp_path / "history.sqlite3")
    digest = "a" * 64
    try:
        store.put_plan(digest, {})
        store.add_awaiting_job("one", digest)
        with pytest.raises(ValueError, match="operator_approval_required"):
            store.approved_enqueue("one", digest, False, awaiting_approval=True)
        assert store.job("one")["state"] == "awaiting_approval"
        store.approve(digest)
        store.db.execute("""CREATE TRIGGER fail_enqueue BEFORE UPDATE OF state ON jobs
            WHEN NEW.state='queued' BEGIN SELECT RAISE(ABORT, 'synthetic failure'); END""")
        store.db.commit()
        with pytest.raises(sqlite3.IntegrityError, match="synthetic failure"):
            store.approved_enqueue("one", digest, False, awaiting_approval=True)
        assert store.job("one")["state"] == "awaiting_approval"
        assert store.db.execute("SELECT count(*) FROM approvals").fetchone()[0] == 1
        store.db.execute("DROP TRIGGER fail_enqueue")
        store.db.commit()
        store.approved_enqueue("one", digest, False, awaiting_approval=True)
        assert store.job("one")["state"] == "queued"
        assert store.db.execute("SELECT count(*) FROM approvals").fetchone()[0] == 0
    finally:
        store.close()
