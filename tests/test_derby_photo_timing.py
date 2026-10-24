"""Synthetic host clocks and serial only; no physical photo/pixel attestation."""

import json
import threading
from collections import deque
from copy import deepcopy

import pytest
from test_derby_smoke import Wire

from cardputer_mcp.config import SerialSettings
from cardputer_mcp.derby_observation import DerbyObservation
from cardputer_mcp.derby_smoke import derby_smoke
from cardputer_mcp.models import TestName as Action
from cardputer_mcp.ports import PortInfo
from cardputer_mcp.serial_backend import Reader, SerialBackend, SerialFailure
from cardputer_mcp.smoke_port import SmokePort


def scene(phase, race="1000001", seed="AB123456"):
    return f"DERBY SCENE {phase} race={race} seed={seed} plans=2 reg=3\n".encode()


class TimedWire(Wire):
    def __init__(self, events):
        super().__init__(phases=False)
        self.events = deque(events)
        self.ready_at = None
        self.sent = []

    def write(self, data):
        self.sent.append((data, self.now))
        count = super().write(data)
        if data == b"e":
            self.ready_at = self.now
        if data == b"s":
            frame = b"".join(self.queue)
            self.queue.clear()
            chunk = b""
            for line in frame.splitlines(keepends=True):
                if len(chunk) + len(line) > 4096:
                    self.queue.append(chunk)
                    chunk = b""
                chunk += line
            if chunk:
                self.queue.append(chunk)
        return count

    def read(self, size=4096):
        if self.running and self.ready_at is not None and self.events:
            if self.now + 0.1 - self.ready_at >= self.events[0][0]:
                self.now += 0.1
                return self.events.popleft()[1]
        return super().read(size)


def schedule(photo=68):
    return [
        (15, b"DERBY DEMO START 5000,0,0,100\n"),
        (16, scene("gate")),
        (18, scene("race")),
        (photo, scene("photo")),
        (90, scene("res")),
        (91, b"DERBY DEMO END 5000,0,0,100\n"),
    ]


def run(wire, deadline=180):
    reader = Reader(wire, lambda _: None, lambda: wire.now, deadline)
    image, code = derby_smoke(reader)
    return image, code, reader.derby_observation(), reader


def test_first_photo_at_68_seconds_is_captured_without_45_second_fallback():
    wire = TimedWire(schedule())
    image, code, report, reader = run(wire)
    assert image.png and report.image_acquired and report.demo_cycle_observed
    assert code == "derby_demo_cycle_observed_render_buffer_lcd_unverified"
    assert report.photo_context_correlation == "same_observed_phase_race_seed"
    assert report.photo_pixels_verified is False
    assert reader.deadline == 180
    shot = [time for key, time in wire.sent if key == b"s"]
    assert len(shot) == 1 and 68 <= shot[0] - wire.ready_at < 69
    assert wire.writes.count(b"q") == 1 and wire.now < 180
    events = {event.kind: event for event in report.events if event.kind != "scene"}
    assert events["photo_observed"].race == "1000001"
    assert events["photo_observed"].seed == "AB123456"
    assert (
        events["photo_observed"].at_ms
        <= events["capture_s_requested"].at_ms
        <= events["capture_s_sent"].at_ms
        <= events["capture_begin"].at_ms
        <= events["capture_end"].at_ms
        <= events["exit_requested"].at_ms
    )


@pytest.mark.parametrize("deadline,latest", [(180, 127), (100, 66)])
def test_no_photo_exits_without_s_with_capture_exit_and_margin_reserved(deadline, latest):
    wire = TimedWire(schedule()[:3])
    image, code, report, reader = run(wire, deadline)
    assert image is None and code == "derby_photo_not_verified_no_capture"
    assert report.no_capture_completed() and not report.demo_cycle_observed
    assert report.capture_reason == "photo_not_observed_before_deadline"
    assert b"s" not in wire.writes and wire.writes[-1] == b"q"
    assert wire.sent[-1][1] < latest and wire.sent[-1][1] <= deadline - 34
    assert reader.deadline == deadline


@pytest.mark.parametrize(
    "changed", [scene("res"), scene("photo", race="1000002"), scene("photo", seed="AAAAAAAA")]
)
def test_phase_race_or_seed_change_during_capture_does_not_attest_photo(changed):
    events = schedule()
    events.insert(4, (69, changed))
    wire = TimedWire(events)
    image, code, report, _ = run(wire)
    assert image.png and report.image_acquired
    assert report.photo_context_correlation == "changed_or_incomplete"
    assert report.photo_pixels_verified is False
    assert code == "derby_image_observed_demo_cycle_or_photo_phase_not_verified"
    assert wire.writes.count(b"s") == wire.writes.count(b"q") == 1


