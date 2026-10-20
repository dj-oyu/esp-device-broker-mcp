"""Offline only: no pySerial Serial construction, enumeration or actual port access."""

from collections import deque
from dataclasses import dataclass, field

import pytest

from cardputer_mcp.broker import Broker, BrokerError
from cardputer_mcp.config import SerialSettings, Settings
from cardputer_mcp.models import AcquireRequest, ExecuteRequest, Origin, PrepareRequest
from cardputer_mcp.models import TestName as Action
from cardputer_mcp.ports import DeviceSelector, PortInfo
from cardputer_mcp.serial_backend import Reader, SerialBackend, SerialFailure


class Clock:
    def __init__(self):
        self.value = 0.0

    def __call__(self):
        return self.value

    def tick(self):
        self.value += 0.01


@dataclass
class FakePort:
    clock: Clock
    split: int = 10000
    transform: object = None
    dtr: bool = True
    rts: bool = True
    port: str | None = None
    is_open: bool = False
    opens: int = 0
    closes: int = 0
    writes: list[bytes] = field(default_factory=list)
    chunks: deque = field(default_factory=deque)
    row: int = 13
    launches: int = 0
    partial_write: bool = False
    disconnect: bool = False

    def open(self):
        assert not self.dtr and not self.rts
        assert not self.is_open
        self.opens += 1
        self.is_open = True

    def close(self):
        self.closes += 1
        self.is_open = False

    def read(self, size=1):
        self.clock.tick()
        if self.disconnect:
            raise OSError("SYNTHETIC_SECRET_DRIVER_DATA")
        return self.chunks.popleft() if self.chunks else b""

    def write(self, data):
        self.writes.append(data)
        if self.partial_write:
            return 0
        if data == b"q":
            reply = (
                b"MEM free=500 largest=300 js=0 frames=2\r\nAPP_STOPPED\r\n"
                if self.launches
                else b""
            ) + b"HOME_READY\r\n"
        elif data == b"a":
            reply = b"CATEGORY 0\r\n"
        elif data == b"u":
            self.row = max(0, self.row - 1)
            reply = f"APP {self.row}\r\n".encode()
        elif data == b"e" and not self.launches:
            self.launches += 1
            reply = b"APP_ID local.hello\r\nHELLO_READY\r\nKASANE_FRAME_PRESENTED\r\n"
        elif data == b"e":
            reply = b"HELLO_COUNT 1\r\n"
        else:
            raise AssertionError("unexpected command")
        if self.transform:
            reply = self.transform(data, reply)
        self.chunks.extend(reply[i : i + self.split] for i in range(0, len(reply), self.split))
        return len(data)


def config():
    return SerialSettings(
        selector=DeviceSelector(vid=0xCAFE, pid=1, serial_number="SYNTHETIC-ONLY"),
        expected_firmware_sha256="a" * 64,
        observe_seconds=0.1,
    )


def inventory():
    return [PortInfo(port="COM42", vid=0xCAFE, pid=1, serial_number="SYNTHETIC-ONLY")]


def backend(split=10000, transform=None):
    clock = Clock()
    port = FakePort(clock, split=split, transform=transform)
    device = SerialBackend(config(), inventory=inventory, factory=lambda: port, clock=clock)
    return device, port


@pytest.mark.parametrize("split", [1, 2, 3, 7, 64, 10000])
async def test_complete_hello_across_arbitrary_read_chunks(split):
    device, port = backend(split)
    plan = {"test": "hello", **device.prepare(Action.HELLO)}
    events = []
    result = await device.execute(plan, events.append)
    assert result == "hello_markers_passed_physical_checks_not_assessed"
    assert port.writes == [b"q", b"a"] + [b"u"] * 14 + [b"e", b"e", b"q"]
    assert port.opens == port.closes == 1
    assert not port.is_open and not device.hardware_connected
    assert device.max_active == 1
    assert "serial_identity_hello" in events and "serial_frame_presented" in events


async def test_observe_does_not_write_and_sanitizes_noisy_data():
    device, port = backend()
    port.chunks.extend([b"Authorization: Bearer SYNTHETIC_SECRET\n", b"HOME_READY\n"])
    events = []
    result = await device.execute(
        {"test": "passive_observe", **device.prepare(Action.OBSERVE)}, events.append
    )
    assert not port.writes
    assert "no_commands" in result
    assert "SYNTHETIC_SECRET" not in str(events)
    assert "serial_home_ready" in events


