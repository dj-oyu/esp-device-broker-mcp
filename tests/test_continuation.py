"""Continuation tickets and the device hold, using the mock backend and synthetic state."""

import asyncio
from contextlib import asynccontextmanager

import pytest

from cardputer_mcp.backend import MockBackend
from cardputer_mcp.broker import Broker, BrokerError
from cardputer_mcp.config import Settings
from cardputer_mcp.continuation import CHAIN_BUDGET_SECONDS, HOLD_SECONDS
from cardputer_mcp.models import (
    AcquireRequest,
    CreateWorkSessionRequest,
    ExecuteRequest,
    Origin,
    PrepareRequest,
    RequestDeviceRequest,
)

SCOPES = frozenset({"read", "control"})


class Clock:
    now = 100.0

    def __call__(self):
        return self.now


@asynccontextmanager
async def running(tmp_path, clock):
    broker = Broker(
        Settings(state_dir=tmp_path / "state", allow_mock_execution=True),
        backend=MockBackend(0.001),
        clock=clock,
        lock_path=tmp_path / "device.lockfile",
        principal_valid=lambda principal, owner, scopes: True,
    )
    await broker.start()
    try:
        yield broker
    finally:
        await broker.close()


def work_session(broker, principal):
    return broker.create_work_session(
        CreateWorkSessionRequest(owner=Origin.REMOTE, ttl_seconds=3600),
        principal_id=principal,
        scopes=SCOPES,
    )


def request(broker, work, principal, **kwargs):
    return broker.request_device(
        RequestDeviceRequest(resume_token=work["resume_token"], **kwargs),
        principal_id=principal,
        scopes=SCOPES,
    )


async def run_job(broker, lease, job_id):
    plan = broker.prepare(PrepareRequest(session=lease, job_id=job_id))
    broker.execute(ExecuteRequest(session=lease, plan_hash=plan["plan_hash"]))
    for _ in range(400):
        if broker.job(job_id)["state"] not in ("queued", "running"):
            break
        await asyncio.sleep(0.005)
    assert broker.job(job_id)["state"] == "succeeded"


async def first_job_with_ticket(broker, principal="key-a", job_id="job-1"):
    work = work_session(broker, principal)
    lease = request(broker, work, principal)["lease"]["session"]
    await run_job(broker, lease, job_id)
    ticket = broker.issue_ticket(job_id)
    broker.maintenance_tick()
    return work, ticket


async def test_success_starts_hold_and_only_the_holder_may_take_the_slot(tmp_path):
    clock = Clock()
    async with running(tmp_path, clock) as broker:
        _, ticket = await first_job_with_ticket(broker)
        assert broker.hold is not None and broker.hold.ticket_id == ticket
        assert broker.session is None
        assert broker.status()["device_hold"]["active"] is True
        with pytest.raises(BrokerError, match="device_busy"):
            broker.acquire(AcquireRequest(owner=Origin.REMOTE), principal_id="key-b")
        # Another principal queues but is never granted while the reservation lasts.
        other = work_session(broker, "key-b")
        waiting = request(broker, other, "key-b")
        broker.maintenance_tick()
        assert waiting["lease"] is None and broker.session is None
        # The holder spends the ticket and jumps the queue.
        follower = work_session(broker, "key-a")
        granted = request(broker, follower, "key-a", after=ticket)
        assert granted["lease"] is not None  # granted at once, ahead of the waiting key-b
        assert broker.session is not None and broker.session.principal_id == "key-a"
        assert waiting["request"]["request_id"] in broker.waiters
        assert broker.hold is None
        assert broker.session.chain_ticket == ticket


async def test_ticket_is_single_use_and_owned_by_the_parent_principal(tmp_path):
    clock = Clock()
    async with running(tmp_path, clock) as broker:
        _, ticket = await first_job_with_ticket(broker)
        stranger = work_session(broker, "key-b")
        with pytest.raises(BrokerError, match="ticket_invalid"):
            request(broker, stranger, "key-b", after=ticket)
        with pytest.raises(BrokerError, match="ticket_invalid"):
            request(broker, stranger, "key-b", after="ZZZZZZZZ")
        follower = work_session(broker, "key-a")
        request(broker, follower, "key-a", after=ticket)
        again = work_session(broker, "key-a")
        with pytest.raises(BrokerError, match="ticket_consumed"):
            request(broker, again, "key-a", after=ticket)


