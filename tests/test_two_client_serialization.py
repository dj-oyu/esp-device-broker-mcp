"""Two clients using one broker are serialized instead of refused.

The failures this covers were seen with two agent sessions sharing a broker: a
chained build cancelled because the other client's fetch held the source cache, a
second source read refused instead of queued, and a running build killed because
the event loop was busy with the other client's request.
"""

import asyncio
import threading

import pytest
from test_deferred_build import defer, finish_parent, parent_fetch, stage
from test_registry_http import registry_service as registry_service

from cardputer_mcp.authority_timing import AUTHORITY_CHECK_SECONDS
from cardputer_mcp.source_preflight import SourceReadWorker
from cardputer_mcp.source_snapshot import SourceError


async def test_chained_build_waits_while_another_client_holds_the_source_cache(
    registry_service, tmp_path, monkeypatch
):
    """Client B's build, chained after B's fetch, must survive client A's fetch."""
    broker, owner = await stage(registry_service, tmp_path, monkeypatch)
    other_client_busy = [True]

    def authorize(*args):
        if other_client_busy[0]:
            raise SourceError("source_fetch_busy")

    monkeypatch.setattr(broker.firmware_source, "authorize_receipt", authorize)
    ticket = parent_fetch(broker, owner)
    defer(broker, owner, ticket)
    finish_parent(broker)

    broker.maintenance_tick()
    waiting = broker.deferred_job("build-1", owner.principal_id)
    assert waiting["state"] == "waiting_parent"  # contention is not a broken chain
    assert broker._store().job("build-1") is None

    other_client_busy[0] = False  # client A's fetch published and released the cache
    broker.maintenance_tick()
    job = broker._store().job("build-1")
    assert job is not None and job["state"] == "queued"
    assert broker.deferred.get("build-1")["state"] == "promoted"


async def test_chained_build_still_fails_for_a_real_source_error(
    registry_service, tmp_path, monkeypatch
):
    broker, owner = await stage(registry_service, tmp_path, monkeypatch)

    def authorize(*args):
        raise SourceError("source_snapshot_changed")

    monkeypatch.setattr(broker.firmware_source, "authorize_receipt", authorize)
    defer(broker, owner, parent_fetch(broker, owner))
    finish_parent(broker)
    broker.maintenance_tick()
    view = broker.deferred_job("build-1", owner.principal_id)
    assert view["state"] == "cancelled" and view["result"] == "source_snapshot_changed"


async def test_two_clients_source_reads_run_one_after_another():
    """The second client's read waits for the first instead of failing as busy."""
    worker = SourceReadWorker()
    first_in, release_first = threading.Event(), threading.Event()
    order: list[str] = []

    def first(stop):
        order.append("first:start")
        first_in.set()
        assert release_first.wait(timeout=3)
        order.append("first:end")
        return "A"

    def second(stop):
        order.append("second:start")
        order.append("second:end")
        return "B"

    task_a = asyncio.create_task(worker.run(first))
    while not first_in.is_set():
        await asyncio.sleep(0.01)
    task_b = asyncio.create_task(worker.run(second))  # arrives while A is reading
    await asyncio.sleep(0.05)
    assert not task_b.done() and order == ["first:start"]  # queued, not refused
    release_first.set()
    assert await task_a == "A" and await task_b == "B"
    assert order == ["first:start", "first:end", "second:start", "second:end"]
    await worker.close()


async def test_a_waiting_source_read_is_refused_once_the_wait_is_over(monkeypatch):
    monkeypatch.setattr("cardputer_mcp.source_preflight.SOURCE_READ_WAIT_SECONDS", 0.05)
    worker = SourceReadWorker()
    entered, release = threading.Event(), threading.Event()

    def blocked(stop):
        entered.set()
        assert release.wait(timeout=3)

    task = asyncio.create_task(worker.run(blocked))
    while not entered.is_set():
        await asyncio.sleep(0.01)
    with pytest.raises(SourceError, match="source_fetch_busy"):
        await worker.run(lambda stop: None)  # bounded wait, then the old refusal
    release.set()
    await task
    await worker.close()


async def test_a_closing_worker_refuses_instead_of_queueing():
    worker = SourceReadWorker()
    await worker.close()
    with pytest.raises(SourceError, match="source_fetch_busy"):
        await worker.run(lambda stop: None)


def test_the_authority_check_outlasts_another_clients_slow_request():
    # A source-cache scan or a firmware hash for someone else's plan can hold the
    # loop for several seconds; the old 5 s limit killed the running build.
    assert AUTHORITY_CHECK_SECONDS >= 20