@pytest.mark.parametrize(
    "replace",
    [
        (b"APP_ID local.hello", b"APP_ID local.wrong"),
        (b"HELLO_READY\r\n", b""),
        (b"HELLO_COUNT 1", b"HELLO_COUNT 2"),
        (b"js=0", b"js=4"),
        (b"APP_STOPPED\r\n", b""),
        (b"APP 0", b"APP 99"),
    ],
)
async def test_hello_fails_closed_on_wrong_or_missing_evidence(replace):
    device, port = backend(transform=lambda key, data: data.replace(*replace))
    with pytest.raises(SerialFailure):
        await device.execute({"test": "hello", **device.prepare(Action.HELLO)}, lambda _: None)
    assert port.closes == 1


async def test_expected_identity_change_before_open_fails_closed():
    device, port = backend()
    plan = {"test": "hello", **device.prepare(Action.HELLO)}
    device.inventory = lambda: [inventory()[0].model_copy(update={"port": "COM43"})]
    with pytest.raises(SerialFailure):
        await device.execute(plan, lambda _: None)
    assert not port.opens and not port.writes


async def test_identity_change_after_open_closes_before_writing():
    device, port = backend()
    plan = {"test": "hello", **device.prepare(Action.HELLO)}
    calls = 0

    def changing():
        nonlocal calls
        calls += 1
        return inventory() if calls == 1 else []

    device.inventory = changing
    with pytest.raises(SerialFailure):
        await device.execute(plan, lambda _: None)
    assert port.opens == port.closes == 1
    assert not port.writes


@pytest.mark.parametrize("data", [b"Guru Medita", b"HOME_READY"])
def test_unterminated_markers_are_not_evidence(data):
    clock = Clock()
    port = FakePort(clock, transform=lambda key, reply: data)
    reader = Reader(port, lambda _: None, clock, 1)
    with pytest.raises(SerialFailure):
        reader.command(b"q", rb"HOME_READY", timeout=0.2)


def test_stale_coalesced_response_cannot_acknowledge_new_command():
    clock = Clock()
    port = FakePort(clock, transform=lambda key, reply: b"")
    port.chunks.append(b"HOME_READY\n")
    reader = Reader(port, lambda _: None, clock, 1)
    with pytest.raises(SerialFailure):
        reader.command(b"q", rb"HOME_READY", timeout=0.2)


async def test_disconnect_has_no_auto_recovery_or_extra_command():
    device, port = backend()
    port.disconnect = True
    with pytest.raises(SerialFailure):
        await device.execute({"test": "hello", **device.prepare(Action.HELLO)}, lambda _: None)
    assert port.opens == port.closes == 1
    assert not port.writes  # Drain detects failure before sending the first key.


async def test_port_not_created_until_explicit_authenticated_execution(tmp_path):
    device, port = backend()
    settings = Settings(backend="serial", serial=config(), state_dir=tmp_path)
    with pytest.raises(BrokerError, match="serial_runtime_opt_in_required"):
        Broker(settings, backend=device)
    broker = Broker(
        settings,
        backend=device,
        serial_enabled=True,
        lock_path=tmp_path / "device.lockfile",
        principal_valid=lambda principal, *_: principal == "serial-client",
    )
    await broker.start()
    try:
        assert not port.opens
        token = broker.acquire(AcquireRequest(owner=Origin.LOCAL), principal_id="serial-client")[
            "session"
        ]
        planned = broker.prepare(
            PrepareRequest(session=token, job_id="serial-test", test=Action.HELLO)
        )
        assert not port.opens
        request = ExecuteRequest(session=token, plan_hash=planned["plan_hash"])
        broker.execute(request)
        await broker.queue.join()
        assert broker.job("serial-test")["state"] == "succeeded"
        assert port.opens == port.closes == 1
    finally:
        await broker.close()


def test_observed_oom_attaches_fixed_reason_without_commands_or_raw_diagnostic():
    clock = Clock()
    port = FakePort(clock)
    port.chunks.append(
        b"PRIVATE APP_LOAD play compile out of memory\n"
        b"OOM first_req=1 used=2\nSTART_FAILED ESP_FAIL\n"
    )
    reader = Reader(port, lambda _: None, clock, 1)
    with pytest.raises(SerialFailure) as error:
        reader.read_once()
    assert error.value.code == "serial_fault_marker"
    assert error.value.diagnostic["device_fault_detail"] == "app_load_out_of_memory"
    assert "PRIVATE" not in repr(error.value.diagnostic)
    assert port.writes == []
