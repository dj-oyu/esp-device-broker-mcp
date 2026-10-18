"""Independent serial safety review. Synthetic serial objects only; no USB access."""

import asyncio
import json
import threading
from collections import deque
from pathlib import Path

import pytest

from cardputer_mcp.broker import Broker
from cardputer_mcp.config import SerialSettings, Settings
from cardputer_mcp.models import AcquireRequest, ExecuteRequest, LogRequest, Origin, PrepareRequest
from cardputer_mcp.models import TestName as DeviceTest
from cardputer_mcp.ports import DeviceSelector, PortInfo
from cardputer_mcp.serial_backend import KNOWN, Reader, SerialBackend, SerialFailure, hello_cycle


class SyntheticClock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


class SyntheticPort:
    def __init__(self, clock, responses=(), preloaded=()):
        self.clock = clock
        self.responses = deque(responses)
        self.incoming = deque(preloaded)
        self.writes = []
        self.is_open = False
        self.dtr = self.rts = True
        self.port = None
        self.open_count = self.close_count = 0
        self.reader_threads = set()

    def open(self):
        assert self.dtr is False and self.rts is False
        self.open_count += 1
        self.is_open = True

    def close(self):
        self.close_count += 1
        self.is_open = False

    def write(self, data):
        self.writes.append(data)
        if self.responses:
            self.incoming.extend(self.responses.popleft())
        return len(data)

    def read(self, size=1):
        self.reader_threads.add(threading.get_ident())
        self.clock.now += 0.01
        if not self.incoming:
            return b""
        chunk = self.incoming.popleft()
        if len(chunk) > size:
            self.incoming.appendleft(chunk[size:])
        return chunk[:size]


def reader_for(port):
    events = []
    return Reader(port, events.append, port.clock, port.clock() + 90), events


def hello_responses(startup=None, teardown=None):
    return (
        [[b"HOME_READY\n"], [b"CATEGORY 0\n"]]
        + [[b"APP 0\n"]] * 14
        + [
            startup or [b"APP_ID local.hello\nHELLO_READY\nHELLO_FRAME_PRESENTED\n"],
            [b"HELLO_COUNT 1\n"],
            teardown or [b"MEM free=100 largest=90 js=0 frames=1\nAPP_STOPPED\nHOME_READY\n"],
        ]
    )


def synthetic_identity(location="synthetic-location"):
    return PortInfo(
        port="SYNTHETIC_PORT", vid=123, pid=456, serial_number="synthetic-serial", location=location
    )


def synthetic_config():
    return SerialSettings(
        selector=DeviceSelector(vid=123, pid=456, serial_number="synthetic-serial"),
        expected_firmware_sha256="a" * 64,
        observe_seconds=0.1,
    )


def prepared(backend, test=DeviceTest.OBSERVE):
    return {"test": test.value, **backend.prepare(test)}


def test_review_serial_fragmented_coalesced_and_log_allowlist():
    chunks = [
        b"secret=DO_NOT_PERSIST\n\x1b[3",
        b"2mI (123) app: APP_ID local.",
        b"hello\x1b[0m\r\nHELLO_RE",
        b"ADY\nHELLO_FRAME_PRESENTED\n",
    ]
    port = SyntheticPort(SyntheticClock(), responses=[chunks])
    reader, events = reader_for(port)
    lines = reader.command(b"e", rb"HELLO_FRAME_PRESENTED")
    assert lines[-3:] == [b"APP_ID local.hello", b"HELLO_READY", b"HELLO_FRAME_PRESENTED"]
    assert events == [
        "serial_wait_command",
        "serial_identity_hello",
        "serial_hello_ready",
        "serial_frame_presented",
    ]
    assert set(events) <= set(KNOWN.values()) | {"serial_wait_command"}
    assert all("DO_NOT_PERSIST" not in event for event in events)
    assert len(port.reader_threads) == 1


