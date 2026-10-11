"""Fixed reboot/log workflow tests. All USB identities, clocks and ports are synthetic."""

import asyncio
import json
import threading
from collections import deque
from dataclasses import replace

import pytest

from cardputer_mcp import serial_backend as serial_module
from cardputer_mcp.backend import BackendResult
from cardputer_mcp.config import SerialSettings
from cardputer_mcp.device_observation import DeviceLogCollector
from cardputer_mcp.models import TestName as Action
from cardputer_mcp.ports import DeviceSelector, PortInfo
from cardputer_mcp.serial_backend import SerialBackend, SerialFailure, stable_usb_fingerprint

STARTUP = (
    b"I (100) app_init: Application information:\n"
    b"I (101) app_init: Project name:     synthetic-pocketjs\n"
    b"I (102) app_init: App version:      v0.0.7-test\n"
    b"I (104) app_init: ELF file SHA256:  abcdef0123456789...\n"
    b"I (105) app_init: ESP-IDF:          v5.5-test\n"
)


class Clock:
    def __init__(self):
        self.now = 0.0
        self.pauses = []

    def __call__(self):
        return self.now

    def pause(self, seconds):
        self.pauses.append(seconds)
        self.now += seconds


class FakePort:
    def __init__(self, clock, *, trace=None, name="initial", chunks=()):
        self.clock = clock
        self.trace = [] if trace is None else trace
        self.name = name
        self.chunks = deque(chunks)
        self._dtr = self._rts = False
        self.port = None
        self.is_open = False
        self.opens = self.closes = self.discards = 0
        self.writes = []
        self.read_sizes = []
        self.read_starts = []
        self.received = 0
        self.owner_threads = set()
        self.read_seconds = 0.1
        self.read_hook = None
        self.close_hook = None
        self.discard_error = self.open_error = self.close_error = False
        self.still_open = False
        self.control_error = None
        self.control_count = 0

    def event(self, *event):
        self.trace.append((self.name, *event))
        self.owner_threads.add(threading.get_ident())

    @property
    def dtr(self):
        return self._dtr

    @dtr.setter
    def dtr(self, value):
        self.event("dtr", value)
        self._dtr = value
        self.control_check()

    @property
    def rts(self):
        return self._rts

    @rts.setter
    def rts(self, value):
        self.event("rts", value)
        self._rts = value
        self.control_check()

    def control_check(self):
        if self.discards:
            self.control_count += 1
            if self.control_count == self.control_error:
                raise OSError("SYNTHETIC_PRIVATE_CONTROL_ERROR")

    def open(self):
        self.event("open", self.port)
        self.opens += 1
        assert self.dtr is False and self.rts is False
        if self.open_error:
            raise OSError("SYNTHETIC_PRIVATE_BUSY_PORT")
        self.is_open = True

    def close(self):
        self.event("close")
        self.closes += 1
        if self.close_hook:
            self.close_hook()
        if self.close_error:
            raise OSError("SYNTHETIC_PRIVATE_CLOSE_ERROR")
        if not self.still_open:
            self.is_open = False

    def reset_input_buffer(self):
        self.event("discard")
        self.discards += 1
        if self.discard_error:
            raise OSError("SYNTHETIC_PRIVATE_DISCARD_ERROR")

    def read(self, size=1):
        self.event("read", size)
        self.read_sizes.append(size)
        self.read_starts.append(self.clock.now)
        self.clock.now += self.read_seconds
        if self.read_hook:
            self.read_hook()
        if not self.chunks:
            return b""
        value = self.chunks.popleft()
        if isinstance(value, Exception):
            raise value
        if type(value) is not bytes:
            return value
        if len(value) > size:
            self.chunks.appendleft(value[size:])
        self.received += len(value[:size])
        return value[:size]

    def write(self, value):
        self.writes.append(value)
        raise AssertionError("Restart must never send serial bytes")


def identity(**changes):
    return PortInfo(
        port="SYNTHETIC_COM1",
        vid=0x303A,
        pid=0x1001,
        serial_number="SYNTHETIC-USB-SERIAL",
        location="1-2",
        description="Synthetic only",
    ).model_copy(update=changes)


