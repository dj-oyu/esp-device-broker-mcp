"""Firmware/log observation uses invented bytes and fake serial ports only."""

import asyncio
import json
import threading
from collections import deque

import pytest

from cardputer_mcp import serial_backend as serial_module
from cardputer_mcp.backend import BackendResult
from cardputer_mcp.config import SerialSettings
from cardputer_mcp.device_observation import DeviceLogCollector
from cardputer_mcp.models import TestName as Action
from cardputer_mcp.ports import DeviceSelector, PortInfo
from cardputer_mcp.serial_backend import SerialBackend, SerialFailure

STARTUP = (
    b"I (100) app_init: Application information:\r\n"
    b"\x1b[0;32mI (101) app_init: Project name:     synthetic-pocketjs\x1b[0m\r\n"
    b"I (102) app_init: App version:      v0.0.7-test\r\n"
    b"I (103) app_init: Compile time:     Oct  3 2026 01:02:03\r\n"
    b"I (104) app_init: ELF file SHA256:  abcdef0123456789...\r\n"
    b"I (105) app_init: ESP-IDF:          v5.5-test\r\n"
)


class Clock:
    def __init__(self, step=0.01):
        self.now = 0.0
        self.step = step

    def __call__(self):
        return self.now


class FakePort:
    def __init__(
        self, clock, *, incoming=(), open_error=False, close_error=False, still_open=False
    ):
        self.clock = clock
        self.chunks = deque(incoming)
        self.open_error = open_error
        self.close_error = close_error
        self.still_open = still_open
        self.port = None
        self.dtr = self.rts = True
        self.is_open = False
        self.opens = self.closes = 0
        self.writes = []
        self.read_starts = []
        self.read_sizes = []
        self.returned_bytes = 0
        self.owner_threads = set()
        self.close_started = threading.Event()
        self.close_release = None

    def open(self):
        assert self.dtr is False and self.rts is False
        assert not self.is_open
        self.owner_threads.add(threading.get_ident())
        self.opens += 1
        if self.open_error:
            raise OSError("SYNTHETIC_PRIVATE_OPEN_ERROR")
        self.is_open = True

    def close(self):
        self.owner_threads.add(threading.get_ident())
        self.closes += 1
        self.close_started.set()
        if self.close_release is not None:
            assert self.close_release.wait(timeout=5)
        if self.close_error:
            raise OSError("SYNTHETIC_PRIVATE_CLOSE_ERROR")
        if not self.still_open:
            self.is_open = False

    def read(self, size=1):
        self.owner_threads.add(threading.get_ident())
        self.read_starts.append(self.clock.now)
        self.read_sizes.append(size)
        self.clock.now += self.clock.step
        if not self.chunks:
            return b""
        chunk = self.chunks.popleft()
        if isinstance(chunk, Exception):
            raise chunk
        if len(chunk) > size:
            self.chunks.appendleft(chunk[size:])
        self.returned_bytes += len(chunk[:size])
        return chunk[:size]

    def write(self, data):
        self.writes.append(data)
        raise AssertionError("Firmware observation must never send serial commands")


def device(*, clock_step=0.01, **kwargs):
    clock = Clock(clock_step)
    port = FakePort(clock, **kwargs)
    config = SerialSettings(
        selector=DeviceSelector(vid=1234, pid=5678, serial_number="SYNTHETIC-OBSERVATION"),
        observe_seconds=0.1,
    )
    backend = SerialBackend(
        config,
        inventory=lambda: [
            PortInfo(port="FAKE_PORT", vid=1234, pid=5678, serial_number="SYNTHETIC-OBSERVATION")
        ],
        factory=lambda: port,
        clock=clock,
    )
    plan = {"test": Action.FIRMWARE.value, **backend.prepare(Action.FIRMWARE)}
    return backend, port, plan


def collected(wire):
    collector = DeviceLogCollector()
    if wire:
        collector.feed(wire)
    return collector.finish()


