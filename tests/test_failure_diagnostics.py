"""Synthetic diagnostic regression tests; never open actual serial hardware."""

import json
import sqlite3

import pytest
from test_serial_backend import backend, config

from cardputer_mcp.backend import DeviceDisconnected, DeviceOperationFailure
from cardputer_mcp.broker import Broker
from cardputer_mcp.config import Settings
from cardputer_mcp.models import AcquireRequest, ExecuteRequest, LogRequest, Origin, PrepareRequest
from cardputer_mcp.models import TestName as Action
from cardputer_mcp.store import Store


@pytest.mark.parametrize("failure_kind", ["silence", "noise", "read_error", "write_error"])
async def test_failure_code_and_safe_counts_persist_without_replay(tmp_path, failure_kind):
    reply = b"UNKNOWN_PREFIX secret=SYNTHETIC_ONLY HOME_READY\n" if failure_kind == "noise" else b""
    device, port = backend(transform=lambda key, data: reply)
    if failure_kind == "read_error":
        port.disconnect = True
    if failure_kind == "write_error":

        def broken_write(data):
            raise OSError("SYNTHETIC_WRITE_SECRET")

        port.write = broken_write
    broker = Broker(
        Settings(backend="serial", serial=config(), state_dir=tmp_path),
        backend=device,
        serial_enabled=True,
        lock_path=tmp_path / "device.lockfile",
        principal_valid=lambda *_: True,
    )
    await broker.start()
    try:
        token = broker.acquire(AcquireRequest(owner=Origin.LOCAL), principal_id="synthetic-client")[
            "session"
        ]
        plan = broker.prepare(
            PrepareRequest(session=token, job_id="failure-diagnostics", test=Action.HELLO)
        )
        broker.execute(ExecuteRequest(session=token, plan_hash=plan["plan_hash"]))
        await broker.queue.join()
        result = broker.job("failure-diagnostics")
        expected = {"read_error": "serial_read_failed", "write_error": "serial_write_failed"}.get(
            failure_kind, "serial_marker_timeout"
        )
        assert result["result"] == expected
        assert result["result"] != "device_disconnected_no_retry"
        # Review classification fix: a typed serial failure with a confirmed close
        # is `failed` (was needs_review); code and safe counts are unchanged.
        assert result["state"] == "failed" and not result["reviewed"]
        assert result["stage"] == "serial_close"
        details = result["diagnostic"]
        assert details["code"] == expected
        assert details["step"] == "initial_home"
        assert details["expected_marker"] == "HOME_READY"
        assert details["recognized_markers"] == 0
        assert details["received_bytes"] == (len(reply) if failure_kind == "noise" else 0)
        assert details["received_lines"] == (1 if failure_kind == "noise" else 0)
        assert port.opens == port.closes == 1
        broker.prepare(PrepareRequest(session=token, job_id="fresh-request", test=Action.HELLO))
        assert broker.job("failure-diagnostics") == result
        assert port.opens == port.closes == 1
        persisted = "\n".join(broker._store().db.iterdump())
        visible = json.dumps(broker.read_logs(LogRequest()))
        for secret in ("SYNTHETIC_ONLY", "SYNTHETIC_WRITE_SECRET", token):
            assert secret not in persisted + visible
    finally:
        await broker.close()


def test_unknown_diagnostic_strings_fail_closed():
    failure = DeviceOperationFailure(
        "private-url-SYNTHETIC",
        step="private-step-SYNTHETIC",
        expected_marker="SYNTHETIC_TOKEN",
        received_bytes=-1,
        received_lines=10**12,
        recognized_markers=-5,
    )
    assert not isinstance(failure, DeviceDisconnected)
    rendered = json.dumps(failure.diagnostic)
    assert "SYNTHETIC" not in rendered
    assert failure.code == "serial_operation_uncertain"
    assert failure.diagnostic["step"] == "operation"
    assert failure.diagnostic["expected_marker"] is None
    assert failure.diagnostic["received_bytes"] == 0
    assert failure.diagnostic["received_lines"] == 65536
    assert failure.diagnostic["recognized_markers"] == 0


def test_old_history_gets_nullable_diagnostic_without_clearing_review(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    database = sqlite3.connect(path)
    database.execute("""CREATE TABLE jobs (
        id TEXT PRIMARY KEY, plan_hash TEXT NOT NULL, state TEXT NOT NULL,
        stage TEXT, result TEXT, reviewed INTEGER NOT NULL DEFAULT 0,
        created TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, finished TEXT)""")
    database.execute("""INSERT INTO jobs(id,plan_hash,state,result) VALUES
        ('old-job','synthetic-hash','needs_review','device_disconnected_no_retry')""")
    database.commit()
    database.close()
    store = Store(path)
    try:
        result = store.job("old-job")
        assert result["diagnostic"] is None
        assert result["result"] == "device_disconnected_no_retry"
        assert result["state"] == "needs_review" and store.needs_review()
        assert not result["reviewed"]
    finally:
        store.close()