def device(*, chunks=(), entries=None, second_chunks=(), selector_location=None):
    clock = Clock()
    trace = []
    ports = [
        FakePort(clock, trace=trace, chunks=chunks),
        FakePort(clock, trace=trace, name="replacement", chunks=second_chunks),
    ]
    inventory = [identity()] if entries is None else entries
    created = []

    def factory():
        assert len(created) < 2, "A second reopen is forbidden"
        port = ports[len(created)]
        created.append(port)
        return port

    backend = SerialBackend(
        SerialSettings(
            selector=DeviceSelector(
                vid=0x303A,
                pid=0x1001,
                serial_number="SYNTHETIC-USB-SERIAL",
                location=selector_location,
            )
        ),
        inventory=lambda: inventory,
        factory=factory,
        clock=clock,
        pause=clock.pause,
    )
    return backend, ports, inventory, created


def prepared(backend):
    return {"test": Action.RESTART.value, **backend.prepare(Action.RESTART)}


def observed(wire, *, gap=False):
    collector = DeviceLogCollector()
    collector.feed(wire)
    return replace(collector.finish(), reset_sequence_issued=True, capture_gap=gap)


def disconnect_to(ports, inventory, replacement=None):
    def disconnect():
        ports[0].read_hook = None
        inventory[:] = [identity(port="SYNTHETIC_COM99") if replacement is None else replacement]

    ports[0].read_hook = disconnect


def test_plan_binds_exact_fixed_reset_and_discloses_limitations_without_open():
    backend, ports, _, created = device()
    plan = prepared(backend)
    assert plan["stages"] == ["serial_open", "restart_and_observe", "serial_close"]
    assert plan["protocol"] == "esp-idf-startup-log-v1"
    assert plan["firmware_verification"] == "unverified_device_report"
    assert plan["observe_seconds"] == 30
    assert plan["restart"] == {
        "method": "esp32s3_usb_serial_jtag_rts_v1",
        "attempts": 1,
        "rts_hold_milliseconds": 100,
        "reconnect_seconds": 8,
        "max_reopens": 1,
        "stable_usb_fingerprint": stable_usb_fingerprint(identity()),
        "buffer_policy": "discard_before_reset",
    }
    for phrase in (
        "lose unsaved",
        "glitch control lines",
        "additional unintended resets",
        "100 ms",
        "no reset retry",
        "64 KiB",
        "30 seconds",
        "8 seconds",
        "Early boot logs",
        "download-mode",
        "authenticated recipient",
        "5 minutes",
        "unverified",
    ):
        assert phrase in " ".join(plan["effects"])
    assert not created and all(not port.trace for port in ports)


@pytest.mark.parametrize(
    "change",
    [
        {"vid": 0x10C4},
        {"pid": 0x1002},
        {"serial_number": None},
        {"serial_number": ""},
        {"serial_number": "   "},
    ],
)
def test_unsupported_or_unidentified_interface_cannot_prepare(change):
    backend, _, inventory, created = device()
    inventory[:] = [identity(**change)]
    # Select by location so the restart guard itself must reject absent serials.
    backend.config = SerialSettings(
        selector=DeviceSelector(vid=inventory[0].vid, pid=inventory[0].pid, location="1-2")
    )
    with pytest.raises(ValueError, match="serial_restart_unsupported"):
        prepared(backend)
    assert not created


def test_duplicate_serial_is_rejected_even_when_location_selects_one():
    backend, _, _, created = device(
        entries=[identity(), identity(port="SYNTHETIC_COM2", location="1-3")],
        selector_location="1-2",
    )
    with pytest.raises(ValueError, match="device_ambiguous"):
        prepared(backend)
    assert not created


