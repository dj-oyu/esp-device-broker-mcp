"""Already-finished synthetic executors; no ports, device IO or real clocks."""

import asyncio

import pytest
from test_serial_observation import device

from cardputer_mcp.serial_backend import SerialFailure


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
