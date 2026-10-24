"""status separates the broker's own port state from USB presence and never opens a port."""

import pytest

from cardputer_mcp.broker import Broker
from cardputer_mcp.config import SerialSettings, Settings
from cardputer_mcp.ports import DeviceSelector, PortInfo
from cardputer_mcp.serial_backend import SerialBackend

SELECTOR = DeviceSelector(vid=1, pid=2, serial_number="SYNTHETIC_SERIAL")
MATCH = PortInfo(port="COM_SYNTHETIC_A", vid=1, pid=2, serial_number="SYNTHETIC_SERIAL")
OTHER = PortInfo(port="COM_SYNTHETIC_B", vid=1, pid=3, serial_number="SYNTHETIC_OTHER")


def never_open():
    raise AssertionError("status must not open a serial port")


def failing_inventory():
    raise OSError("synthetic enumeration failure")


@pytest.mark.parametrize(
    ("inventory", "expected"),
    [
        (lambda: [MATCH, OTHER], "present"),
        (lambda: [OTHER], "missing"),
        (lambda: [MATCH, MATCH.model_copy(update={"port": "COM_SYNTHETIC_C"})], "ambiguous"),
        (failing_inventory, "unavailable"),
    ],
)
async def test_serial_status_reports_usb_presence_without_opening(tmp_path, inventory, expected):
    serial = SerialSettings(selector=SELECTOR)
    backend = SerialBackend(serial, inventory=inventory, factory=never_open)
    broker = Broker(
        Settings(state_dir=tmp_path / "state", backend="serial", serial=serial),
        backend=backend,
        serial_enabled=True,
        lock_path=tmp_path / "device.lockfile",
        principal_valid=lambda *args: True,
    )
    await broker.start()
    try:
        status = broker.status()
    finally:
        await broker.close()
    connection = status["connection"]
    assert connection["usb_presence"] == expected
    assert connection["usb_presence_source"] == "usb_enumeration_without_opening_port"
    assert connection["serial_port_open"] is False is status["hardware_connected"]
    assert "idle is normal" in connection["serial_port_open_meaning"]
    assert "SYNTHETIC" not in repr(connection)
    assert backend.calls == 0


async def test_mock_status_does_not_claim_usb_presence(tmp_path):
    broker = Broker(
        Settings(state_dir=tmp_path / "state"),
        lock_path=tmp_path / "device.lockfile",
        principal_valid=lambda *args: True,
    )
    await broker.start()
    try:
        connection = broker.status()["connection"]
    finally:
        await broker.close()
    assert connection["usb_presence"] == "not_applicable_mock_backend"
    assert connection["usb_presence_source"] is None