async def test_exact_pulse_no_writes_no_reopen_and_report_after_close():
    backend, ports, _, created = device(chunks=[STARTUP])
    events = []
    result = await backend.execute(prepared(backend), events.append)
    port = ports[0]
    assert isinstance(result, BackendResult)
    assert result.code == "restart_observation_completed_unverified"
    assert result.observation.metadata() == observed(STARTUP).metadata()
    assert result.observation.payload() == observed(STARTUP).payload()
    assert result.observation.metadata()["firmware"]["status"] == "observed"
    discard = port.trace.index(("initial", "discard"))
    assert port.trace[discard : discard + 8] == [
        ("initial", "discard"),
        ("initial", "dtr", False),
        ("initial", "rts", False),
        ("initial", "dtr", False),
        ("initial", "rts", True),
        ("initial", "dtr", False),
        ("initial", "rts", False),
        ("initial", "dtr", False),
    ]
    assert backend.clock.pauses == [0.1]
    assert port.discards == port.opens == port.closes == 1
    assert port.writes == [] and len(created) == 1 and not port.is_open
    assert backend.active == 0 and not backend.hardware_connected
    assert len(port.owner_threads) == 1
    assert events == [
        "serial_open",
        "restart_and_observe",
        "serial_restart_requested",
        "serial_restart_complete",
        "firmware_observe",
        "serial_close",
    ]


async def test_empty_report_does_not_claim_application_boot():
    backend, ports, _, _ = device()
    ports[0].read_seconds = 1
    result = await backend.execute(prepared(backend), lambda _: None)
    assert result.observation.metadata() == observed(b"").metadata()
    assert result.observation.metadata()["firmware"]["status"] == "unavailable"
    assert all(start < 30 for start in ports[0].read_starts)
    assert backend.clock.now == pytest.approx(30.1)


@pytest.mark.parametrize(
    "change",
    [
        {"port": "SYNTHETIC_COM99"},
        {"location": "1-3"},
        {"serial_number": "OTHER"},
    ],
)
async def test_initial_full_fingerprint_change_never_opens(change):
    backend, _, inventory, created = device()
    plan = prepared(backend)
    inventory[:] = [identity(**change)]
    with pytest.raises((SerialFailure, ValueError)):
        await backend.execute(plan, lambda _: None)
    assert not created


async def test_post_open_identity_change_prevents_discard_and_pulse():
    backend, ports, inventory, _ = device()
    plan = prepared(backend)
    original = ports[0].open

    def changed_open():
        original()
        inventory[:] = [identity(port="SYNTHETIC_COM99")]

    ports[0].open = changed_open
    with pytest.raises(SerialFailure, match="device_identity_changed"):
        await backend.execute(plan, lambda _: None)
    assert ports[0].opens == ports[0].closes == 1
    assert ports[0].discards == 0


@pytest.mark.parametrize(
    "field,value",
    [
        ("method", "OTHER"),
        ("attempts", 2),
        ("rts_hold_milliseconds", 200),
        ("reconnect_seconds", 9),
        ("max_reopens", 2),
        ("buffer_policy", "after_reset"),
        ("stable_usb_fingerprint", "0" * 64),
    ],
)
async def test_changed_reset_plan_never_opens(field, value):
    backend, _, _, created = device()
    plan = prepared(backend)
    plan["restart"][field] = value
    with pytest.raises(SerialFailure, match="device_or_plan_changed"):
        await backend.execute(plan, lambda _: None)
    assert not created


async def test_discard_failure_does_not_reset():
    backend, ports, _, created = device()
    ports[0].discard_error = True
    with pytest.raises(SerialFailure, match="serial_restart_buffer_discard_failed"):
        await backend.execute(prepared(backend), lambda _: None)
    assert not any(item[1:] == ("rts", True) for item in ports[0].trace)
    assert len(created) == 1 and ports[0].closes == 1


@pytest.mark.parametrize("control", range(1, 8))
async def test_any_control_failure_has_unknown_outcome_and_never_retries(control):
    backend, ports, _, created = device()
    ports[0].control_error = control
    with pytest.raises(SerialFailure, match="serial_restart_control_failed") as error:
        await backend.execute(prepared(backend), lambda _: None)
    assert error.value.diagnostic["step"] == "restart_control"
    assert len(created) == 1 and ports[0].closes == 1
    assert not ports[0].read_starts
    assert sum(item[1:] == ("rts", True) for item in ports[0].trace) <= 1
    assert "PRIVATE" not in json.dumps(error.value.diagnostic)


