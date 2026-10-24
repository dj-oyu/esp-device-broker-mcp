"""Capture integration uses invented bytes and fake ports only, never hardware."""

import asyncio
import threading
from collections import deque

import pytest

from cardputer_mcp.backend import BackendResult
from cardputer_mcp.capture import HEIGHT, WIDTH
from cardputer_mcp.config import SerialSettings
from cardputer_mcp.models import TestName as Action
from cardputer_mcp.ports import DeviceSelector, PortInfo
from cardputer_mcp.serial_backend import Reader, SerialBackend, SerialFailure


def frame() -> bytes:
    return (
        b"CAPTURE_BEGIN 240 135\n"
        + b"".join(f"PIX {y} ".encode() + b"f800" * WIDTH + b"\n" for y in range(HEIGHT))
        + b"CAPTURE_END\n"
    )


class Clock:
    def __init__(self, step=0.00001):
        self.now = 0.0
        self.step = step

    def __call__(self):
        return self.now


class FakePort:
    def __init__(
        self,
        clock,
        *,
        home=b"HOME_READY\n",
        shot=None,
        split=4096,
        preloaded=(),
        after_frame=(),
        close_error=False,
        partial_write=False,
    ):
        self.clock = clock
        self.home = home
        self.shot = frame() if shot is None else shot
        self.split = split
        self.chunks = deque(preloaded)
        self.after_frame = after_frame
        self.close_error = close_error
        self.partial_write = partial_write
        self.port = None
        self.dtr = self.rts = True
        self.is_open = False
        self.opens = self.closes = 0
        self.writes = []
        self.read_threads = set()
        self.close_started = threading.Event()
        self.close_release = None

    def open(self):
        assert not self.dtr and not self.rts
        assert not self.is_open
        self.opens += 1
        self.is_open = True

    def close(self):
        self.closes += 1
        self.close_started.set()
        if self.close_release is not None:
            assert self.close_release.wait(timeout=5)
        if self.close_error:
            raise OSError("SYNTHETIC_SECRET_CLOSE_ERROR")
        self.is_open = False

    def write(self, data):
        self.writes.append(data)
        if data == b"\x1b":
            reply = self.home
        elif data == b"s":
            if self.partial_write:
                return 0
            reply = self.shot
        else:
            raise AssertionError("Only one fixed Esc and one s are supported by this fixture")
        self.chunks.extend(reply[at : at + self.split] for at in range(0, len(reply), self.split))
        if data == b"s":
            self.chunks.extend(self.after_frame)
        return len(data)

    def read(self, size=1):
        self.clock.now += self.clock.step
        self.read_threads.add(threading.get_ident())
        if not self.chunks:
            return b""
        chunk = self.chunks.popleft()
        if isinstance(chunk, Exception):
            raise chunk
        if len(chunk) > size:
            self.chunks.appendleft(chunk[size:])
        return chunk[:size]


def device(*, clock_step=0.00001, **kwargs):
    clock = Clock(clock_step)
    port = FakePort(clock, **kwargs)
    config = SerialSettings(
        selector=DeviceSelector(vid=1234, pid=5678, serial_number="SYNTHETIC-CAPTURE"),
        expected_firmware_sha256="a" * 64,
    )
    backend = SerialBackend(
        config,
        inventory=lambda: [
            PortInfo(port="FAKE_PORT", vid=1234, pid=5678, serial_number="SYNTHETIC-CAPTURE")
        ],
        factory=lambda: port,
        clock=clock,
    )
    plan = {"test": Action.CAPTURE.value, **backend.prepare(Action.CAPTURE)}
    return backend, port, plan


@pytest.mark.parametrize("split", [7, 65536])
async def test_capture_uses_one_reader_one_escape_one_shot_and_confirmed_close(split):
    backend, port, plan = device(split=split)
    events = []
    result = await backend.execute(plan, events.append)
    assert isinstance(result, BackendResult)
    assert result.code == "render_buffer_captured_lcd_unverified"
    assert result.image.png.startswith(b"\x89PNG")
    assert result.image.metadata()["coherent_frame"] == "unknown"
    assert result.image.metadata()["lcd_transfer_ack"] == "unknown"
    assert port.writes == [b"\x1b", b"s"]
    assert port.opens == port.closes == 1
    assert not port.is_open and not backend.hardware_connected
    assert len(port.read_threads) == 1
    assert backend.max_active == 1
    assert events[-1] == "serial_close"
    assert not any("PIX" in event or "f800" in event for event in events)


def test_preflight_requires_expected_firmware_and_lists_true_effects_without_open():
    backend, port, plan = device()
    assert not port.opens and not port.writes
    assert plan["protocol"] == "pocketjs-legacy-capture-v1"
    assert plan["firmware_verification"] == "configured_expected_hash_only"
    effects = " ".join(plan["effects"])
    for phrase in ("already HOME", "one fixed Esc", "lose unsaved", "private data", "18-second"):
        assert phrase in effects
    assert "does not prove LCD" in effects
    missing = backend.config.model_copy(update={"expected_firmware_sha256": None})
    backend.config = missing
    with pytest.raises(ValueError, match="^expected_firmware_required$"):
        backend.prepare(Action.CAPTURE)
    assert not port.opens