async def test_hold_expires_after_idle_period_and_late_ticket_is_refused(tmp_path):
    clock = Clock()
    async with running(tmp_path, clock) as broker:
        _, ticket = await first_job_with_ticket(broker)
        clock.now += HOLD_SECONDS + 1
        broker.maintenance_tick()
        assert broker.hold is None
        follower = work_session(broker, "key-a")
        with pytest.raises(BrokerError, match="hold_expired"):
            request(broker, follower, "key-a", after=ticket)
        assert broker.acquire(AcquireRequest(owner=Origin.REMOTE), principal_id="key-b")


async def test_failed_or_cancelled_parent_voids_its_ticket(tmp_path):
    clock = Clock()
    async with running(tmp_path, clock) as broker:
        work = work_session(broker, "key-a")
        lease = request(broker, work, "key-a")["lease"]["session"]
        plan = broker.prepare(PrepareRequest(session=lease, job_id="cancelled"))
        broker.execute(ExecuteRequest(session=lease, plan_hash=plan["plan_hash"]))
        ticket = broker.issue_ticket("cancelled")
        # Cancel before the worker takes the job.
        broker._store().set_job("cancelled", "cancelled", "client_cancelled_queued")
        broker.maintenance_tick()
        assert broker.hold is None
        follower = work_session(broker, "key-a")
        with pytest.raises(BrokerError, match="ticket_void"):
            request(broker, follower, "key-a", after=ticket)


async def test_cancel_ticket_releases_the_hold(tmp_path):
    clock = Clock()
    async with running(tmp_path, clock) as broker:
        _, ticket = await first_job_with_ticket(broker)
        with pytest.raises(BrokerError, match="ticket_invalid"):
            broker.cancel_ticket(ticket, "key-b")
        assert broker.cancel_ticket(ticket, "key-a") == {"cancelled": True}
        assert broker.hold is None
        assert broker.acquire(AcquireRequest(owner=Origin.REMOTE), principal_id="key-b")


async def test_chain_budget_stops_the_reservation(tmp_path):
    clock = Clock()
    async with running(tmp_path, clock) as broker:
        work = work_session(broker, "key-a")
        lease = request(broker, work, "key-a")["lease"]["session"]
        await run_job(broker, lease, "late")
        ticket = broker.issue_ticket("late")
        broker._store().db.execute(
            "UPDATE continuation_tickets SET chain_started=chain_started-? WHERE ticket_id=?",
            (CHAIN_BUDGET_SECONDS + 1, ticket),
        )
        broker.maintenance_tick()
        assert broker.hold is None


async def test_progress_follows_the_parent_links_and_hides_nothing_aggregate(tmp_path):
    clock = Clock()
    async with running(tmp_path, clock) as broker:
        _, ticket = await first_job_with_ticket(broker)
        progress = broker.chain_progress("job-1")
        assert progress is not None
        assert progress["chain_position"] == 1 and progress["chain_open"] is True
        assert progress["chain_state"] == "holding_device"
        assert 0 < progress["hold_expires_in_seconds"] <= HOLD_SECONDS
        follower = work_session(broker, "key-a")
        request(broker, follower, "key-a", after=ticket)
        broker.maintenance_tick()
        lease = broker.session.capability
        await run_job(broker, lease, "job-2")
        second = broker.chain_progress("job-2")
        assert second is not None and second["chain_position"] == 2
        assert second["chain_jobs_known"] == 2 and second["chain_open"] is False
        assert broker.chain_progress("missing") is None


async def test_restart_voids_unspent_tickets(tmp_path):
    clock = Clock()
    async with running(tmp_path, clock) as broker:
        _, ticket = await first_job_with_ticket(broker)
    async with running(tmp_path, clock) as broker:
        follower = work_session(broker, "key-a")
        with pytest.raises(BrokerError, match="ticket_void"):
            request(broker, follower, "key-a", after=ticket)
        assert broker.hold is None