def test_invalid_frame_records_received_begin_and_s_but_no_completed_image():
    wire = TimedWire(schedule())
    write = wire.write

    def invalid_write(data):
        count = write(data)
        if data == b"s":
            wire.queue[0] = wire.queue[0].replace(b"PIX 1 ", b"PIX 0 ")
        return count

    wire.write = invalid_write
    reader = Reader(wire, lambda _: None, lambda: wire.now, 180)
    with pytest.raises(SerialFailure) as failure:
        derby_smoke(reader)
    report = failure.value.diagnostic["derby_observation"]
    kinds = [e["kind"] for e in report["events"]]
    assert "capture_begin" in kinds and "capture_s_sent" in kinds
    assert "capture_end" not in kinds and not report["image_acquired"]
    assert wire.writes.count(b"s") == 1 and b"q" not in wire.writes


async def test_legacy_fallback_plan_cannot_open_or_execute_new_behavior():
    wire = Wire()
    config = SerialSettings(
        selector={"vid": 1, "pid": 2, "serial_number": "synthetic"},
        expected_firmware_sha256="a" * 64,
    )
    backend = SerialBackend(
        config,
        inventory=lambda: [PortInfo(port="COM42", vid=1, pid=2, serial_number="synthetic")],
        factory=lambda: wire,
        clock=lambda: wire.now,
    )
    old = deepcopy({"test": Action.DERBY.value, **backend.prepare(Action.DERBY)})
    old["smoke"]["capture"] = "one_s_at_photo_or_45_second_fallback"
    with pytest.raises(SerialFailure):
        await backend.execute_smoke(old, lambda _: None, lambda: True)
    assert not wire.is_open and not wire.writes


async def test_waiting_for_late_photo_does_not_extend_or_bypass_live_authority():
    wire = TimedWire(schedule())
    config = SerialSettings(
        selector={"vid": 1, "pid": 2, "serial_number": "synthetic"},
        expected_firmware_sha256="a" * 64,
    )
    backend = SerialBackend(
        config,
        inventory=lambda: [PortInfo(port="COM42", vid=1, pid=2, serial_number="synthetic")],
        factory=lambda: wire,
        clock=lambda: wire.now,
    )
    plan = {"test": Action.DERBY.value, **backend.prepare(Action.DERBY)}
    with pytest.raises(SerialFailure):
        await backend.execute_smoke(plan, lambda _: None, lambda: wire.now < 55)
    assert wire.closed and backend.active == 0
    assert b"s" not in wire.writes and b"q" not in wire.writes


def test_trace_is_bounded_and_contains_no_unrecognized_console_text():
    events = schedule()
    events[1:1] = [(15.2 + i * 0.1, scene("pad")) for i in range(75)]
    events.insert(1, (15.1, b"SYNTHETIC_PRIVATE_CONSOLE_TEXT\n"))
    # Keep the synthetic stream in physical time order.
    events.sort(key=lambda item: item[0])
    _, _, report, _ = run(TimedWire(events))
    assert len(report.events) == 64 and report.events_truncated
    assert not report.demo_cycle_observed
    assert report.photo_context_correlation != "same_observed_phase_race_seed"
    assert "SYNTHETIC_PRIVATE_CONSOLE_TEXT" not in report.model_dump_json()


def test_silence_after_s_still_expires_capture_within_18_seconds_no_retry():
    wire = TimedWire(schedule())
    write = wire.write

    def silent_write(data):
        count = write(data)
        if data == b"s":
            wire.queue.clear()
            wire.events.clear()
        return count

    wire.write = silent_write
    reader = Reader(wire, lambda _: None, lambda: wire.now, 180)
    with pytest.raises(SerialFailure) as failure:
        derby_smoke(reader)
    sent_at = next(time for key, time in wire.sent if key == b"s")
    assert wire.now - sent_at <= 18.2 and reader.deadline == 180
    assert wire.writes.count(b"s") == 1 and b"q" not in wire.writes
    assert failure.value.diagnostic["derby_observation"]["image_acquired"] is False


@pytest.mark.parametrize("marker", [b"APP_STOPPED\n", b"HOME_READY\n"])
def test_unexpected_teardown_during_frame_fails_without_any_new_q(marker):
    events = schedule()
    events.insert(4, (69, marker))
    wire = TimedWire(events)
    with pytest.raises(SerialFailure):
        run(wire)
    assert wire.writes.count(b"s") == 1 and b"q" not in wire.writes