async def test_reenumeration_changes_com_but_preserves_collector_and_single_owner():
    split = 80
    backend, ports, inventory, created = device(
        chunks=[STARTUP[:split], OSError("SYNTHETIC_PRIVATE_DISCONNECT")],
        second_chunks=[STARTUP[split:]],
    )
    disconnect_to(ports, inventory)
    events = []
    result = await backend.execute(prepared(backend), events.append)
    expected = DeviceLogCollector()
    expected.feed(STARTUP[:split])
    expected.mark_gap()
    expected.feed(STARTUP[split:])
    report = replace(expected.finish(), reset_sequence_issued=True)
    assert result.observation.payload() == report.payload()
    assert report.firmware.status == "unavailable"
    assert report.pending_line_omitted and report.omitted_lines == 2
    assert len(created) == 2 and ports[1].port == "SYNTHETIC_COM99"
    assert ports[0].closes == ports[1].closes == 1
    assert ports[0].discards == 1 and ports[1].discards == 0
    assert ports[0].trace.index(("initial", "close")) < ports[0].trace.index(
        ("replacement", "open", "SYNTHETIC_COM99")
    )
    assert sum(item[1:] == ("rts", True) for item in ports[0].trace) == 1
    assert len(ports[0].owner_threads | ports[1].owner_threads) == 1
    assert backend.active == 0 and backend.max_active == 1
    assert events[-4:] == [
        "serial_reconnect_wait",
        "serial_reopened",
        "firmware_observe",
        "serial_close",
    ]


@pytest.mark.parametrize("location", [None, "1-2"])
async def test_reconnect_requires_exact_location_including_none(location):
    backend, ports, inventory, created = device(
        entries=[identity(location=location)],
        chunks=[OSError("disconnect")],
    )
    disconnect_to(
        ports,
        inventory,
        identity(port="SYNTHETIC_COM99", location=("different" if location is None else None)),
    )
    with pytest.raises(SerialFailure, match="device_identity_changed"):
        await backend.execute(prepared(backend), lambda _: None)
    assert len(created) == 1 and ports[0].closes == 1


async def test_reconnect_accepts_unchanged_none_location_and_description_change():
    backend, ports, inventory, created = device(
        entries=[identity(location=None)],
        chunks=[OSError("disconnect")],
    )
    disconnect_to(
        ports,
        inventory,
        identity(port="SYNTHETIC_COM99", location=None, description="Changed description"),
    )
    result = await backend.execute(prepared(backend), lambda _: None)
    assert result.observation.metadata() == observed(b"", gap=True).metadata()
    assert len(created) == 2


@pytest.mark.parametrize(
    "entries",
    [
        [],
        [identity(vid=0x10C4)],
        [identity(pid=0x1002)],
        [identity(serial_number="OTHER")],
    ],
)
async def test_missing_or_other_identity_waits_only_eight_seconds_without_open(entries):
    backend, ports, inventory, created = device(chunks=[OSError("disconnect")])

    def vanished():
        inventory[:] = entries
        ports[0].read_hook = None

    ports[0].read_hook = vanished
    with pytest.raises(SerialFailure, match="serial_reconnect_timeout"):
        await backend.execute(prepared(backend), lambda _: None)
    assert len(created) == 1 and ports[0].closes == 1
    assert backend.clock.now == pytest.approx(8.1)
    assert backend.active == 0 and not backend.hardware_connected


async def test_duplicate_usb_serial_on_reconnect_rejects_without_open():
    backend, ports, inventory, created = device(chunks=[OSError("disconnect")])

    def duplicates():
        inventory[:] = [identity(), identity(port="SYNTHETIC_COM99", location="different")]

    ports[0].read_hook = duplicates
    with pytest.raises(SerialFailure, match="device_identity_changed"):
        await backend.execute(prepared(backend), lambda _: None)
    assert len(created) == 1 and ports[0].closes == 1


