"""Characterization (stage 0.5): ticket limits, TTL, hold start/release and fence gaps.

Pins today's numbers and FIFO moves (``docs/redesign/contracts/jobs.md`` section 2) and
records the known fence gap of DESIGN-REDESIGN section 2.1-2 as strict xfails that state
the desired behaviour. Mock backend and synthetic state only; no hardware.
"""

import asyncio

import pytest
from test_continuation import (
    Clock,
    first_job_with_ticket,
    request,
    run_job,
    running,
    work_session,
)
from test_deferred_device import defer, fake_parent, settle
from test_stage05_cancellation import until_running

from cardputer_mcp import continuation
from cardputer_mcp.broker import BrokerError, DeviceWait
from cardputer_mcp.continuation import (
    HOLD_SECONDS,
    MAX_ISSUED_TICKETS,
    MAX_PENDING_DEFERRED,
    TICKET_TTL_SECONDS,
    Deferred,
    TicketError,
    Tickets,
)
from cardputer_mcp.models import ExecuteRequest, LeaseRequest, PrepareRequest
from cardputer_mcp.store import Store

FENCE_GAP = "DESIGN-REDESIGN §2.1-2: fence_seq is set only for tickets still unspent at parent end"


def test_published_limits_and_durations():
    assert (MAX_ISSUED_TICKETS, MAX_PENDING_DEFERRED) == (256, 64)
    assert (HOLD_SECONDS, TICKET_TTL_SECONDS, continuation.CHAIN_BUDGET_SECONDS) == (
        60.0,
        1800.0,
        1800.0,
    )


@pytest.fixture
def store(tmp_path):
    opened = Store(tmp_path / "history.sqlite3")
    try:
        yield opened
    finally:
        opened.close()


# --- ticket_limit ------------------------------------------------------------------------


def test_ticket_limit_counts_only_issued_tickets(store):
    tickets = Tickets(lambda: store, lambda: "epoch-1")
    issued = [tickets.issue(f"parent-{n}", "key-a") for n in range(MAX_ISSUED_TICKETS)]
    assert len(set(issued)) == MAX_ISSUED_TICKETS
    with pytest.raises(TicketError, match="^ticket_limit$"):
        tickets.issue("one-too-many", "key-a")
    # Idempotent re-issue for a known parent is answered before the limit.
    assert tickets.issue("parent-0", "key-a") == issued[0]
    # Any non-issued state frees a slot.
    tickets.consume(issued[0], "key-a")
    room = tickets.issue("one-too-many", "key-a")
    with pytest.raises(TicketError, match="^ticket_limit$"):
        tickets.issue("another", "key-a")
    for state in ("void", "expired"):
        tickets.mark_state(room if state == "void" else issued[1], state, "synthetic")
    tickets.issue("another", "key-a")
    tickets.issue("and-another", "key-a")


async def test_broker_issue_ticket_reports_ticket_limit(tmp_path):
    clock = Clock()
    async with running(tmp_path, clock) as broker:
        for n in range(MAX_ISSUED_TICKETS):
            broker.tickets.issue(f"synthetic-{n}", "key-z")
        with pytest.raises(BrokerError, match="^ticket_limit$"):
            fake_parent(broker, "key-b", "b-build")
        assert broker.tickets.row_for_parent("b-build") is None


# --- deferred queue_full -----------------------------------------------------------------


def test_deferred_queue_full_counts_only_pending_rows(store):
    deferred = Deferred(lambda: store, lambda: "epoch-1")
    for n in range(MAX_PENDING_DEFERRED):
        deferred.add(f"job-{n}", "h", "parent", "key-a", "remote_mcp")
    with pytest.raises(TicketError, match="^queue_full$"):
        deferred.add("one-too-many", "h", "parent", "key-a", "remote_mcp")
    assert deferred.get("one-too-many") is None
    deferred.finish("job-0", "cancelled", "synthetic")
    deferred.add("one-too-many", "h", "parent", "key-a", "remote_mcp")


async def test_execute_deferred_queue_full_leaves_the_ticket_unspent(tmp_path):
    clock = Clock()
    async with running(tmp_path, clock) as broker:
        for n in range(MAX_PENDING_DEFERRED):
            broker.deferred.add(f"synthetic-{n}", "h", "nowhere", "key-z", "remote_mcp")
        ticket = fake_parent(broker, "key-b", "b-build")
        with pytest.raises(BrokerError, match="^queue_full$"):
            defer(broker, "key-b", ticket, "b-test")
        assert broker.tickets._row(ticket)["state"] == "issued"
        assert broker.deferred.get("b-test") is None


# --- TICKET_TTL for hold='none' ----------------------------------------------------------


def backdate_ticket(broker, ticket, seconds):
    broker._store().db.execute(
        "UPDATE continuation_tickets SET created=created-? WHERE ticket_id=?", (seconds, ticket)
    )
    broker._store().db.commit()


