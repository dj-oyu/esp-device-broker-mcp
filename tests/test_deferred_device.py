"""A device job requested ahead of time waits for its parent and the device (mock backend)."""

import asyncio

import pytest
from test_continuation import Clock, request, run_job, running, work_session

from cardputer_mcp.broker import BrokerError, digest_plan
from cardputer_mcp.models import ExecuteRequest, LeaseRequest, PrepareRequest
from cardputer_mcp.models import TestName as Action


def fake_parent(broker, principal, job_id, state="running"):
    """A registered-project style parent that has not finished, with a ticket."""
    plan = {
        "version": 2,
        "execution_context": "registered_project",
        "broker_epoch": broker.epoch,
        "job_id": job_id,
        "test": "firmware_build",
        "principal_id": principal,
        "origin": "remote_mcp",
        "project_id": "fixture",
    }
    digest = digest_plan(plan)
    store = broker._store()
    store.put_plan(digest, plan)
    store.add_job(job_id, digest)
    store.set_job(job_id, state)
    return broker.issue_ticket(job_id)


def defer(broker, principal, ticket, job_id, **extra):
    prepared = broker.prepare_deferred(
        PrepareRequest(job_id=job_id, test=Action.DIAGNOSTICS, after=ticket, **extra),
        principal,
        "remote_mcp",
    )
    queued = broker.execute_deferred(
        ExecuteRequest(plan_hash=prepared["plan_hash"]), principal, "remote_mcp"
    )
    return prepared, queued


async def settle(broker, job_id, states=("succeeded", "failed", "cancelled", "needs_review")):
    for _ in range(600):
        broker.maintenance_tick()
        job = broker._store().job(job_id)
        if job is not None and job["state"] in states:
            return job
        await asyncio.sleep(0.01)
    raise AssertionError(f"{job_id} did not settle")


async def hold_for_a(broker):
    work = work_session(broker, "key-a")
    lease = request(broker, work, "key-a")["lease"]["session"]
    await run_job(broker, lease, "a1")
    ticket = broker.issue_ticket("a1")
    broker.maintenance_tick()
    assert broker.hold is not None and broker.hold.ticket_id == ticket
    return ticket


async def test_scenario_other_chain_holds_then_deferred_job_runs_without_a_client_lease(tmp_path):
    clock = Clock()
    async with running(tmp_path, clock) as broker:
        ticket_a = await hold_for_a(broker)
        ticket_b = fake_parent(broker, "key-b", "b-build")
        prepared, queued = defer(broker, "key-b", ticket_b, "b-test")
        assert queued["state"] == "waiting_parent" and queued["deduplicated"] is False
        assert broker._store().job("b-test") is None
        assert broker.tickets._row(ticket_b)["state"] == "consumed"
        # Parent finishes: the job takes a queue position but A's reservation still blocks.
        broker._store().set_job("b-build", "succeeded", "ok")
        broker.maintenance_tick()
        view = broker.deferred_job("b-test", "key-b")
        assert view["state"] == "waiting_for_device" and view["device_queue_position"] == 1
        assert broker.session is None and broker.hold is not None
        # A continues first, ahead of B, with the ticket it holds.
        follower = work_session(broker, "key-a")
        granted = request(broker, follower, "key-a", after=ticket_a)
        assert granted["lease"] is not None and broker.session.principal_id == "key-a"
        await run_job(broker, granted["lease"]["session"], "a2")
        assert broker.deferred_job("b-test", "key-b")["state"] == "waiting_for_device"
        # A lets go; B's job gets its own lease and runs with no capability handed out.
        broker.release(LeaseRequest(session=granted["lease"]["session"]))
        job = await settle(broker, "b-test")
        assert job["state"] == "succeeded"
        assert job["plan_hash"] != prepared["plan_hash"]
        resolved = broker._store().plan(job["plan_hash"])
        assert broker.job_principal("b-test") == "key-b" and resolved["session_id"]
        row = broker.deferred.get("b-test")
        assert row["state"] == "promoted" and row["resolved_plan_hash"] == job["plan_hash"]
        broker.maintenance_tick()
        assert broker.session is None  # the lease ended with its job


