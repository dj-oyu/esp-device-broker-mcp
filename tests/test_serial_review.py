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
from cardputer_mcp.serial_backend import Reader, SerialBackend, SerialFailure


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