def test_prepare_discloses_passive_bounds_and_sharing_without_open():
    backend, port, plan = device()
    assert plan["protocol"] == "esp-idf-startup-log-v1"
    assert plan["firmware_verification"] == "unverified_device_report"
    assert plan["expected_firmware_sha256"] is None
    assert plan["observe_seconds"] == 30
    assert plan["stages"] == ["serial_open", "firmware_observe", "serial_close"]
    effects = " ".join(plan["effects"])
    for phrase in (
        "glitch control lines",
        "30 seconds",
        "64 KiB",
        "no commands",
        "no automatic reopen",
        "authenticated recipient",
        "redaction is not guaranteed complete",
        "5 minutes",
        "unverified",
    ):
        assert phrase in effects
    assert backend.config.expected_firmware_sha256 is None
    assert port.opens == port.closes == 0 and port.writes == []


@pytest.mark.parametrize("split", [1, 2, 7, 64, 4096])
async def test_fragmented_startup_uses_one_owner_no_commands_and_confirmed_close(split):
    wire = STARTUP + b"ordinary console output\n"
    backend, port, plan = device(incoming=[wire[n : n + split] for n in range(0, len(wire), split)])
    events = []
    result = await backend.execute(plan, events.append)
    assert isinstance(result, BackendResult)
    assert result.code == "firmware_observation_completed_no_commands"
    assert result.image is None
    assert result.observation is not None
    expected = collected(wire)
    assert result.observation.payload() == expected.payload()
    assert result.observation.metadata() == expected.metadata()
    assert result.observation.sha256 == expected.sha256
    firmware = result.observation.metadata()["firmware"]
    assert firmware["status"] == "observed"
    assert firmware["project_name"] == "synthetic-pocketjs"
    assert firmware["app_version"] == "v0.0.7-test"
    assert firmware["elf_sha256_prefix"] == "abcdef0123456789"
    assert firmware["source_verified"] is False and firmware["binary_sha256_verified"] is False
    assert port.opens == port.closes == 1 and port.writes == []
    assert not port.is_open and not backend.hardware_connected
    assert backend.active == 0 and backend.max_active == 1
    assert len(port.owner_threads) == 1
    assert set(port.read_sizes) == {4096}
    assert events == ["serial_open", "firmware_observe", "serial_close"]
    assert "synthetic-pocketjs" not in json.dumps(events)


async def test_zero_bytes_returns_an_explicit_empty_observation_at_fixed_deadline():
    backend, port, plan = device(clock_step=1)
    result = await backend.execute(plan, lambda _: None)
    assert isinstance(result, BackendResult) and result.observation is not None
    assert result.observation.payload() == collected(b"").payload()
    assert result.observation.metadata() == collected(b"").metadata()
    assert result.observation.metadata()["bytes_received"] == 0
    assert result.observation.metadata()["firmware"]["status"] == "unavailable"
    assert result.observation.metadata()["firmware"]["app_version"] is None
    assert port.clock.now == 30
    assert port.read_starts == list(range(30))
    assert port.writes == [] and port.opens == port.closes == 1


async def test_console_faults_and_markers_do_not_use_legacy_reader(monkeypatch):
    def forbidden_reader(*args, **kwargs):
        raise AssertionError("Firmware observation must not use the legacy marker reader")

    monkeypatch.setattr(serial_module, "Reader", forbidden_reader)
    wire = (
        b"Guru Meditation Error: synthetic panic\nHOME_READY\n"
        b"APP_ID local.synthetic\nAPP_ERROR synthetic\n"
        b"Authorization: Bearer SYNTHETIC_PRIVATE_TOKEN\n"
    )
    backend, port, plan = device(incoming=[wire])
    events = []
    result = await backend.execute(plan, events.append)
    assert isinstance(result, BackendResult) and result.observation is not None
    payload = result.observation.payload()
    assert payload == collected(wire).payload()
    assert "Guru Meditation" in json.dumps(payload)
    assert "SYNTHETIC_PRIVATE_TOKEN" not in json.dumps(payload)
    assert events == ["serial_open", "firmware_observe", "serial_close"]
    assert port.writes == []