async def test_job_lease_becomes_the_reservation_when_the_job_names_a_child(tmp_path):
    clock = Clock()
    async with running(tmp_path, clock) as broker:
        ticket = fake_parent(broker, "key-b", "b-build")
        defer(broker, "key-b", ticket, "b-test")
        child = broker.issue_ticket("b-test")  # a waiting job may name its own child
        broker._store().set_job("b-build", "succeeded", "ok")
        await settle(broker, "b-test")
        broker.maintenance_tick()
        assert broker.session is None
        assert broker.hold is not None and broker.hold.ticket_id == child


async def test_parent_failure_cancels_the_waiting_job(tmp_path):
    clock = Clock()
    async with running(tmp_path, clock) as broker:
        ticket = fake_parent(broker, "key-b", "b-build")
        defer(broker, "key-b", ticket, "b-test")
        broker._store().set_job("b-build", "failed", "boom")
        broker.maintenance_tick()
        view = broker.deferred_job("b-test", "key-b")
        assert view["state"] == "cancelled" and view["result"] == "chain_broken"
        assert broker._store().job("b-test") is None


async def test_device_write_between_parent_and_child_is_refused(tmp_path):
    clock = Clock()
    async with running(tmp_path, clock) as broker:
        ticket = await hold_for_a(broker)
        broker._store().db.execute("UPDATE device_writes SET seq=seq+1")  # someone wrote flash
        broker._store().db.commit()
        defer(broker, "key-a", ticket, "a-test")
        broker.maintenance_tick()
        view = broker.deferred_job("a-test", "key-a")
        assert view["state"] == "cancelled" and view["result"] == "device_state_changed"
        assert broker.session is None and broker._store().job("a-test") is None


async def test_unchanged_device_lets_the_continuation_run_even_after_hold_expiry(tmp_path):
    clock = Clock()
    async with running(tmp_path, clock) as broker:
        ticket = await hold_for_a(broker)
        defer(broker, "key-a", ticket, "a-test")
        job = await settle(broker, "a-test")
        assert job["state"] == "succeeded"


async def test_waiting_job_can_be_cancelled_and_leaves_the_queue(tmp_path):
    clock = Clock()
    async with running(tmp_path, clock) as broker:
        await hold_for_a(broker)
        ticket_b = fake_parent(broker, "key-b", "b-build")
        defer(broker, "key-b", ticket_b, "b-test")
        broker._store().set_job("b-build", "succeeded", "ok")
        broker.maintenance_tick()
        assert broker.deferred_job("b-test", "key-b")["state"] == "waiting_for_device"
        assert broker.cancel_deferred("b-test", "key-a") is None  # not the owner
        cancelled = broker.cancel_deferred("b-test", "key-b")
        assert cancelled["state"] == "cancelled"
        assert not broker.waiters
        assert broker._store().job("b-test") is None


async def test_ticket_is_owner_bound_and_single_use_for_device_jobs(tmp_path):
    clock = Clock()
    async with running(tmp_path, clock) as broker:
        ticket = fake_parent(broker, "key-b", "b-build")
        with pytest.raises(BrokerError, match="ticket_invalid"):
            defer(broker, "key-a", ticket, "x-test")
        defer(broker, "key-b", ticket, "b-test")
        with pytest.raises(BrokerError, match="ticket_consumed"):
            defer(broker, "key-b", ticket, "b-test-2")


def test_session_and_after_are_mutually_exclusive_and_backup_cannot_be_deferred():
    with pytest.raises(ValueError, match="session_not_allowed_with_after"):
        PrepareRequest(job_id="j", session="a" * 64, after="ABCDEFGH")
    with pytest.raises(ValueError, match="after_only_for_registered_build_without_receipt"):
        PrepareRequest(job_id="j", test=Action.BACKUP, device_id="dev", after="ABCDEFGH")
    with pytest.raises(ValueError, match="lease_seconds_only_with_after"):
        PrepareRequest(job_id="j", session="a" * 64, lease_seconds=30)