@pytest.mark.parametrize("failure_kind", ["transport", "revoked_before_s"])
async def test_direct_s_write_failure_preserves_timing_and_still_closes(failure_kind):
    wire = TimedWire(schedule())
    valid = [True]
    attempted_s = []
    original_write = wire.write

    def failed_write(data):
        if data == b"s":
            attempted_s.append(True)
            raise OSError("SYNTHETIC_PRIVATE_WRITE_ERROR")
        return original_write(data)

    class RevokeS(SmokePort):
        def write(self, data):
            if data == b"s":
                attempted_s.append(True)
                valid[0] = False
            return super().write(data)

    if failure_kind == "transport":
        wire.write = failed_write
        port = wire
    else:
        port = RevokeS(wire, lambda: valid[0], threading.Event())
    config = SerialSettings(
        selector={"vid": 1, "pid": 2, "serial_number": "synthetic"},
        expected_firmware_sha256="a" * 64,
    )
    backend = SerialBackend(
        config,
        inventory=lambda: [PortInfo(port="COM42", vid=1, pid=2, serial_number="synthetic")],
        factory=lambda: port,
        clock=lambda: wire.now,
    )
    plan = {"test": Action.DERBY.value, **backend.prepare(Action.DERBY)}
    with pytest.raises(SerialFailure) as failure:
        await backend.execute_smoke(plan, lambda _: None, lambda: valid[0])
    assert failure.value.code == (
        "serial_write_failed"
        if failure_kind == "transport"
        else "operation_interrupted_review_required"
    )
    report = failure.value.diagnostic["derby_observation"]
    kinds = [event["kind"] for event in report["events"]]
    assert "photo_observed" in kinds and "capture_s_requested" in kinds
    assert "capture_s_sent" not in kinds and "capture_begin" not in kinds
    assert report["capture_reason"] == "capture_incomplete" and not report["image_acquired"]
    assert failure.value.diagnostic["receive_summary"]["command_write_completed"] is False
    assert len(attempted_s) == 1 and b"s" not in wire.writes and b"q" not in wire.writes
    assert wire.closed and backend.active == 0 and backend.hardware_connected is False
    assert "SYNTHETIC_PRIVATE_WRITE_ERROR" not in str(failure.value.diagnostic)


@pytest.mark.parametrize(
    "interruption", ["authority", "stop", "os_read_error", "non_os_read_error"]
)
async def test_interruption_before_photo_cutoff_does_not_report_a_deadline(interruption):
    wire = TimedWire(schedule())
    valid = [True]
    read = wire.read
    config = SerialSettings(
        selector={"vid": 1, "pid": 2, "serial_number": "synthetic"},
        expected_firmware_sha256="a" * 64,
    )
    backend = SerialBackend(
        config,
        inventory=lambda: [PortInfo(port="COM42", vid=1, pid=2, serial_number="synthetic")],
        factory=lambda: wire,
        clock=lambda: wire.now,
    )

    def interrupted_read(size=4096):
        if wire.running and wire.now - wire.ready_at >= 23:
            if interruption == "authority":
                valid[0] = False
            elif interruption == "stop":
                backend.smoke_stop.set()
            elif interruption == "os_read_error":
                raise OSError("SYNTHETIC_PRIVATE_READ_ERROR")
            else:
                raise RuntimeError("SYNTHETIC_PRIVATE_READ_ERROR")
        return read(size)

    wire.read = interrupted_read
    plan = {"test": Action.DERBY.value, **backend.prepare(Action.DERBY)}
    with pytest.raises(SerialFailure) as caught:
        await backend.execute_smoke(plan, lambda _: None, lambda: valid[0])
    assert caught.value.code == (
        "serial_read_failed"
        if interruption == "os_read_error"
        else "serial_operation_uncertain"
        if interruption == "non_os_read_error"
        else "operation_interrupted_review_required"
    )
    assert caught.value.diagnostic["failure_origin"] == (
        "port_read_os_error"
        if interruption == "os_read_error"
        else "port_read_error"
        if interruption == "non_os_read_error"
        else "authority_or_stop_check"
    )
    report = DerbyObservation.model_validate_json(
        json.dumps(caught.value.diagnostic["derby_observation"])
    )
    assert wire.now - wire.ready_at < 120
    assert report.capture_reason == "observation_incomplete"
    assert not report.image_acquired and not report.exit_home_observed
    assert not report.model_copy(update={"exit_home_observed": True}).no_capture_completed()
    assert all(event.reason != "photo_not_observed_before_deadline" for event in report.events)
    assert b"s" not in wire.writes and b"q" not in wire.writes
    assert wire.closed and backend.active == 0 and not backend.hardware_connected
    assert not backend.ownership_uncertain
    assert "SYNTHETIC_PRIVATE" not in str(caught.value.diagnostic)
