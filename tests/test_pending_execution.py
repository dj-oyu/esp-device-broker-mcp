"""Explicit execution intents retain authentication, ownership and cancellation guards."""

import asyncio
from contextlib import asynccontextmanager

import pytest

from cardputer_mcp.broker import Broker, BrokerError
from cardputer_mcp.config import Settings
from cardputer_mcp.models import (
    AcquireRequest,
    CancelRequest,
    ExecuteRequest,
    LeaseRequest,
    Origin,
    PrepareRequest,
    RenewRequest,
)


class Clock:
    now = 100.0

    def __call__(self):
        return self.now


@asynccontextmanager
async def running(tmp_path, *, max_queue=8):
    clock = Clock()
    broker = Broker(
        Settings(state_dir=tmp_path / "state", max_queue=max_queue, mock_stage_seconds=0.001),
        clock=clock,
        lock_path=tmp_path / "device.lockfile",
        principal_valid=lambda *args: True,
    )
    await broker.start()
    broker.acquire(AcquireRequest(owner=Origin.REMOTE), principal_id="synthetic-client")
    try:
        yield broker, clock
    finally:
        broker.closing = False
        broker.principal_valid = lambda *args: True
        if broker.backend.gate is not None:
            broker.backend.gate.set()
        await broker.close()


def prepare(broker, job="submitted"):
    return broker.prepare(
        PrepareRequest(session=broker.session.capability, job_id=job)
    )["plan_hash"]


def submit(broker, digest, *, waiting=True):
    return broker.execute(
        ExecuteRequest(
            session=broker.session.capability, plan_hash=digest, wait_for_approval=waiting
        )
    )


@pytest.mark.parametrize("waiting", [False, True])
async def test_only_explicit_execute_submits_once_without_operator_poll(tmp_path, waiting):
    async with running(tmp_path) as (broker, _):
        digest = prepare(broker)
        assert broker.queue.empty() and broker._store().record_count("jobs") == 0
        result = submit(broker, digest, waiting=waiting)
        assert result["state"] == "queued" and not result["deduplicated"]
        assert submit(broker, digest)["deduplicated"]
        assert broker.queue.qsize() == 1
        await broker.queue.join()
        assert broker.job("submitted")["state"] == "succeeded"
        assert broker.backend.calls == 1
        assert submit(broker, digest)["deduplicated"]
        assert broker._store().db.execute("SELECT count(*) FROM approvals").fetchone()[0] == 0


@pytest.mark.parametrize(
    "transition", ["revoke", "scope", "exception", "no-validator", "anonymous"]
)
async def test_explicit_request_requires_live_read_and_control_authority(tmp_path, transition):
    async with running(tmp_path) as (broker, _):
        digest = prepare(broker)
        if transition == "revoke":
            broker.principal_valid = lambda *args: False
        elif transition == "scope":
            broker.principal_valid = lambda principal, owner, scopes: "control" not in scopes
        elif transition == "no-validator":
            broker.principal_valid = None
        elif transition == "anonymous":
            broker.session.principal_id = None
        else:

            def fail(*args):
                raise RuntimeError("SYNTHETIC_PRIVATE_EXCEPTION")
            broker.principal_valid = fail
        with pytest.raises(BrokerError, match="^(principal_not_authorized|invalid_session)$"):
            submit(broker, digest)
        assert broker._store().record_count("jobs") == 0
        assert broker.backend.calls == 0


@pytest.mark.parametrize("transition", ["expire", "revoke", "scope", "exception", "no-validator"])
async def test_queued_request_rechecks_authority_before_worker_dispatch(tmp_path, transition):
    async with running(tmp_path) as (broker, clock):
        digest = prepare(broker)
        submit(broker, digest)
        if transition == "expire":
            clock.now += 30
        elif transition == "revoke":
            broker.principal_valid = lambda *args: False
        elif transition == "scope":
            broker.principal_valid = lambda principal, owner, scopes: "control" not in scopes
        elif transition == "no-validator":
            broker.principal_valid = None
        else:

            def fail(*args):
                raise RuntimeError("SYNTHETIC_PRIVATE_EXCEPTION")
            broker.principal_valid = fail
        await broker.queue.join()
        assert broker.job("submitted")["state"] == "cancelled"
        assert broker.backend.calls == 0


