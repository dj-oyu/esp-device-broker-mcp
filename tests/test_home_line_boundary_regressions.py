"""Synthetic regressions for review findings; never touch a real serial port."""

import asyncio
import threading
from dataclasses import replace

import pytest
from test_home_line_integration import ATTESTATION, approve, prepare, running
from test_serial_capture import device

from cardputer_mcp import serial_backend
from cardputer_mcp.broker import BrokerError
from cardputer_mcp.home_line import HomeLineProbe
from cardputer_mcp.serial_backend import SerialFailure


@pytest.mark.parametrize("change", ["expiry", "lease", "epoch"])
async def test_final_callback_cannot_consume_stale_local_consent(tmp_path, change):
    async with running(tmp_path) as env:
        plan_hash, revision = prepare(env)
        approve(env, plan_hash, revision)
        broker, entry, lease = env.broker, env.broker.local_home_diagnostic, env.broker.session
        plan = broker._store().plan(plan_hash)
        calls = 0

        def change_on_final_validation(*_):
            nonlocal calls
            calls += 1
            if calls == 2:
                if change == "expiry":
                    env.clock.now = entry.deadline + 1
                elif change == "lease":
                    broker.session = replace(lease, id="f" * 32)
                else:
                    broker.epoch = "f" * 32
            return True

        broker.principal_valid = change_on_final_validation
        with pytest.raises(BrokerError, match="local_home_diagnostic_no_longer_valid"):
            broker._take_local_home_probe(plan, lease, policy_authorized=False)
        assert entry.consumed is False and entry.probe is None
        assert env.port.opens == 0


async def test_final_approval_callback_expiry_does_not_install_local_consent(tmp_path, monkeypatch):
    async with running(tmp_path) as env:
        plan_hash, revision = prepare(env)
        original = env.broker._make_local_home_diagnostic

        def prepare_then_expire(plan, lease):
            entry = original(plan, lease)

            def expire_during_validation(*_):
                env.clock.now = entry.deadline + 1
                return True

            env.broker.principal_valid = expire_during_validation
            return entry

        monkeypatch.setattr(env.broker, "_make_local_home_diagnostic", prepare_then_expire)
        with pytest.raises(BrokerError):
            env.approvals.approve(plan_hash, revision, ATTESTATION, True)
        assert env.broker.local_home_diagnostic is None
        assert env.broker._store().db.execute("SELECT * FROM approvals").fetchall() == []
        assert env.port.opens == 0


async def test_retained_failure_drops_serial_trace_frames_and_reader_buffers(monkeypatch):
    wire = b"UNKNOWN HOME_READY private=SYNTHETIC_TRACE_SECRET\n"
    backend, port, plan = device(clock_step=0.01, home=wire)
    probe = HomeLineProbe(lambda: 10.0, 130.0)
    readers = []
    original = serial_backend.Reader

    def reader(*args, **kwargs):
        result = original(*args, **kwargs)
        readers.append(result)
        return result

    monkeypatch.setattr(serial_backend, "Reader", reader)
    with pytest.raises(SerialFailure, match="serial_capture_timeout") as failure:
        await backend.execute_with_local_probe(plan, lambda _: None, probe)
    trace = failure.value.__traceback__
    names = []
    while trace is not None:
        names.append(trace.tb_frame.f_code.co_name)
        trace = trace.tb_next
    assert not {"read_once", "command", "capture_home", "_execute_sync"} & set(names)
    assert failure.value.__context__ is None and failure.value.__cause__ is None
    assert readers[0].lines == [] and readers[0].pending == readers[0].fault_tail == b""
    assert readers[0].local_home_probe is None
    probe.finish()
    assert "SYNTHETIC_TRACE_SECRET" in probe.preview()["escaped_line"]
    probe.clear()
    assert "escaped_line" not in probe.preview()
    assert port.writes == [b"\x1b"] and port.closes == 1


async def test_blocked_close_keeps_ownership_but_drops_reader_copies_before_expiry(monkeypatch):
    backend, port, plan = device(clock_step=0.01, home=b"HOME_READY SYNTHETIC_CLOSE_SECRET\n")
    now = [10.0]
    probe = HomeLineProbe(lambda: now[0], 130.0)
    readers = []
    original = serial_backend.Reader

    def reader(*args, **kwargs):
        result = original(*args, **kwargs)
        readers.append(result)
        return result

    monkeypatch.setattr(serial_backend, "Reader", reader)
    port.close_release = threading.Event()
    task = asyncio.create_task(backend.execute_with_local_probe(plan, lambda _: None, probe))
    try:
        assert await asyncio.to_thread(port.close_started.wait, 5)
        assert backend.active == 1 and not task.done() and port.is_open
        assert readers[0].lines == [] and readers[0].pending == readers[0].fault_tail == b""
        assert readers[0].local_home_probe is None
        assert probe.metadata()["state"] == "capturing"
        now[0] = 131.0
        probe.prune()
        assert probe.metadata()["state"] == "expired"
        assert "escaped_line" not in probe.preview()
        assert backend.active == 1 and not task.done() and port.is_open
    finally:
        port.close_release.set()
    with pytest.raises(SerialFailure, match="serial_capture_timeout"):
        await task
    assert backend.active == 0 and not port.is_open
