"""Synthetic single-reader telemetry; no serial hardware or firmware work."""

import json

import pytest
from test_derby_smoke import Wire
from test_serial_backend import Clock, FakePort

from cardputer_mcp.config import SerialSettings
from cardputer_mcp.derby_smoke import derby_smoke
from cardputer_mcp.derby_telemetry import DerbyDiagnosticsCollector
from cardputer_mcp.models import TestName as Action
from cardputer_mcp.ports import PortInfo
from cardputer_mcp.serial_backend import Reader, SerialBackend, SerialFailure

TLM = (
    b"DERBY_TLM v=1 run=1 us=1000000 begin=0 frame=30 ctx=30 seed=00000002 "
    b"race=1 phase=2 cam=8 tier=1 step=391 u=500000 gu_sample_max=130000 "
    b"cap=163840 eval_used=120000 eval_cap=163840 refused=0 seq=1 limit=300 "
    b"io_us_max=200 intervals=0:0,0:0,0:0,0:0,0:0,0:0,0:0,0:0,29:966667,0:0"
)


def device_for(wire):
    return SerialBackend(
        SerialSettings(
            selector={"vid": 1, "pid": 2, "serial_number": "synthetic"},
            expected_firmware_sha256="a" * 64,
        ),
        inventory=lambda: [PortInfo(port="COM42", vid=1, pid=2, serial_number="synthetic")],
        factory=lambda: wire,
        clock=lambda: wire.now,
    )


@pytest.mark.parametrize("split", [1, 7, 4096])
def test_only_bare_complete_tlm_survives_fragmented_or_coalesced_single_reader(split):
    clock = Clock()
    port = FakePort(clock)
    wire = b"I (1) telemetry: " + TLM + b"\n" + TLM + b"\n"
    wire += TLM + b"\r\n"  # Normalization must not make a non-bare row valid.
    wire += b"PIX 0 SYNTHETIC_PRIVATE_PIXELS\nSYNTHETIC_PRIVATE_CONSOLE\n"
    port.chunks.extend(wire[at : at + split] for at in range(0, len(wire), split))
    reader = Reader(port, lambda _: None, clock, 180)
    collector = reader.derby_diagnostics = DerbyDiagnosticsCollector(clock=clock, origin=0)
    while reader.read_once():
        pass
    report = collector.snapshot()
    assert len(report.samples) == 1 and report.samples[0].seq == 1
    assert report.samples[0].cam == 8 and report.samples[0].tier == 1
    serialized = report.model_dump_json()
    assert "SYNTHETIC_PRIVATE" not in serialized and "DERBY_TLM" not in serialized
    assert port.writes == [] and port.opens == port.closes == 0
    reader.clear_transient()
    assert reader.derby_diagnostics is None and reader.derby_observation is None
    assert reader.pending == b"" and reader.lines == [] and reader.fault_tail == b""
    assert collector.snapshot().samples == ()


@pytest.mark.parametrize(
    "fault,kind,phase",
    [
        (b"DERBY DEMOFAIL SYNTHETIC_PRIVATE_EXCEPTION", "demo_fail", None),
        (b"DERBY FRAMEFAIL gate SYNTHETIC_PRIVATE_EXCEPTION", "frame_fail", "gate"),
        (b"DERBY LOADFAIL SYNTHETIC_PRIVATE_PLAN SYNTHETIC_PRIVATE_EXCEPTION", "load_fail", None),
        (b"E (123) app: OOM n=1 first_req=8192 used=150000", "oom", None),
        (
            b"\x1b[0;31mE (123) app: OOM n=1 first_req=8192 used=150000\x1b[0m\r",
            "oom",
            None,
        ),
    ],
)
@pytest.mark.parametrize("split", [None, 8])
def test_complete_fatal_category_is_collected_before_stop_without_suffix(fault, kind, phase, split):
    clock = Clock()
    port = FakePort(clock)
    port.chunks.append(TLM + b"\nDERBY GO LOADSTALL\n")
    line = fault + b"\n"
    port.chunks.extend([line] if split is None else [line[:split], line[split:]])
    reader = Reader(port, lambda _: None, clock, 180)
    reader.derby_mode = True
    collector = reader.derby_diagnostics = DerbyDiagnosticsCollector(clock=clock, origin=0)
    reader.read_once()  # LOADSTALL remains nonfatal.
    assert collector.snapshot().events[0].kind == "load_stall"
    with pytest.raises(SerialFailure, match="serial_fault_marker"):
        while reader.read_once():
            pass
    report = collector.snapshot()
    assert len(report.samples) == 1
    assert [event.kind for event in report.events] == ["load_stall", kind]
    assert report.events[-1].phase == phase
    assert "SYNTHETIC_PRIVATE" not in report.model_dump_json()
    assert port.writes == [] and not port.chunks