async def test_receive_limit_finishes_early_without_an_extra_read_or_reopen():
    wire = (b"password=" + b"x" * 1014 + b"\n") * 64
    backend, port, plan = device(incoming=[wire, b"AFTER_LIMIT_NOT_READ\n"])
    result = await backend.execute(plan, lambda _: None)
    assert isinstance(result, BackendResult) and result.observation is not None
    assert result.observation.payload() == collected(wire).payload()
    assert result.observation.metadata() == collected(wire).metadata()
    assert result.observation.metadata()["bytes_received"] == 65536
    assert result.observation.metadata()["stop_reason"] == "receive_limit"
    assert result.observation.metadata()["truncated"] is True
    assert len(port.read_starts) == 16
    assert port.returned_bytes == 65536
    assert port.clock.now < 30
    assert list(port.chunks) == [b"AFTER_LIMIT_NOT_READ\n"]
    assert port.writes == [] and port.opens == port.closes == 1


async def test_fragmented_receive_budget_limits_last_read_to_one_wire_byte():
    wire = (b"password=" + b"x" * 1014 + b"\n") * 64
    after_limit = b"SYNTHETIC_PRIVATE_AFTER_LIMIT" * 200
    backend, port, plan = device(incoming=[wire[:-1], wire[-1:] + after_limit])
    result = await backend.execute(plan, lambda _: None)
    assert isinstance(result, BackendResult) and result.observation is not None
    assert result.observation.payload() == collected(wire).payload()
    assert result.observation.metadata()["bytes_received"] == 65536
    assert result.observation.metadata()["stop_reason"] == "receive_limit"
    assert port.returned_bytes == 65536
    assert port.read_sizes == [4096] * 16 + [1]
    assert list(port.chunks) == [after_limit]
    assert port.clock.now < 30
    assert port.writes == [] and port.opens == port.closes == 1


@pytest.mark.parametrize("read_seconds", [0.01, 30])
async def test_oversized_driver_return_is_rejected_without_exposing_data(read_seconds):
    backend, port, plan = device(clock_step=read_seconds, incoming=[b"x" * 4096])
    original_read = port.read

    def oversized_read(size):
        return original_read(size) + b"SYNTHETIC_PRIVATE_OVERSIZED_RETURN"

    port.read = oversized_read
    events = []
    with pytest.raises(SerialFailure, match="^serial_read_failed$") as error:
        await backend.execute(plan, events.append)
    assert error.value.diagnostic["step"] == "firmware_observe"
    assert "SYNTHETIC_PRIVATE" not in repr(error.value) + json.dumps(error.value.diagnostic)
    assert not hasattr(error.value, "observation")
    assert port.read_sizes == [4096]
    assert port.writes == [] and port.opens == port.closes == 1
    assert backend.active == 0 and not backend.hardware_connected
    assert events == ["serial_open", "firmware_observe", "serial_close"]


@pytest.mark.parametrize("read_seconds", [30, 31])
async def test_read_returned_at_or_after_deadline_is_discarded(read_seconds):
    backend, port, plan = device(clock_step=read_seconds, incoming=[STARTUP, b"NOT_READ\n"])
    result = await backend.execute(plan, lambda _: None)
    assert isinstance(result, BackendResult) and result.observation is not None
    assert result.observation.payload() == collected(b"").payload()
    assert len(port.read_starts) == 1 and port.read_starts == [0]
    assert list(port.chunks) == [b"NOT_READ\n"]
    assert port.writes == [] and port.closes == 1


@pytest.mark.parametrize("change", [{"observe_seconds": 31}, {"protocol": "synthetic-other"}])
async def test_changed_plan_never_opens_port(change):
    backend, port, plan = device()
    plan.update(change)
    with pytest.raises(SerialFailure, match="^device_or_plan_changed$"):
        await backend.execute(plan, lambda _: None)
    assert port.opens == port.closes == 0 and port.writes == []
    assert backend.active == 0