def test_review_serial_cached_coalesced_ack_cannot_satisfy_next_command():
    port = SyntheticPort(SyntheticClock(), responses=[[b"CATEGORY 0\nHOME_READY\n"], []])
    reader, _ = reader_for(port)
    reader.command(b"a", rb"CATEGORY 0")
    with pytest.raises(SerialFailure, match="serial_marker_timeout"):
        reader.command(b"q", rb"HOME_READY", timeout=0.1)


def test_review_serial_unread_stale_ack_cannot_satisfy_new_command():
    port = SyntheticPort(SyntheticClock(), preloaded=[b"HOME_READY\n"])
    reader, _ = reader_for(port)
    with pytest.raises(SerialFailure):
        reader.command(b"q", rb"HOME_READY", timeout=0.1)


def test_review_serial_partial_stale_ack_cannot_satisfy_new_command():
    port = SyntheticPort(SyntheticClock(), responses=[[b"_READY\n"]], preloaded=[b"HOME"])
    reader, _ = reader_for(port)
    reader.read_once()
    with pytest.raises(SerialFailure, match="serial_marker_timeout"):
        reader.command(b"q", rb"HOME_READY", timeout=0.1)


def test_review_serial_valid_complete_hello_cycle_uses_only_expected_keys():
    port = SyntheticPort(SyntheticClock(), responses=hello_responses())
    reader, _ = reader_for(port)
    hello_cycle(reader)
    assert port.writes == [b"q", b"a"] + [b"u"] * 14 + [b"e", b"e", b"q"]


def test_review_serial_rejects_ready_before_identity():
    startup = [b"HELLO_READY\nAPP_ID local.hello\nHELLO_FRAME_PRESENTED\n"]
    port = SyntheticPort(SyntheticClock(), responses=hello_responses(startup=startup))
    reader, _ = reader_for(port)
    with pytest.raises(SerialFailure):
        hello_cycle(reader)


@pytest.mark.parametrize(
    "startup",
    [
        [b"APP_ID local.other\nHELLO_READY\nHELLO_FRAME_PRESENTED\n"],
        [b"APP_ID local.hello\nHELLO_FRAME_PRESENTED\n"],
        [b"HELLO_FRAME_PRESENTED\nAPP_ID local.hello\nHELLO_READY\n"],
    ],
)
def test_review_serial_rejects_bad_startup(startup):
    port = SyntheticPort(SyntheticClock(), responses=hello_responses(startup=startup))
    reader, _ = reader_for(port)
    with pytest.raises(SerialFailure):
        hello_cycle(reader)
    assert port.writes.count(b"e") == 1


@pytest.mark.parametrize(
    "teardown",
    [
        [b"APP_STOPPED\nMEM free=100 largest=90 js=0 frames=1\nHOME_READY\n"],
        [b"MEM free=100 largest=90 js=1 frames=1\nAPP_STOPPED\nHOME_READY\n"],
        [b"MEM free=100 largest=90 js=0 frames=0\nAPP_STOPPED\nHOME_READY\n"],
        [b"APP_STOPPED\nHOME_READY\n"],
    ],
)
def test_review_serial_rejects_bad_teardown(teardown):
    port = SyntheticPort(SyntheticClock(), responses=hello_responses(teardown=teardown))
    reader, _ = reader_for(port)
    with pytest.raises(SerialFailure):
        hello_cycle(reader)


def test_review_serial_rejects_unsupported_keys_without_write():
    port = SyntheticPort(SyntheticClock())
    reader, _ = reader_for(port)
    for key in (b"d", b"reset", b"flash", b"", b"qq"):
        with pytest.raises(SerialFailure, match="command_not_allowlisted"):
            reader.command(key, rb"HOME_READY")
    assert port.writes == []


@pytest.mark.parametrize(
    "chunks,error",
    [
        ([b"A" * 16385], "serial_line_limit"),
        ([b"x\n" * 4097], "serial_marker_limit"),
        ([b"x" * 1023 + b"\n"] * 257, "serial_capture_limit"),
        ([b"Guru Medita", b"tion Error\n"], "serial_fault_marker"),
    ],
)
def test_review_serial_bounds_and_fragmented_faults(chunks, error):
    port = SyntheticPort(SyntheticClock(), preloaded=chunks)
    reader, _ = reader_for(port)
    with pytest.raises(SerialFailure, match=error):
        for _ in range(10000):
            reader.read_once()