async def test_reopen_busy_is_one_attempt_with_no_reset_or_retry():
    backend, ports, inventory, created = device(chunks=[OSError("disconnect")])
    disconnect_to(ports, inventory)
    ports[1].open_error = True
    with pytest.raises(SerialFailure, match="serial_open_failed"):
        await backend.execute(prepared(backend), lambda _: None)
    assert len(created) == 2
    assert ports[0].closes == ports[1].closes == ports[1].opens == 1
    assert ports[1].discards == 0


async def test_identity_change_during_reopen_closes_new_handle_before_reading():
    backend, ports, inventory, created = device(chunks=[OSError("disconnect")])
    disconnect_to(ports, inventory)
    original = ports[1].open

    def changed_open():
        original()
        inventory[:] = [identity(port="SYNTHETIC_COM100")]

    ports[1].open = changed_open
    with pytest.raises(SerialFailure, match="device_identity_changed"):
        await backend.execute(prepared(backend), lambda _: None)
    assert len(created) == 2 and ports[1].closes == 1 and not ports[1].read_starts


async def test_second_disconnect_never_opens_a_third_handle():
    backend, ports, inventory, created = device(
        chunks=[OSError("disconnect")],
        second_chunks=[OSError("again")],
    )
    disconnect_to(ports, inventory)
    with pytest.raises(SerialFailure, match="serial_read_failed"):
        await backend.execute(prepared(backend), lambda _: None)
    assert len(created) == 2 and all(port.closes == 1 for port in ports)


@pytest.mark.parametrize("value", [RuntimeError("PRIVATE"), None, bytearray(b"PRIVATE"), "PRIVATE"])
async def test_non_os_failure_and_malformed_reads_never_reopen(value):
    backend, ports, _, created = device(chunks=[value])
    with pytest.raises(SerialFailure, match="serial_read_failed"):
        await backend.execute(prepared(backend), lambda _: None)
    assert len(created) == 1 and ports[0].closes == 1


async def test_oversized_driver_return_never_reopens_or_publishes():
    backend, ports, _, created = device()
    ports[0].read = lambda size: b"x" * (size + 1)
    with pytest.raises(SerialFailure, match="serial_read_failed"):
        await backend.execute(prepared(backend), lambda _: None)
    assert len(created) == 1 and ports[0].closes == 1


async def test_actual_wire_byte_cap_survives_reopen_and_limits_final_read():
    wire = (b"password=" + b"x" * 1014 + b"\n") * 64
    backend, ports, inventory, created = device(
        chunks=[wire[:65535], OSError("disconnect")],
        second_chunks=[wire[65535:] + b"AFTER"],
    )
    disconnect_to(ports, inventory)
    result = await backend.execute(prepared(backend), lambda _: None)
    expected = DeviceLogCollector()
    expected.feed(wire[:65535])
    expected.mark_gap()
    expected.feed(wire[65535:])
    assert (
        result.observation.payload()
        == replace(expected.finish(), reset_sequence_issued=True).payload()
    )
    assert result.observation.redacted_lines == 63
    assert result.observation.omitted_lines == 2
    assert len(created) == 2
    assert sum(port.received for port in ports) == 65536
    assert ports[1].read_sizes == [1] and list(ports[1].chunks) == [b"AFTER"]
    assert backend.clock.now < 30


async def test_read_returning_after_deadline_is_omitted_without_reopen():
    backend, ports, _, created = device(chunks=[STARTUP])
    ports[0].read_seconds = 30
    result = await backend.execute(prepared(backend), lambda _: None)
    assert result.observation.metadata() == observed(b"").metadata()
    assert len(ports[0].read_starts) == len(created) == 1


async def test_disconnect_after_reset_settling_window_does_not_reopen():
    backend, ports, _, created = device(chunks=[OSError("late disconnect")])
    ports[0].read_seconds = 9
    with pytest.raises(SerialFailure, match="serial_read_failed"):
        await backend.execute(prepared(backend), lambda _: None)
    assert len(created) == 1 and ports[0].closes == 1