@pytest.mark.parametrize(
    ("kwargs", "code", "step"),
    [
        ({"open_error": True}, "serial_open_failed", "open_serial"),
        (
            {"incoming": [STARTUP, OSError("SYNTHETIC_PRIVATE_READ_ERROR")]},
            "serial_read_failed",
            "firmware_observe",
        ),
    ],
)
async def test_errors_expose_only_fixed_diagnostics_and_no_report(kwargs, code, step):
    backend, port, plan = device(**kwargs)
    events = []
    with pytest.raises(SerialFailure, match=f"^{code}$") as error:
        await backend.execute(plan, events.append)
    public = json.dumps(error.value.diagnostic) + repr(error.value) + json.dumps(events)
    assert "SYNTHETIC_PRIVATE" not in public and "synthetic-pocketjs" not in public
    assert error.value.diagnostic["step"] == step
    assert not hasattr(error.value, "observation")
    assert error.value.__context__ is None and error.value.__cause__ is None
    assert port.opens == port.closes == 1 and port.writes == []
    assert backend.active == 0 and not backend.hardware_connected


async def test_collector_failure_is_sanitized_before_close_waits(monkeypatch):
    import weakref

    collectors = []

    class FailingCollector:
        def __init__(self):
            self.raw = b""
            collectors.append(weakref.ref(self))

        def feed(self, incoming):
            self.raw = incoming
            raise RuntimeError("SYNTHETIC_PRIVATE_COLLECTOR_ERROR")

    monkeypatch.setattr(serial_module, "DeviceLogCollector", FailingCollector)
    backend, port, plan = device(incoming=[STARTUP])
    port.close_release = threading.Event()
    task = asyncio.create_task(backend.execute(plan, lambda _: None))
    try:
        assert await asyncio.to_thread(port.close_started.wait, 5)
        assert not task.done() and backend.active == 1
        assert len(collectors) == 1 and collectors[0]() is None
    finally:
        port.close_release.set()
    with pytest.raises(SerialFailure, match="^serial_operation_uncertain$") as error:
        await task
    assert error.value.diagnostic["step"] == "firmware_observe"
    assert "SYNTHETIC_PRIVATE" not in repr(error.value)
    assert error.value.__context__ is None and error.value.__cause__ is None
    assert port.closes == 1 and port.writes == []


@pytest.mark.parametrize("kwargs", [{"close_error": True}, {"still_open": True}])
async def test_unconfirmed_close_withholds_report_and_keeps_ownership(kwargs):
    backend, port, plan = device(clock_step=1, incoming=[STARTUP], **kwargs)
    events = []
    with pytest.raises(SerialFailure, match="^serial_close_unconfirmed$") as error:
        await backend.execute(plan, events.append)
    assert "SYNTHETIC_PRIVATE" not in str(error.value)
    assert "synthetic-pocketjs" not in json.dumps(error.value.diagnostic)
    assert backend.ownership_uncertain and backend.hardware_connected
    assert backend.active == 1 and port.closes == 1
    assert events[-1] == "serial_close_unconfirmed"
    with pytest.raises(SerialFailure, match="^multiple_device_owners$"):
        await backend.execute(plan, lambda _: None)
    assert port.opens == port.closes == 1 and port.writes == []


async def test_report_and_single_owner_are_retained_until_close_finishes():
    backend, port, plan = device(clock_step=1, incoming=[STARTUP])
    port.close_release = threading.Event()
    task = asyncio.create_task(backend.execute(plan, lambda _: None))
    try:
        assert await asyncio.to_thread(port.close_started.wait, 5)
        assert not task.done() and backend.active == 1 and backend.hardware_connected
        with pytest.raises(SerialFailure, match="^multiple_device_owners$"):
            await backend.execute(plan, lambda _: None)
        assert port.opens == 1
    finally:
        port.close_release.set()
    result = await task
    assert isinstance(result, BackendResult) and result.observation is not None
    assert backend.active == 0 and not backend.hardware_connected


async def test_cancellation_waits_for_close_and_withholds_report():
    backend, port, plan = device(clock_step=1, incoming=[STARTUP])
    port.close_release = threading.Event()
    task = asyncio.create_task(backend.execute(plan, lambda _: None))
    try:
        assert await asyncio.to_thread(port.close_started.wait, 5)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done() and backend.active == 1 and backend.hardware_connected
    finally:
        port.close_release.set()
    with pytest.raises(SerialFailure, match="^operation_interrupted_review_required$") as error:
        await task
    assert not hasattr(error.value, "observation")
    assert backend.active == 0 and not port.is_open
    assert port.opens == port.closes == 1 and port.writes == []
