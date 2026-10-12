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
    clean_line,
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


@pytest.mark.parametrize(
    "line",
    [
        b"I (0) shell: HOME_READY\r",
        b"I (123456789) shell: HOME_READY",
        b"\x1b[0;32mI (123) shell: HOME_READY\x1b[0m\r",
        b"\x1b[0m\x1b[0;32mI (123) shell: HOME_READY\x1b[0m",
        b"HOME_READY\r",
    ],
)
def test_diagnostics_review_accepts_ordinary_esp_log_prefix(line):
    assert clean_line(line) == b"HOME_READY"


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


@pytest.mark.parametrize(
    "reply",
    [
        b"I(123) shell: HOME_READY\n",  # Missing the standard ESP-IDF space.
        b"2026-01-01 I (123) shell: HOME_READY\n",
        b"I (123) shell: HOME_READY suffix=INVENTED_SECRET\n",
        b"UNKNOWN_PREFIX HOME_READY token=INVENTED_SECRET\n",
    ],
)
def test_diagnostics_review_unknown_prefix_counts_without_recognizing_or_emitting(reply):
    reader, port, events = make_reader(reply, split=3)
    with pytest.raises(SerialFailure, match="serial_marker_timeout") as failure:
        reader.command(b"q", rb"HOME_READY", timeout=2)
    assert reader.bytes_read == len(reply) > 0
    assert len(reader.lines) == 1
    assert events == ["serial_wait_command"]
    assert port.writes == [b"q"]
    diagnostic = dict(failure.value.diagnostic)
    summary = diagnostic.pop("receive_summary")
    assert summary["last_command"] == "home_quit"
    assert summary["command_write_completed"] is True
    assert summary["before_command"]["bytes"] == 0
    assert summary["after_command"]["bytes"] == len(reply)
    assert summary["after_command"]["home_ready_token_lines"] == 1
    assert summary["after_command"]["home_ready_exact_lines"] == 0
    assert diagnostic == {
        "code": "serial_marker_timeout",
        "step": "command",
        "expected_marker": "HOME_READY",
        "received_bytes": len(reply),
        "received_lines": 1,
        "recognized_markers": 0,
    }
    public = json.dumps(failure.value.diagnostic)
    assert "INVENTED_SECRET" not in public
    assert "shell:" not in public
    assert "UNKNOWN_PREFIX" not in public


def test_diagnostics_review_silence_has_zero_receive_counts_and_stops_at_first_q():
    reader, port, events = make_reader()
    with pytest.raises(SerialFailure, match="serial_marker_timeout") as failure:
        hello_cycle(reader)
    assert port.writes == [b"q"]
    assert reader.bytes_read == len(reader.lines) == 0
    assert events == ["serial_wait_initial_home"]
    diagnostic = dict(failure.value.diagnostic)
    summary = diagnostic.pop("receive_summary")
    assert summary["last_command"] == "home_quit"
    assert summary["command_write_completed"] is True
    assert summary["before_command"]["bytes"] == 0
    assert summary["after_command"]["bytes"] == 0
    assert diagnostic == {
        "code": "serial_marker_timeout",
        "step": "initial_home",
        "expected_marker": "HOME_READY",
        "received_bytes": 0,
        "received_lines": 0,
        "recognized_markers": 0,
    }
    assert 12 <= reader.clock() < 12.1


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


@pytest.mark.parametrize("reply", [b"", b"NOISY_PREFIX token=INVENTED_SECRET\r\n"])
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
    )
    await broker.start()
    try:
        token = broker.acquire(AcquireRequest(owner=Origin.LOCAL))["session"]
        plan = broker.prepare(
            PrepareRequest(session=token, job_id="diagnostics-review", test=DeviceTest.HELLO)
        )
        broker._store().approve(plan["plan_hash"])
        broker.execute(ExecuteRequest(session=token, plan_hash=plan["plan_hash"]))
        await asyncio.wait_for(broker.queue.join(), timeout=2)
        job = broker.job("diagnostics-review")
        assert job["state"] == "needs_review"
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
        assert broker.status()["needs_review"] is True
        stored = "\n".join(broker._store().db.iterdump())
        public = json.dumps([job, broker.read_logs(LogRequest())])
        assert "INVENTED_SECRET" not in stored + public
        assert "NOISY_PREFIX" not in stored + public
    finally:
        await broker.close()