async def test_success_keeps_original_capture_keys_and_snapshots_without_pixels(monkeypatch):
    wire = Wire()
    write = wire.write

    def with_telemetry(key):
        count = write(key)
        if key == b"e":
            # Collection starts before launch acknowledgement, not after READY.
            wire.queue.appendleft(TLM + b"\nSYNTHETIC_PRIVATE_CONSOLE\n")
        return count

    wire.write = with_telemetry
    retained = []
    clear = Reader.clear_transient

    def clear_reader(reader):
        retained.append(reader)
        clear(reader)

    monkeypatch.setattr(Reader, "clear_transient", clear_reader)
    device = device_for(wire)
    plan = {"test": Action.DERBY.value, **device.prepare(Action.DERBY)}
    result = await device.execute_smoke(plan, lambda _: None, lambda: True)
    assert result.code == "derby_demo_cycle_observed_render_buffer_lcd_unverified"
    observation = result.derby_observation
    assert observation.image_acquired and observation.demo_cycle_observed
    assert len(observation.diagnostics.samples) == 1
    assert observation.diagnostics.samples[0].seq == 1
    serialized = observation.model_dump_json()
    assert "SYNTHETIC_PRIVATE" not in serialized and "PIX " not in serialized
    assert wire.writes == [b"\x1b", b"a"] + [b"u"] * 16 + [b"d"] * 12 + [b"e", b"s", b"q"]
    assert wire.closed and device.active == 0 and not device.hardware_connected
    assert len(retained) == 1 and retained[0].derby_diagnostics is None
    assert retained[0].pending == b"" and retained[0].lines == []


async def test_launch_fault_retains_diagnostics_before_ready_and_still_closes():
    wire = Wire()
    write = wire.write

    def failed_launch(key):
        count = write(key)
        if key == b"e":
            wire.queue.clear()
            wire.queue.append(TLM + b"\nDERBY FRAMEFAIL gate SYNTHETIC_PRIVATE_EXCEPTION\n")
        return count

    wire.write = failed_launch
    device = device_for(wire)
    plan = {"test": Action.DERBY.value, **device.prepare(Action.DERBY)}
    with pytest.raises(SerialFailure, match="serial_fault_marker") as caught:
        await device.execute_smoke(plan, lambda _: None, lambda: True)
    report = caught.value.diagnostic["derby_observation"]
    assert report["events"] == [] and not report["image_acquired"]
    assert len(report["diagnostics"]["samples"]) == 1
    assert report["diagnostics"]["events"][0]["kind"] == "frame_fail"
    assert report["diagnostics"]["events"][0]["phase"] == "gate"
    assert "SYNTHETIC_PRIVATE" not in json.dumps(caught.value.diagnostic)
    assert wire.closed and device.active == 0 and not device.hardware_connected
    assert b"s" not in wire.writes and b"q" not in wire.writes


def test_telemetry_photo_phase_does_not_authorize_a_capture_or_change_smoke_result():
    wire = Wire(phases=False)
    write = wire.write

    def with_photo_telemetry(key):
        count = write(key)
        if key == b"e":
            wire.queue.append(
                TLM.replace(b"phase=2", b"phase=3").replace(b"u=500000", b"u=-1") + b"\n"
            )
        return count

    wire.write = with_photo_telemetry
    reader = Reader(wire, lambda _: None, lambda: wire.now, 180)
    image, code = derby_smoke(reader)
    report = reader.derby_observation()
    assert len(report.diagnostics.samples) == 1 and report.diagnostics.samples[0].phase == 3
    assert image is None and code == "derby_photo_not_verified_no_capture"
    assert report.no_capture_completed() and not report.demo_cycle_observed
    assert b"s" not in wire.writes and wire.writes.count(b"q") == 1
    assert reader.deadline == 180 and wire.now < 180


@pytest.mark.parametrize("source", ["read", "authority", "cancel", "close"])
async def test_known_failure_snapshot_preserves_classification_and_cleanup(source, monkeypatch):
    wire = Wire()
    device = device_for(wire)
    read = wire.read
    valid = [True]
    known = SerialFailure("device_identity_changed", step="validate_identity", received_bytes=17)
    known.diagnostic["failure_origin"] = "authority_or_stop_check"
    injected = [False]

    def failing_read(size=4096):
        if injected[0]:
            if source == "read":
                raise known
            if source == "authority":
                valid[0] = False
            elif source == "cancel":
                device.smoke_stop.set()
        data = read(size)
        if data.startswith(b"DERBY SCENE gate"):
            injected[0] = True
            return data + TLM + b"\n"
        return data

    wire.read = failing_read
    if source == "close":

        def failed_close():
            raise OSError("SYNTHETIC_PRIVATE_CLOSE_ERROR")

        wire.close = failed_close
    retained = []
    clear = Reader.clear_transient

    def cleared(reader):
        retained.append(reader)
        clear(reader)

    monkeypatch.setattr(Reader, "clear_transient", cleared)
    plan = {"test": Action.DERBY.value, **device.prepare(Action.DERBY)}
    with pytest.raises(SerialFailure) as caught:
        await device.execute_smoke(plan, lambda _: None, lambda: valid[0])
    failure = caught.value
    if source == "read":
        assert failure is known and failure.code == "device_identity_changed"
        assert failure.diagnostic["received_bytes"] == 17
    elif source == "close":
        assert failure.code == "serial_close_unconfirmed" and device.ownership_uncertain
    else:
        assert failure.code == "operation_interrupted_review_required"
        assert failure.diagnostic["failure_origin"] == "authority_or_stop_check"
    report = failure.diagnostic["derby_observation"]
    assert len(report["diagnostics"]["samples"]) == 1
    assert "SYNTHETIC_PRIVATE" not in json.dumps(failure.diagnostic)
    assert len(retained) == 1 and retained[0].derby_diagnostics is None
    assert retained[0].pending == b"" and retained[0].fault_tail == b""
    if source != "close":
        assert wire.closed and not device.hardware_connected and not device.ownership_uncertain
        assert b"s" not in wire.writes and b"q" not in wire.writes
    else:
        assert wire.writes.count(b"s") == wire.writes.count(b"q") == 1