async def test_reset_pulse_holds_asserted_rts_and_cleared_dtr_for_fixed_pause():
    backend, ports, _, _ = device()
    pauses = []

    def checked_pause(seconds):
        pauses.append((seconds, ports[0].rts, ports[0].dtr))
        backend.clock.pause(seconds)

    backend.pause = checked_pause
    await backend.execute(prepared(backend), lambda _: None)
    assert pauses == [(0.1, True, False)]


async def test_reconnect_wait_consumes_original_thirty_second_receive_budget():
    backend, ports, inventory, created = device(chunks=[OSError("disconnect")])
    ports[0].read_hook = lambda: inventory.clear()
    ports[1].read_seconds = 1

    def delayed_return(seconds):
        backend.clock.pause(seconds)
        if backend.clock.now >= 7:
            inventory[:] = [identity(port="SYNTHETIC_COM99")]

    backend.pause = delayed_return
    result = await backend.execute(prepared(backend), lambda _: None)
    assert len(created) == 2 and result.observation.metadata()["capture_gap"]
    assert 7 <= ports[1].read_starts[0] < 8
    assert all(start < 30 for start in ports[1].read_starts)
    assert 30 <= backend.clock.now < 31


@pytest.mark.parametrize("slow_phase", ["old_close", "new_open", "post_open_identity"])
async def test_slow_reconnect_phase_cannot_extend_eight_second_window(slow_phase):
    backend, ports, inventory, created = device(chunks=[OSError("disconnect")])
    disconnect_to(ports, inventory)
    if slow_phase == "old_close":
        ports[0].close_hook = lambda: backend.clock.pause(9)
    else:
        original = ports[1].open

        def slow_open():
            original()
            if slow_phase == "new_open":
                backend.clock.pause(9)
            else:

                def slow_inventory():
                    backend.clock.pause(9)
                    return inventory

                backend.inventory = slow_inventory

        ports[1].open = slow_open
    with pytest.raises(SerialFailure, match="serial_reconnect_timeout"):
        await backend.execute(prepared(backend), lambda _: None)
    assert all(port.closes == 1 for port in created)
    assert not ports[1].read_starts
    assert backend.active == 0 and not backend.hardware_connected


async def test_fault_strings_are_observations_and_known_secrets_are_redacted():
    wire = STARTUP + b"Guru Meditation Error: synthetic panic\npassword=SYNTHETIC_SECRET\n"
    backend, ports, _, _ = device(chunks=[wire])
    result = await backend.execute(prepared(backend), lambda _: None)
    payload = json.dumps(result.observation.payload())
    assert "Guru Meditation" in payload and "SYNTHETIC_SECRET" not in payload
    assert ports[0].writes == []


@pytest.mark.parametrize("failure", ["feed", "finish", "clear_transient"])
@pytest.mark.parametrize("close_uncertain", [False, True])
async def test_unexpected_collector_failure_cannot_skip_final_close(
    monkeypatch, failure, close_uncertain
):
    class BrokenCollector(DeviceLogCollector):
        pass

    def broken(*args):
        raise RuntimeError("SYNTHETIC_PRIVATE_PARSER_ERROR")

    setattr(BrokenCollector, failure, broken)
    monkeypatch.setattr(serial_module, "DeviceLogCollector", BrokenCollector)
    backend, ports, _, created = device(chunks=[STARTUP])
    ports[0].close_error = close_uncertain
    expected = "serial_close_unconfirmed" if close_uncertain else "serial_operation_uncertain"
    with pytest.raises(SerialFailure, match=expected) as error:
        await backend.execute(prepared(backend), lambda _: None)
    assert len(created) == ports[0].closes == 1
    assert backend.active == int(close_uncertain)
    assert backend.hardware_connected == backend.ownership_uncertain == close_uncertain
    assert error.value.__context__ is None and error.value.__cause__ is None
    assert "PRIVATE" not in repr(error.value)