@pytest.mark.parametrize(
    ("age", "state"),
    [
        pytest.param(TICKET_TTL_SECONDS - 60, "issued", id="inside-ttl"),
        pytest.param(TICKET_TTL_SECONDS + 60, "expired", id="past-ttl"),
    ],
)
async def test_unheld_ticket_expires_after_ttl_from_issue(tmp_path, age, state):
    clock = Clock()
    async with running(tmp_path, clock) as broker:
        # A registered parent has no session_id: its ticket never holds the device.
        ticket = fake_parent(broker, "key-b", "b-build", state="succeeded")
        broker.maintenance_tick()
        row = broker.tickets._row(ticket)
        assert (row["state"], row["hold"], row["fence_seq"]) == ("issued", "none", None)
        backdate_ticket(broker, ticket, age)
        broker.maintenance_tick()
        row = broker.tickets._row(ticket)
        assert row["state"] == state
        assert row["reason"] == ("hold_expired" if state == "expired" else None)


async def test_ttl_counts_from_issue_and_needs_one_tick_after_hold_none(tmp_path):
    clock = Clock()
    async with running(tmp_path, clock) as broker:
        ticket = fake_parent(broker, "key-b", "b-build", state="running")
        backdate_ticket(broker, ticket, TICKET_TTL_SECONDS + 60)
        broker.maintenance_tick()
        assert broker.tickets._row(ticket)["state"] == "issued"  # parent still running
        broker._store().set_job("b-build", "succeeded", "ok")
        broker.maintenance_tick()  # first decision: hold='none'
        row = broker.tickets._row(ticket)
        assert (row["state"], row["hold"]) == ("issued", "none")
        broker.maintenance_tick()  # TTL checked on the next tick
        assert broker.tickets._row(ticket)["state"] == "expired"


@pytest.mark.parametrize("marker", ["held", "ended"])
async def test_issued_ticket_with_held_or_ended_marker_has_no_ttl(tmp_path, marker):
    clock = Clock()
    async with running(tmp_path, clock) as broker:
        ticket = fake_parent(broker, "key-b", "b-build", state="succeeded")
        broker.tickets.set_hold(ticket, marker)
        backdate_ticket(broker, ticket, TICKET_TTL_SECONDS * 10)
        broker.maintenance_tick()
        assert broker.tickets._row(ticket)["state"] == "issued"


# --- hold start is deferred while the lane is not idle -----------------------------------


async def lease_with_finished_parent(broker):
    work = work_session(broker, "key-a")
    lease = request(broker, work, "key-a")["lease"]["session"]
    await run_job(broker, lease, "job-1")
    return lease


def queue_job(broker, lease, job_id):
    plan = broker.prepare(PrepareRequest(session=lease, job_id=job_id))
    broker.execute(ExecuteRequest(session=lease, plan_hash=plan["plan_hash"]))


def _queued(broker, lease):
    queue_job(broker, lease, "job-2")

    async def clear():
        await broker.queue.join()  # the worker runs job-2; its final tick may start the hold

    return clear


def _intent_only(broker, lease):
    queue_job(broker, lease, "job-2")
    assert broker.queue.get_nowait() == "job-2"
    broker.queue.task_done()
    assert broker.queue.empty() and "job-2" in broker._execution_intents

    async def clear():
        intent = broker._execution_intents["job-2"]
        broker._cancel_execution_intent(intent, "synthetic_clear")

    return clear


def _busy(broker, lease):
    broker.active_job = "synthetic-busy"

    async def clear():
        broker.active_job = None

    return clear


@pytest.mark.parametrize("blocker", [_queued, _intent_only, _busy])
async def test_hold_waits_while_queue_intents_or_busy_then_starts_next_tick(tmp_path, blocker):
    clock = Clock()
    async with running(tmp_path, clock) as broker:
        lease = await lease_with_finished_parent(broker)
        owner = broker.session
        clear = blocker(broker, lease)
        ticket = broker.issue_ticket("job-1")
        broker.maintenance_tick()
        row = broker.tickets._row(ticket)
        assert broker.hold is None and broker.session is owner
        # The fence is taken before the idle check; the decision is retried later.
        assert row["state"] == "issued" and row["hold"] is None
        assert row["fence_seq"] == broker._store().device_write_seq()
        await clear()
        broker.maintenance_tick()
        assert broker.hold is not None and broker.hold.ticket_id == ticket
        assert broker.tickets._row(ticket)["hold"] == "held"
        assert broker.session is None  # the lease became the reservation
        assert broker._store().request(owner.request_id)["reason"] == "chain_hold"


# --- _release_hold FIFO ------------------------------------------------------------------