async def test_review_serial_fingerprint_change_rejects_before_factory():
    identity = [synthetic_identity()]
    factory_calls = []

    def forbidden_factory():
        factory_calls.append(True)
        raise AssertionError("Synthetic factory must not be invoked")

    backend = SerialBackend(
        synthetic_config(), inventory=lambda: identity, factory=forbidden_factory
    )
    plan = prepared(backend)
    identity[0] = synthetic_identity("changed-synthetic-location")
    with pytest.raises(SerialFailure, match="device_or_plan_changed"):
        await backend.execute(plan, lambda _: None)
    assert factory_calls == []
    assert backend.active == 0


async def test_review_serial_fingerprint_change_after_open_closes_before_any_command():
    clock = SyntheticClock()
    port = SyntheticPort(clock)
    calls = 0

    def inventory():
        nonlocal calls
        calls += 1
        return [synthetic_identity("changed" if calls >= 3 else "synthetic-location")]

    backend = SerialBackend(
        synthetic_config(), inventory=inventory, factory=lambda: port, clock=clock
    )
    with pytest.raises(SerialFailure):
        await backend.execute(prepared(backend, DeviceTest.HELLO), lambda _: None)
    assert port.open_count == port.close_count == 1
    assert not port.is_open
    assert port.writes == []


async def test_review_serial_cancellation_keeps_owner_until_thread_and_close_finish():
    clock = SyntheticClock()
    reading, allow_read, closing, allow_close = [threading.Event() for _ in range(4)]

    class BlockingSyntheticPort(SyntheticPort):
        def read(self, size=1):
            reading.set()
            assert allow_read.wait(timeout=3)
            return super().read(size)

        def close(self):
            closing.set()
            assert allow_close.wait(timeout=3)
            super().close()

    port = BlockingSyntheticPort(clock)
    backend = SerialBackend(
        synthetic_config(),
        inventory=lambda: [synthetic_identity()],
        factory=lambda: port,
        clock=clock,
    )
    task = asyncio.create_task(backend.execute(prepared(backend), lambda _: None))
    try:
        assert await asyncio.to_thread(reading.wait, 2)
        task.cancel()
        await asyncio.sleep(0)
        assert backend.active == 1 and port.is_open and not task.done()
        allow_read.set()
        assert await asyncio.to_thread(closing.wait, 2)
        task.cancel()
        await asyncio.sleep(0)
        assert backend.active == 1 and port.is_open and not task.done()
        allow_close.set()
        with pytest.raises(SerialFailure, match="operation_interrupted_review_required"):
            await asyncio.wait_for(task, timeout=2)
        assert backend.active == 0 and not port.is_open
        assert port.close_count == 1
    finally:
        allow_read.set()
        allow_close.set()
        if not task.done():
            await asyncio.gather(task, return_exceptions=True)