async def test_malformed_closed_state_retains_ownership():
    backend, ports, _, created = device()

    def uncertain_close():
        ports[0].closes += 1
        ports[0].is_open = None

    ports[0].close = uncertain_close
    with pytest.raises(SerialFailure, match="serial_close_unconfirmed"):
        await backend.execute(prepared(backend), lambda _: None)
    assert backend.active == 1 and backend.ownership_uncertain
    assert len(created) == ports[0].closes == 1


async def test_cancellation_during_intermediate_close_keeps_owner_through_reopen():
    backend, ports, inventory, created = device(chunks=[OSError("disconnect")])
    disconnect_to(ports, inventory)
    closing, allow_close = threading.Event(), threading.Event()

    def blocked_close():
        closing.set()
        assert allow_close.wait(timeout=5)

    ports[0].close_hook = blocked_close
    plan = prepared(backend)
    task = asyncio.create_task(backend.execute(plan, lambda _: None))
    try:
        assert await asyncio.to_thread(closing.wait, 2)
        task.cancel()
        await asyncio.sleep(0)
        assert backend.active == 1 and ports[0].is_open and not task.done()
        assert len(created) == 1
        with pytest.raises(SerialFailure, match="multiple_device_owners"):
            await backend.execute(plan, lambda _: None)
        allow_close.set()
        with pytest.raises(SerialFailure, match="operation_interrupted_review_required"):
            await asyncio.wait_for(task, 2)
        assert len(created) == 2 and all(port.closes == 1 for port in ports)
        assert backend.active == 0 and not backend.hardware_connected
    finally:
        allow_close.set()
        if not task.done():
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("intermediate", [False, True])
@pytest.mark.parametrize("raises", [False, True])
async def test_uncertain_close_retains_owner_without_second_close_or_reopen(intermediate, raises):
    backend, ports, inventory, created = device(
        chunks=[OSError("disconnect")] if intermediate else [STARTUP],
    )
    disconnect_to(ports, inventory)
    ports[0].close_error = raises
    ports[0].still_open = not raises
    plan = prepared(backend)
    events = []
    with pytest.raises(SerialFailure, match="serial_close_unconfirmed"):
        await backend.execute(plan, events.append)
    assert backend.ownership_uncertain and backend.active == 1 and backend.hardware_connected
    assert backend._unclosed_port is ports[0]
    assert len(created) == ports[0].closes == 1
    assert events.count("serial_close_unconfirmed") == 1 and "serial_close" not in events
    with pytest.raises(SerialFailure, match="multiple_device_owners"):
        await backend.execute(plan, lambda _: None)
    assert len(created) == ports[0].closes == 1


async def test_cancellation_drains_reset_reader_and_final_close_under_same_owner():
    backend, ports, _, created = device(chunks=[STARTUP])
    reading, allow_read, closing, allow_close = [threading.Event() for _ in range(4)]

    def read_wait():
        reading.set()
        assert allow_read.wait(timeout=5)

    def close_wait():
        closing.set()
        assert allow_close.wait(timeout=5)

    ports[0].read_hook = read_wait
    ports[0].close_hook = close_wait
    task = asyncio.create_task(backend.execute(prepared(backend), lambda _: None))
    try:
        assert await asyncio.to_thread(reading.wait, 2)
        task.cancel()
        await asyncio.sleep(0)
        assert backend.active == 1 and ports[0].is_open and not task.done()
        allow_read.set()
        assert await asyncio.to_thread(closing.wait, 2)
        task.cancel()
        await asyncio.sleep(0)
        assert backend.active == 1 and ports[0].is_open and not task.done()
        allow_close.set()
        with pytest.raises(SerialFailure, match="operation_interrupted_review_required"):
            await asyncio.wait_for(task, 2)
        assert backend.active == 0 and not ports[0].is_open
        assert len(created) == ports[0].closes == 1
    finally:
        allow_read.set()
        allow_close.set()
        if not task.done():
            await asyncio.gather(task, return_exceptions=True)