@pytest.mark.parametrize(
    ("kwargs", "code", "writes"),
    [
        ({"preloaded": [b"HOME_READY\n"], "home": b""}, "serial_capture_timeout", [b"\x1b"]),
        ({"preloaded": [b"HOME_RE"]}, "serial_capture_boundary", []),
        ({"home": b"HOME_READY\npartial"}, "serial_capture_boundary", [b"\x1b"]),
        ({"home": b"HOME_READY"}, "serial_capture_timeout", [b"\x1b"]),
        ({"home": b"APP_ID local.wrong\nHOME_READY\n"}, "serial_capture_unexpected_app", [b"\x1b"]),
        ({"home": b"HOME_READY\nAPP_ID local.wrong\n"}, "serial_capture_unexpected_app", [b"\x1b"]),
        ({"home": b"CAPTURE_BEGIN 240 135\nHOME_READY\n"}, "serial_capture_boundary", [b"\x1b"]),
        ({"shot": b""}, "serial_capture_timeout", [b"\x1b", b"s"]),
        ({"shot": frame()[:-1]}, "serial_capture_timeout", [b"\x1b", b"s"]),
        (
            {"shot": b"CAPTURE_BEGIN 240 135\nCAPTURE_END\n"},
            "serial_capture_invalid",
            [b"\x1b", b"s"],
        ),
        (
            {"shot": frame().replace(b"PIX 12 ", b"PIX 11 ")},
            "serial_capture_invalid",
            [b"\x1b", b"s"],
        ),
        (
            {"shot": frame() + b"APP_ID local.wrong\n"},
            "serial_capture_unexpected_app",
            [b"\x1b", b"s"],
        ),
        (
            {"after_frame": [b"APP_ID local.wrong\n"]},
            "serial_capture_unexpected_app",
            [b"\x1b", b"s"],
        ),
        ({"after_frame": [frame()]}, "serial_capture_invalid", [b"\x1b", b"s"]),
        ({"after_frame": [b"CAPTURE_BEGIN"]}, "serial_capture_boundary", [b"\x1b", b"s"]),
        ({"after_frame": [b"Guru Meditation Error\n"]}, "serial_fault_marker", [b"\x1b", b"s"]),
        ({"after_frame": [OSError("SYNTHETIC_SECRET")]}, "serial_read_failed", [b"\x1b", b"s"]),
        ({"partial_write": True}, "incomplete_serial_write", [b"\x1b", b"s"]),
    ],
)
async def test_capture_failures_are_fixed_bounded_and_do_not_retry(kwargs, code, writes):
    backend, port, plan = device(clock_step=0.01, **kwargs)
    events = []
    with pytest.raises(SerialFailure, match=f"^{code}$") as error:
        await backend.execute(plan, events.append)
    assert error.value.diagnostic["step"] == "capture_home"
    assert port.writes == writes
    assert port.opens == port.closes == 1
    assert "SYNTHETIC_SECRET" not in str(error.value.diagnostic) + str(events)
    assert not port.is_open and not backend.hardware_connected
    assert port.clock.now <= 18.02


async def test_frame_after_fault_does_not_return_an_image():
    backend, port, plan = device(shot=b"panic'ed SYNTHETIC_SECRET\n" + frame())
    with pytest.raises(SerialFailure, match="^serial_fault_marker$"):
        await backend.execute(plan, lambda _: None)
    assert port.writes == [b"\x1b", b"s"]


async def test_image_is_withheld_if_port_close_fails():
    backend, port, plan = device(close_error=True)
    with pytest.raises(SerialFailure, match="^serial_close_unconfirmed$"):
        await backend.execute(plan, lambda _: None)
    assert backend.ownership_uncertain and backend.hardware_connected
    assert backend.active == 1 and port.closes == 1


async def test_image_and_ownership_are_withheld_until_close_has_finished():
    backend, port, plan = device()
    port.close_release = threading.Event()
    task = asyncio.create_task(backend.execute(plan, lambda _: None))
    try:
        assert await asyncio.to_thread(port.close_started.wait, 5)
        assert not task.done() and backend.active == 1
        assert backend.hardware_connected
    finally:
        port.close_release.set()
    result = await task
    assert isinstance(result, BackendResult)
    assert backend.active == 0 and not backend.hardware_connected


async def test_running_cancellation_waits_for_close_and_does_not_publish_image():
    backend, port, plan = device()
    port.close_release = threading.Event()
    task = asyncio.create_task(backend.execute(plan, lambda _: None))
    try:
        assert await asyncio.to_thread(port.close_started.wait, 5)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done() and backend.active == 1
    finally:
        port.close_release.set()
    with pytest.raises(SerialFailure, match="^operation_interrupted_review_required$"):
        await task
    assert backend.active == 0 and not port.is_open


def test_escape_is_not_a_general_raw_command_capability():
    backend, port, _ = device()
    reader = Reader(port, lambda _: None, port.clock, 18)
    for key, marker, step in (
        (b"\x1b", rb"HOME_READY", "command"),
        (b"\x1b", rb"APP_ROW", "capture_home"),
        (b"s", rb"CAPTURE_END", "capture_home"),
        (b"raw text", rb"HOME_READY", "capture_home"),
    ):
        with pytest.raises(SerialFailure, match="^command_not_allowlisted$"):
            reader.command(key, marker, step=step)
    assert not port.writes and not backend.calls