async def test_lease_renewal_does_not_extend_original_execution_deadline(tmp_path):
    async with running(tmp_path) as (broker, clock):
        digest = prepare(broker)
        submit(broker, digest)
        clock.now += 10
        broker.renew(RenewRequest(session=broker.session.capability, lease_seconds=300))
        clock.now += 20
        broker.maintenance_tick()
        assert broker.session is not None
        await broker.queue.join()
        assert broker.job("submitted")["state"] == "cancelled"
        assert broker.backend.calls == 0


async def test_cancelled_job_and_released_lease_never_replay(tmp_path):
    async with running(tmp_path) as (broker, _):
        digest = prepare(broker)
        submit(broker, digest)
        broker.cancel(CancelRequest(session=broker.session.capability, job_id="submitted"))
        assert submit(broker, digest)["state"] == "cancelled"
        other = prepare(broker, "released")
        submit(broker, other)
        broker.release(LeaseRequest(session=broker.session.capability))
        broker.acquire(AcquireRequest(owner=Origin.REMOTE), principal_id="synthetic-client")
        with pytest.raises(BrokerError, match="^plan_session_mismatch$"):
            submit(broker, other)
        await broker.queue.join()
        assert broker.job("released")["state"] == "cancelled"
        assert broker.backend.calls == 0


@pytest.mark.parametrize("mutation", ["cancel", "expire", "closing", "revoke", "plan"])
async def test_auth_callback_races_cannot_start_a_queued_operation(tmp_path, mutation):
    async with running(tmp_path) as (broker, clock):
        digest = prepare(broker)
        submit(broker, digest)
        invoked = False

        def mutate(*args):
            nonlocal invoked
            if not invoked:
                invoked = True
                if mutation == "cancel":
                    broker.cancel(
                        CancelRequest(session=broker.session.capability, job_id="submitted")
                    )
                elif mutation == "expire":
                    clock.now += 30
                elif mutation == "closing":
                    broker.closing = True
                elif mutation == "revoke":
                    broker.principal_valid = lambda *args: False
                else:
                    broker._store().db.execute("DELETE FROM plans WHERE hash=?", (digest,))
                    broker._store().db.commit()
            return True

        broker.principal_valid = mutate
        await broker.queue.join()
        assert invoked
        assert broker.job("submitted")["state"] == "cancelled"
        assert broker.backend.calls == 0


async def test_bounded_queue_cancel_and_no_second_operation(tmp_path):
    async with running(tmp_path, max_queue=1) as (broker, _):
        broker.backend.gate = asyncio.Event()
        submit(broker, prepare(broker, "active"))
        await asyncio.sleep(0)
        second = prepare(broker, "queued")
        submit(broker, second)
        third = prepare(broker, "overflow")
        with pytest.raises(BrokerError, match="^queue_full$"):
            submit(broker, third)
        broker.cancel(CancelRequest(session=broker.session.capability, job_id="queued"))
        broker.backend.gate.set()
        await broker.queue.join()
        assert broker.job("queued")["state"] == "cancelled"
        assert broker.backend.calls == 1


@pytest.mark.parametrize(
    "mutation", ["expire", "closing", "replace-validator", "replace-lease", "plan"]
)
async def test_request_callback_races_cannot_enqueue(tmp_path, mutation, monkeypatch):
    async with running(tmp_path) as (broker, clock):
        digest = prepare(broker)
        session = broker.session

        def mutate(_principal, _owner, scopes):
            if scopes != frozenset({"read", "control"}):
                return True
            if mutation == "expire":
                clock.now += 30
            elif mutation == "closing":
                broker.closing = True
            elif mutation == "replace-validator":
                broker.principal_valid = lambda *args: True
            elif mutation == "replace-lease":
                from dataclasses import replace

                broker.session = replace(session, id="different-lease")
            else:
                broker._store().db.execute("DELETE FROM plans WHERE hash=?", (digest,))
                broker._store().db.commit()
            return True

        from cardputer_mcp.plan_approval import PlanApprovals

        original = PlanApprovals.validate_current

        def checked(validator, plan_hash, lease):
            result = original(validator, plan_hash, lease)
            broker.principal_valid = mutate
            return result

        monkeypatch.setattr(PlanApprovals, "validate_current", checked)
        with pytest.raises(BrokerError):
            submit(broker, digest)
        assert broker.queue.empty()
        assert broker._store().record_count("jobs") == 0
        assert broker.backend.calls == 0
