"""Offline diagnostic review using invented bytes and a synthetic HOME shell."""

import asyncio
import json
from collections import deque

import pytest

from cardputer_mcp.backend import DeviceOperationFailure
from cardputer_mcp.broker import Broker
from cardputer_mcp.config import SerialSettings, Settings
from cardputer_mcp.models import AcquireRequest, ExecuteRequest, LogRequest, Origin, PrepareRequest
from cardputer_mcp.models import TestName as DeviceTest
from cardputer_mcp.ports import DeviceSelector, PortInfo
from cardputer_mcp.serial_backend import (
    Reader,
    SerialBackend,
    SerialFailure,
    hello_cycle,
)


class DiagnosticClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class DiagnosticPort:
    """Only supplies invented bytes. This is never a real serial connection."""

    def __init__(self, clock, reply=b"", *, split=4096, preloaded=()):
        self.clock = clock
        self.reply = reply
        self.split = split
        self.chunks = deque(preloaded)
        self.writes = []
        self.is_open = False
        self.dtr = self.rts = True
        self.port = None

    def open(self):
        self.is_open = True

    def close(self):
        self.is_open = False

    def read(self, size=1):
        self.clock.now += 0.01
        if not self.chunks:
            return b""
        chunk = self.chunks.popleft()
        if len(chunk) > size:
            self.chunks.appendleft(chunk[size:])
        return chunk[:size]

    def write(self, key):
        self.writes.append(key)
        assert key == b"q", "These fixtures must never progress beyond the first HOME command"
        self.chunks.extend(
            self.reply[index : index + self.split]
            for index in range(0, len(self.reply), self.split)
        )
        return len(key)


def make_reader(reply=b"", *, split=4096, preloaded=()):
    clock = DiagnosticClock()
    port = DiagnosticPort(clock, reply, split=split, preloaded=preloaded)
    events = []
    reader = Reader(port, events.append, clock, clock() + 90)
    return reader, port, events


@pytest.mark.parametrize("split", [1, 2, 7, 4096])
def test_diagnostics_review_initial_q_at_home_still_gets_fresh_ack(split):
    # Pinned source maps q -> KEY_BACK. shell_key(KEY_BACK) emits this marker
    # even when the ordinary shell is already on HOME; no reset is needed.
    reply = b"\x1b[0;32mI (456) shell: HOME_READY\x1b[0m\r\n"
    reader, port, events = make_reader(reply, split=split)
    assert reader.command(b"q", rb"HOME_READY") == [b"HOME_READY"]
    assert port.writes == [b"q"]
    assert events == ["serial_wait_command", "serial_home_ready"]
    assert reader.bytes_read == len(reply)
    assert len(reader.lines) == 1


def test_diagnostics_review_previous_home_marker_does_not_bypass_initial_q():
    reader, port, events = make_reader(preloaded=[b"I (100) shell: HOME_READY\r\n"])
    reader.read_once()
    with pytest.raises(SerialFailure, match="serial_marker_timeout"):
        hello_cycle(reader)
    assert port.writes == [b"q"]
    assert events == ["serial_home_ready", "serial_wait_initial_home"]
    assert len(reader.lines) == 1


def test_diagnostics_review_categories_cannot_contain_exception_secrets():
    failure = DeviceOperationFailure(
        "INVENTED_EXCEPTION_SECRET",
        step="INVENTED_STEP_SECRET",
        expected_marker="INVENTED_MARKER_SECRET",
        received_bytes=2**40,
        received_lines=-3,
        recognized_markers=2**40,
    )
    assert failure.diagnostic == {
        "code": "serial_operation_uncertain",
        "step": "operation",
        "expected_marker": None,
        "received_bytes": 1048576,
        "received_lines": 0,
        "recognized_markers": 65536,
    }
    assert "SECRET" not in json.dumps(failure.diagnostic)
    assert str(failure) == "serial_operation_uncertain"


@pytest.mark.parametrize(
    "reply",
    [
        b"",
        b"NOISY_PREFIX token=INVENTED_SECRET\r\n",
        b"I (1) shell: HOME_READY token=INVENTED_SECRET\r\n",
    ],
)
async def test_diagnostics_review_safe_timeout_detail_survives_backend_and_broker(tmp_path, reply):
    clock = DiagnosticClock()
    port = DiagnosticPort(clock, reply)
    config = SerialSettings(
        selector=DeviceSelector(vid=123, pid=456, serial_number="INVENTED_DEVICE"),
        expected_firmware_sha256="a" * 64,
    )
    identity = PortInfo(port="INVENTED_PORT", vid=123, pid=456, serial_number="INVENTED_DEVICE")
    backend = SerialBackend(config, inventory=lambda: [identity], factory=lambda: port, clock=clock)
    broker = Broker(
        Settings(backend="serial", serial=config, state_dir=tmp_path / "state"),
        backend=backend,
        serial_enabled=True,
        lock_path=tmp_path / "synthetic.lock",
        principal_valid=lambda *_: True,
    )
    await broker.start()
    try:
        token = broker.acquire(AcquireRequest(owner=Origin.LOCAL), principal_id="synthetic-client")[
            "session"
        ]
        plan = broker.prepare(
            PrepareRequest(session=token, job_id="diagnostics-review", test=DeviceTest.HELLO)
        )
        broker.execute(ExecuteRequest(session=token, plan_hash=plan["plan_hash"]))
        await asyncio.wait_for(broker.queue.join(), timeout=2)
        job = broker.job("diagnostics-review")
        # Review classification fix: a timeout with a confirmed close and no write is
        # `failed` (was needs_review); the result code and diagnostic are unchanged.
        assert job["state"] == "failed"
        assert job["result"] == "serial_marker_timeout"
        diagnostic = dict(job["diagnostic"])
        summary = diagnostic.pop("receive_summary")
        assert summary["last_command"] == "home_quit"
        assert summary["after_command"]["bytes"] == len(reply)
        assert summary["after_command"]["complete_lines"] == int(bool(reply))
        assert diagnostic == {
            "code": "serial_marker_timeout",
            "step": "initial_home",
            "expected_marker": "HOME_READY",
            "received_bytes": len(reply),
            "received_lines": int(bool(reply)),
            "recognized_markers": 0,
        }
        assert port.writes == [b"q"]
        assert not port.is_open
        assert broker.status()["needs_review"] is False  # failed, not a review
        stored = "\n".join(broker._store().db.iterdump())
        public = json.dumps([job, broker.read_logs(LogRequest())])
        if b"HOME_READY" in reply:
            assert summary["home_marker_shape"]["normalization"] == "other_surrounding"
            assert '"home_marker_shape"' in stored
        assert "INVENTED_SECRET" not in stored + public
        assert "NOISY_PREFIX" not in stored + public
    finally:
        await broker.close()