async def test_expired_hold_returns_a_spent_ticket_follower_to_the_back(tmp_path):
    """Restores the test trimmed in 1307df4, now checking the actual tail move."""
    clock = Clock()
    async with running(tmp_path, clock) as broker:
        _, ticket = await first_job_with_ticket(broker)
        other = work_session(broker, "key-b")
        waiting_b = request(broker, other, "key-b")["request"]["request_id"]
        broker.backend.active = 1  # keep the slot from being granted for now
        try:
            follower = work_session(broker, "key-a")
            spent = request(broker, follower, "key-a", after=ticket)["request"]["request_id"]
            # Spending the held ticket in time jumps the queue.
            assert broker.waiters == [spent, waiting_b]
            assert broker.tickets._row(ticket)["state"] == "consumed"
            clock.now += HOLD_SECONDS + 1
            broker.maintenance_tick()
            assert broker.hold is None
            assert broker.waiters == [waiting_b, spent]
            row = broker.tickets._row(ticket)
            assert (row["state"], row["hold"]) == ("consumed", "ended")
        finally:
            broker.backend.active = 0
        broker.maintenance_tick()
        assert broker.session is not None and broker.session.principal_id == "key-b"
        assert broker.waiters == [spent]


async def test_release_hold_moves_every_follower_to_the_tail_in_order(tmp_path):
    clock = Clock()
    async with running(tmp_path, clock) as broker:
        _, ticket = await first_job_with_ticket(broker)
        sequence = broker._queue_sequence
        waits = {
            name: DeviceWait(name, "synthetic", 60, clock.now + 600, ticket_id=ticket_id)
            for name, ticket_id in (("f1", ticket), ("x", None), ("f2", ticket), ("y", "OTHER"))
        }
        broker.device_requests.update(waits)
        broker.waiters[:] = ["f1", "x", "f2", "y"]
        # No await until the synthetic waiters are gone: the maintenance task must not see them.
        broker._release_hold("hold_expired")
        order = list(broker.waiters)
        broker.waiters.clear()
        for name in waits:
            del broker.device_requests[name]
        assert order == ["x", "y", "f1", "f2"]
        assert broker.hold is None and broker._queue_sequence == sequence + 1
        row = broker.tickets._row(ticket)
        assert (row["state"], row["reason"], row["hold"]) == ("expired", "hold_expired", "ended")


# --- fence gap (§2.1-2) ------------------------------------------------------------------


def bump_device_write(broker):
    broker._store().db.execute("UPDATE device_writes SET seq=seq+1 WHERE id=1")
    broker._store().db.commit()


async def child_spent_while_parent_runs_then_device_written(broker):
    """Parent job-1 runs, its ticket is spent ahead of time, a write lands after it ends."""
    work = work_session(broker, "key-a")
    lease = request(broker, work, "key-a")["lease"]["session"]
    gate = broker.backend.gate = asyncio.Event()
    try:
        queue_job(broker, lease, "job-1")
        ticket = broker.issue_ticket("job-1")
        await until_running(broker, "job-1")
        defer(broker, "key-a", ticket, "a-test")
        assert broker.tickets._row(ticket)["state"] == "consumed"
    finally:
        gate.set()  # never leave the worker parked, or close() waits forever
        broker.backend.gate = None
    await broker.queue.join()
    assert broker._store().job("job-1")["state"] == "succeeded"
    broker.maintenance_tick()
    bump_device_write(broker)  # someone else wrote flash between parent and child
    broker.release(LeaseRequest(session=lease))
    await settle(broker, "a-test")
    return ticket


async def test_current_ticket_spent_before_parent_end_gets_no_fence(tmp_path):
    clock = Clock()
    async with running(tmp_path, clock) as broker:
        ticket = await child_spent_while_parent_runs_then_device_written(broker)
        assert broker.tickets._row(ticket)["fence_seq"] is None
        row = broker.deferred.get("a-test")
        assert row["state"] == "promoted"
        assert broker._store().job("a-test")["state"] == "succeeded"


@pytest.mark.xfail(strict=True, reason=FENCE_GAP)
async def test_ticket_spent_while_parent_runs_is_still_fenced(tmp_path):
    clock = Clock()
    async with running(tmp_path, clock) as broker:
        await child_spent_while_parent_runs_then_device_written(broker)
        row = broker.deferred.get("a-test")
        assert (row["state"], row["reason"]) == ("cancelled", "device_state_changed")
        assert broker._store().job("a-test") is None


async def test_current_request_device_after_does_not_check_the_fence(tmp_path):
    clock = Clock()
    async with running(tmp_path, clock) as broker:
        _, ticket = await first_job_with_ticket(broker)
        fence = broker.tickets._row(ticket)["fence_seq"]
        assert fence == broker._store().device_write_seq()
        bump_device_write(broker)
        follower = work_session(broker, "key-a")
        granted = request(broker, follower, "key-a", after=ticket)
        assert granted["lease"] is not None
        assert broker._store().device_write_seq() != fence


@pytest.mark.xfail(strict=True, reason=FENCE_GAP + "; request_device(after=) has no fence check")
async def test_request_device_after_refuses_a_changed_device(tmp_path):
    clock = Clock()
    async with running(tmp_path, clock) as broker:
        _, ticket = await first_job_with_ticket(broker)
        bump_device_write(broker)
        follower = work_session(broker, "key-a")
        with pytest.raises(BrokerError, match="^device_state_changed$"):
            request(broker, follower, "key-a", after=ticket)
