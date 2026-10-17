"""Already-finished synthetic executors; no ports, device IO or real clocks."""

import asyncio

import pytest
from test_serial_observation import device

from cardputer_mcp.serial_backend import SerialFailure


@pytest.mark.parametrize("failure", [False, True])
async def test_stage_callbacks_precede_fast_worker_result_and_owner_release(monkeypatch, failure):
    backend, port, plan = device()
    loop = asyncio.get_running_loop()
    events = []

    def synthetic_worker(plan, emit, *unused):
        emit("serial_open")
        emit("serial_close")
        if failure:
            raise SerialFailure("serial_read_failed")
        return "synthetic_success"

    def already_done(executor, function, *args):
        future = loop.create_future()
        try:
            future.set_result(function(*args))
        except SerialFailure as error:
            future.set_exception(error)
        return future

    monkeypatch.setattr(backend, "_execute_sync", synthetic_worker)
    monkeypatch.setattr(loop, "run_in_executor", already_done)

    def stage(value):
        events.append((value, backend.active))

    if failure:
        with pytest.raises(SerialFailure, match="^serial_read_failed$"):
            await backend.execute(plan, stage)
    else:
        assert await backend.execute(plan, stage) == "synthetic_success"
    assert events == [("serial_open", 1), ("serial_close", 1)]
    assert backend.active == port.opens == port.closes == 0 and port.writes == []
    await asyncio.sleep(0)
    assert len(events) == 2  # No notification arrives after completion.


async def test_cancel_during_stage_drain_keeps_owner_and_never_reports_success(monkeypatch):
    backend, port, plan = device()
    loop = asyncio.get_running_loop()
    events = []

    def already_done(executor, function, plan, emit, *unused):
        emit("serial_open")
        emit("serial_close")
        future = loop.create_future()
        future.set_result("synthetic_success")
        return future

    def stage(value):
        events.append((value, backend.active))
        if value == "serial_open":
            task.cancel()

    monkeypatch.setattr(loop, "run_in_executor", already_done)
    task = asyncio.create_task(backend.execute(plan, stage))
    with pytest.raises(SerialFailure, match="^operation_interrupted_review_required$"):
        await task
    assert events == [("serial_open", 1), ("serial_close", 1)]
    assert backend.active == port.opens == port.closes == 0 and port.writes == []