async def test_review_serial_exception_and_raw_bytes_are_not_persisted(tmp_path: Path):
    clock = SyntheticClock()

    class FailingSyntheticPort(SyntheticPort):
        def read(self, size=1):
            if self.incoming:
                return super().read(size)
            raise RuntimeError("SYNTHETIC_EXCEPTION_SECRET")

    port = FailingSyntheticPort(clock, preloaded=[b"secret=SYNTHETIC_RAW_SECRET\nHOME_READY\n"])
    config = synthetic_config()
    backend = SerialBackend(
        config, inventory=lambda: [synthetic_identity()], factory=lambda: port, clock=clock
    )
    broker = Broker(
        Settings(backend="serial", serial=config, state_dir=tmp_path / "state"),
        backend=backend,
        serial_enabled=True,
        lock_path=tmp_path / "device.lock",
        principal_valid=lambda *_: True,
    )
    await broker.start()
    try:
        token = broker.acquire(AcquireRequest(owner=Origin.LOCAL), principal_id="synthetic-client")[
            "session"
        ]
        plan = broker.prepare(
            PrepareRequest(session=token, job_id="synthetic-serial", test=DeviceTest.OBSERVE)
        )
        broker.execute(ExecuteRequest(session=token, plan_hash=plan["plan_hash"]))
        await asyncio.wait_for(broker.queue.join(), timeout=2)
        persisted = "\n".join(broker._store().db.iterdump())
        visible = json.dumps([broker.status(), broker.read_logs(LogRequest())])
        assert broker.job("synthetic-serial")["state"] == "needs_review"
        for secret in ("SYNTHETIC_RAW_SECRET", "SYNTHETIC_EXCEPTION_SECRET"):
            assert secret not in persisted and secret not in visible
        assert not port.is_open and port.close_count == 1
    finally:
        await broker.close()


@pytest.mark.parametrize("raise_on_close", [False, True])
async def test_review_serial_unconfirmed_close_never_reports_disconnected(raise_on_close):
    clock = SyntheticClock()

    class UnclosedSyntheticPort(SyntheticPort):
        def close(self):
            self.close_count += 1
            if raise_on_close:
                raise RuntimeError("SYNTHETIC_CLOSE_SECRET")
            # A deliberately broken synthetic driver that fails to close.

    port = UnclosedSyntheticPort(clock)
    backend = SerialBackend(
        synthetic_config(),
        inventory=lambda: [synthetic_identity()],
        factory=lambda: port,
        clock=clock,
    )
    events = []
    try:
        await backend.execute(prepared(backend), events.append)
    except Exception:
        pass
    await asyncio.sleep(0)
    assert port.is_open
    assert backend.hardware_connected, "An unconfirmed close must not claim disconnection"
    assert "serial_close" not in events, "Only a confirmed close should emit serial_close"
    with pytest.raises(SerialFailure):
        await backend.execute(prepared(backend), events.append)
    assert port.open_count == 1


def test_review_serial_duplicate_coalesced_teardown_is_not_accepted():
    teardown = [
        b"MEM free=100 largest=90 js=0 frames=1\nAPP_STOPPED\nHOME_READY\n"
        b"MEM free=100 largest=90 js=0 frames=1\nAPP_STOPPED\nHOME_READY\n"
    ]
    port = SyntheticPort(SyntheticClock(), responses=hello_responses(teardown=teardown))
    reader, _ = reader_for(port)
    with pytest.raises(SerialFailure):
        hello_cycle(reader)


async def test_review_serial_worker_task_cancellation_cannot_release_live_port():
    clock = SyntheticClock()
    reading, allow_read, closed = (threading.Event() for _ in range(3))

    class BlockingSyntheticPort(SyntheticPort):
        def read(self, size=1):
            reading.set()
            assert allow_read.wait(timeout=3)
            return super().read(size)

        def close(self):
            super().close()
            closed.set()

    port = BlockingSyntheticPort(clock)
    backend = SerialBackend(
        synthetic_config(),
        inventory=lambda: [synthetic_identity()],
        factory=lambda: port,
        clock=clock,
    )
    existing = asyncio.all_tasks()
    execution = asyncio.create_task(backend.execute(prepared(backend), lambda _: None))
    try:
        assert await asyncio.to_thread(reading.wait, 2)
        # Model event-loop shutdown cancelling the child Task independently.
        workers = [
            task
            for task in asyncio.all_tasks() - existing
            if task is not execution and task.get_coro().__name__ == "to_thread"
        ]
        for worker in workers:
            worker.cancel()
        execution.cancel()
        for _ in range(5):
            await asyncio.sleep(0)
        assert port.is_open
        assert backend.active == 1, "A live serial thread must retain ownership"
        assert not execution.done()
    finally:
        allow_read.set()
        await asyncio.gather(execution, return_exceptions=True)
        assert await asyncio.to_thread(closed.wait, 2)
