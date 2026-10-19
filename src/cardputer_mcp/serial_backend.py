"""Opt-in fixed serial operations. No flash, upload, or raw command API.

A single dedicated worker thread opens, reads, writes and closes the port for one
requested job. All consumers receive sanitized events from that one reader.
"""

import asyncio
import hashlib
import json
import os
import re
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import replace
from typing import Any, Protocol

from .backend import BackendResult, DeviceOperationFailure
from .capture import CapturedImage, CaptureError, LegacyCaptureDecoder
from .config import SerialSettings
from .derby_fixed import FIXED_PLAN, FixedCollector, FixedTrial
from .derby_observation import DerbyObservation
from .derby_telemetry import DerbyDiagnosticsCollector
from .device_faults import fault_category
from .device_observation import MAX_RECEIVE_BYTES, DeviceLogCollector, DeviceObservation
from .home_line import HomeLineProbe
from .models import TestName
from .ports import PortInfo, enumerate_ports, resolve_port
from .serial_diagnostics import ReceiveDiagnostics

ANSI = re.compile(rb"\x1b\[[0-?]*[ -/]*[@-~]")
PREFIX = re.compile(rb"^[IWEVD] \([^\r\n)]*\) [A-Za-z0-9_.-]+: ")
FINAL_MEMORY = re.compile(rb"MEM free=(\d+) largest=(\d+) js=(\d+) frames=(\d+)")
KNOWN = {
    b"HOME_READY": "serial_home_ready",
    b"CATEGORY 0": "serial_category_apps",
    b"APP 0": "serial_row_hello",
    b"APP_ID local.hello": "serial_identity_hello",
    b"HELLO_READY": "serial_hello_ready",
    b"HELLO_COUNT 1": "serial_hello_count_one",
    b"HELLO_FRAME_PRESENTED": "serial_frame_presented",
    b"KASANE_FRAME_PRESENTED": "serial_frame_presented",
    b"APP_STOPPED": "serial_app_stopped",
}


class SerialPort(Protocol):
    dtr: bool
    rts: bool
    port: str | None
    is_open: bool

    def open(self) -> None: ...
    def close(self) -> None: ...
    def read(self, size: int = 1) -> bytes: ...
    def write(self, data: bytes) -> int | None: ...
    def reset_input_buffer(self) -> None: ...


class SerialFailure(DeviceOperationFailure):
    """Fixed-category failure; unknown device outcome always requires review."""

    fixed_trial: FixedTrial | None = None


def make_serial() -> SerialPort:
    # Delayed until an requested job. Construct unopened, suppress control signals
    # before open. Drivers may still glitch DTR/RTS; this is not a no-reset guarantee.
    import serial

    return serial.Serial(
        port=None,
        baudrate=115200,
        timeout=0.05,
        write_timeout=1,
        exclusive=True if os.name != "nt" else None,
    )


def fingerprint(port: PortInfo) -> str:
    identity = port.model_dump(exclude={"description"})
    return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()


def stable_usb_fingerprint(port: PortInfo) -> str:
    """Reconnect identity includes USB location, even when it is unavailable."""
    identity = port.model_dump(exclude={"description", "port"})
    return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()


def require_restart_identity(port: PortInfo, inventory: list[PortInfo]) -> None:
    """Only a uniquely identified native ESP32-S3 Serial/JTAG interface may reset."""
    if (
        port.vid != 0x303A
        or port.pid != 0x1001
        or not port.serial_number
        or not port.serial_number.strip()
    ):
        raise ValueError("serial_restart_unsupported")
    matching = [
        candidate
        for candidate in inventory
        if (candidate.vid, candidate.pid, candidate.serial_number)
        == (port.vid, port.pid, port.serial_number)
    ]
    if len(matching) != 1:
        raise ValueError("device_ambiguous")


def clean_line(line: bytes) -> bytes:
    return PREFIX.sub(b"", ANSI.sub(b"", line).rstrip(b"\r"), count=1)


class Reader:
    """The only serial reader. Bounded framing retains fragmented/coalesced markers."""

    def __init__(
        self,
        port: SerialPort,
        emit: Callable[[str], None],
        clock: Callable[[], float],
        deadline: float,
        local_home_probe: HomeLineProbe | None = None,
    ) -> None:
        self.port, self.emit, self.clock, self.deadline = port, emit, clock, deadline
        self.phase_observer: Callable[[bytes], None] | None = None
        self.wire_observer: Callable[[bytes], None] | None = None
        self.fixed_collector: FixedCollector | None = None
        self.derby_observation: Callable[[], DerbyObservation] | None = None
        self.derby_diagnostics: DerbyDiagnosticsCollector | None = None
        self.derby_mode = False
        self.pending = b""
        self.lines: list[bytes] = []
        self.bytes_read = 0
        self.fault_tail = b""
        self.step = "operation"
        self.expected_marker: str | None = None
        self.recognized_markers = 0
        self.capture_guard = False
        self.capture_decoder: LegacyCaptureDecoder | None = None
        self.receive_diagnostics = ReceiveDiagnostics()
        self.diagnostic_pending_bytes = 0
        self.local_home_probe = local_home_probe

    def failure(self, code: str) -> SerialFailure:
        failure = SerialFailure(
            code,
            step=self.step,
            expected_marker=self.expected_marker,
            received_bytes=self.bytes_read,
            received_lines=len(self.lines),
            recognized_markers=self.recognized_markers,
            receive_summary=self.receive_diagnostics.snapshot(self.diagnostic_pending_bytes),
        )
        if self.fixed_collector is not None:
            failure.diagnostic["workload_end_observed"] = self.fixed_collector.end_seen
            failure.diagnostic["owner_exit_observed"] = self.fixed_collector.exit_seen
            failure.diagnostic["app_stopped_observed"] = self.fixed_collector.stopped
            failure.diagnostic["home_ready_observed"] = self.fixed_collector.home
            failure.diagnostic["partial_workload_is_terminal_success"] = False
        if self.derby_observation is not None:
            failure.diagnostic["derby_observation"] = self.derby_observation().model_dump(
                mode="json", exclude_none=True
            )
        return failure

    def read_once(self) -> bool:
        if self.clock() >= self.deadline:
            raise self.failure("serial_deadline")
        try:
            incoming = self.port.read(4096)
        except SerialFailure:
            # Owned-port guards already carry a fixed category and diagnostics.
            raise
        except OSError:
            failure = self.failure("serial_read_failed")
            failure.diagnostic["failure_origin"] = "port_read_os_error"
            raise failure from None
        except Exception:
            failure = self.failure("serial_operation_uncertain")
            failure.diagnostic["failure_origin"] = "port_read_error"
            raise failure from None
        if incoming:
            self.bytes_read += len(incoming)
            if self.wire_observer is not None:
                try:
                    self.wire_observer(incoming)
                except ValueError:
                    raise self.failure("derby_fixed_trial_invalid") from None
            self.receive_diagnostics.observe_bytes(incoming)
            # Count returned bytes even if their read exceeded the deadline.
            # Classify framing before a fault or invalid frame rejects a chunk;
            # only the existing bounded protocol buffer is used temporarily.
            # Diagnostics retain counters and tail length, never another raw copy.
            diagnostic_lines = (self.pending + incoming).split(b"\n")
            self.diagnostic_pending_bytes = len(diagnostic_lines[-1])
            for diagnostic_line in diagnostic_lines[:-1]:
                self.receive_diagnostics.observe_line(diagnostic_line)
                if self.derby_diagnostics is not None:
                    # Feed exact complete wire lines before normalization and
                    # fatal checks. The collector retains only bounded numbers
                    # and fixed categories, never console text or frame pixels.
                    self.derby_diagnostics.feed(diagnostic_line)
                if self.phase_observer is not None:
                    # Record bounded host-observed markers in wire order, even
                    # if a subsequent frame record fails decoder validation.
                    self.phase_observer(clean_line(diagnostic_line))
                if self.local_home_probe is not None:
                    self.local_home_probe.observe_line(diagnostic_line)
        if self.capture_guard and self.clock() >= self.deadline:
            raise self.failure("serial_capture_timeout")
        if not incoming:
            return False
        if self.bytes_read > 256 * 1024:
            raise self.failure("serial_capture_limit")
        cleaned = ANSI.sub(b"", self.fault_tail + incoming)
        category = fault_category(cleaned, fixed_workload=self.fixed_collector is not None)
        if category is not None or (
            self.derby_mode and re.search(rb"DEMOFAIL|FRAMEFAIL|LOADFAIL|DEGRADE|app: OOM", cleaned)
        ):
            if self.derby_diagnostics is not None and self.diagnostic_pending_bytes:
                self.derby_diagnostics.mark_partial_line_or_fault()
            failure = self.failure("serial_fault_marker")
            failure.diagnostic["device_fault_category"] = category or "derby_runtime_fault"
            raise failure
        self.fault_tail = cleaned[-256:]
        capture_finished = self.capture_decoder is not None and self.capture_decoder.complete
        if self.capture_decoder is not None and not capture_finished:
            try:
                self.capture_decoder.feed(incoming)
            except CaptureError:
                raise self.failure("serial_capture_invalid") from None
        self.pending += incoming
        if len(self.pending) > 16384:
            raise self.failure("serial_line_limit")
        while b"\n" in self.pending:
            raw, self.pending = self.pending.split(b"\n", 1)
            line = clean_line(raw)
            if self.capture_guard:
                if line.startswith(b"APP_ID"):
                    raise self.failure("serial_capture_unexpected_app")
                if line.startswith((b"CAPTURE_", b"PIX")):
                    # Wire records are bare printf lines in the pinned firmware.
                    # The decoder must not ignore a prefixed second frame that
                    # marker normalization would otherwise recognize after END.
                    if line != raw.rstrip(b"\r"):
                        raise self.failure("serial_capture_invalid")
                    if self.capture_decoder is None:
                        raise self.failure("serial_capture_boundary")
                    if capture_finished:
                        raise self.failure("serial_capture_invalid")
            # The capture decoder owns frame bytes. They must never enter the
            # marker history, public event cache or serial diagnostics.
            if self.capture_decoder is None:
                # Capture only needs exact allowlisted marker history. Unknown
                # lines count toward framing limits without keeping a second
                # private copy alongside the explicitly consented local probe.
                self.lines.append(line if not self.capture_guard or line in KNOWN else b"")
                if len(self.lines) > 4096:
                    raise self.failure("serial_marker_limit")
            # Only exact allowlisted markers leave the reader. Raw lines and
            # unrecognized noisy/credential-bearing data are never cached or saved.
            if event := KNOWN.get(line):
                self.recognized_markers += 1
                self.emit(event)
        return True

    def command(
        self, key: bytes, marker: bytes, *, timeout: float = 12, step: str = "command"
    ) -> list[bytes]:
        self.step = step if step in DeviceOperationFailure.STEPS else "command"
        self.expected_marker = {
            rb"HOME_READY": "HOME_READY",
            rb"CATEGORY \d+": "CATEGORY",
            rb"APP \d+": "APP_ROW",
            rb"(?:HELLO|KASANE)_FRAME_PRESENTED": "FRAME_PRESENTED",
            rb"HELLO_COUNT 1": "HELLO_COUNT_ONE",
        }.get(marker)
        capture_escape = key == b"\x1b" and marker == rb"HOME_READY" and step == "capture_home"
        derby_down = self.derby_mode and key == b"d" and marker == rb"APP \d+"
        if key not in (b"q", b"a", b"u", b"e") and not capture_escape and not derby_down:
            raise self.failure("command_not_allowlisted")
        # Drain already-buffered data through the same reader, preserving fault
        # detection. Never use reset_input_buffer to silently discard evidence.
        quiet_by = min(self.clock() + 1, self.deadline)
        while self.read_once():
            if self.clock() >= quiet_by:
                raise self.failure("serial_not_quiet_before_command")
        start = len(self.lines)
        # An old partial line cannot be accepted as the new command's acknowledgement.
        stale_partial = bool(self.pending)
        self.receive_diagnostics.begin_command(
            {
                b"\x1b": "home_escape",
                b"q": "home_quit",
                b"a": "select_apps",
                b"u": "select_previous",
                b"d": "select_next",
                b"e": "enter",
            }[key],
            cross_command_partial=stale_partial,
        )
        try:
            written = self.port.write(key)
        except Exception:
            raise self.failure("serial_write_failed") from None
        if written != len(key):
            raise self.failure("incomplete_serial_write")
        self.receive_diagnostics.write_completed()
        if capture_escape and self.local_home_probe is not None:
            self.local_home_probe.begin_window()
        self.emit("serial_wait_" + self.step)
        stop = min(self.clock() + timeout, self.deadline)
        examined = start
        pattern = re.compile(marker)
        while self.clock() < stop:
            self.read_once()
            while examined < len(self.lines):
                line = self.lines[examined]
                examined += 1
                if stale_partial:
                    stale_partial = False
                    start = examined
                    continue
                if pattern.fullmatch(line):
                    return self.lines[start:examined]
        raise self.failure("serial_marker_timeout")

    def observe(self, seconds: float) -> None:
        stop = min(self.clock() + seconds, self.deadline)
        while self.clock() < stop:
            self.read_once()

    def clear_transient(self) -> None:
        """Drop protocol buffers after this worker finishes, including on failure."""
        self.pending = b""
        self.lines.clear()
        self.phase_observer = None
        self.wire_observer = None
        self.fixed_collector = None
        self.derby_observation = None
        if self.derby_diagnostics is not None:
            self.derby_diagnostics.clear()
            self.derby_diagnostics = None
        self.fault_tail = b""
        self.capture_decoder = None
        self.local_home_probe = None

    def capture_quiet_boundary(self) -> None:
        """Drain through the sole reader, requiring a quiet and empty boundary."""
        quiet_by = min(self.clock() + 1, self.deadline)
        while self.read_once():
            if self.clock() >= quiet_by:
                raise self.failure("serial_capture_boundary")
        if self.pending:
            raise self.failure("serial_capture_boundary")


def capture_home(reader: Reader) -> CapturedImage:
    """One explicitly requested legacy HOME capture; no retry, navigation loop or replay.

    Firmware 22f6d54 main/main.c:211-225 treats q/s as text in an editor,
    but :214 and :361 map Esc to KEY_BACK. The operator must first verify HOME
    and refrain from physical input: legacy firmware has no atomic capture op.
    https://github.com/dj-oyu/cardputer-adv-pocketjs/blob/22f6d54a5c2fad6f7f40a8d5aa7cea7d7f371c51/main/main.c#L211-L225
    https://github.com/dj-oyu/cardputer-adv-pocketjs/blob/22f6d54a5c2fad6f7f40a8d5aa7cea7d7f371c51/main/main.c#L360-L362
    https://github.com/dj-oyu/cardputer-adv-pocketjs/blob/22f6d54a5c2fad6f7f40a8d5aa7cea7d7f371c51/main/hal/board.c#L419-L439
    """
    reader.step = "capture_home"
    reader.expected_marker = "HOME_READY"
    reader.capture_guard = True
    try:
        reader.capture_quiet_boundary()
        # Esc is intentionally fixed, and used only for this exact HOME marker.
        # A modal/editor that does not reach HOME is a failure, never another Esc.
        try:
            reader.command(b"\x1b", rb"HOME_READY", step="capture_home")
        finally:
            if reader.local_home_probe is not None:
                reader.local_home_probe.end_window()
        reader.capture_quiet_boundary()
        reader.capture_decoder = LegacyCaptureDecoder()
        reader.expected_marker = "CAPTURE_END"
        reader.receive_diagnostics.begin_command("capture_frame")
        try:
            written = reader.port.write(b"s")
        except Exception:
            raise reader.failure("serial_write_failed") from None
        if written != 1:
            raise reader.failure("incomplete_serial_write")
        reader.receive_diagnostics.write_completed()
        reader.emit("serial_wait_capture_home")
        while not reader.capture_decoder.complete:
            reader.read_once()
        # Includes bytes coalesced after END and one bounded quiet drain. A new
        # APP_ID, second frame, partial record or late fault cannot pass as success.
        reader.capture_quiet_boundary()
        try:
            return reader.capture_decoder.finish()
        except CaptureError:
            raise reader.failure("serial_capture_invalid") from None
    except SerialFailure as error:
        if error.code in ("serial_deadline", "serial_marker_timeout"):
            raise reader.failure("serial_capture_timeout") from None
        raise


def hello_cycle(reader: Reader) -> None:
    reader.command(b"q", rb"HOME_READY", step="initial_home")
    category = reader.command(b"a", rb"CATEGORY \d+", step="select_apps")
    if category[-1] != b"CATEGORY 0":
        raise reader.failure("wrong_category")
    previous: int | None = None
    for _ in range(14):
        row = reader.command(b"u", rb"APP \d+", step="select_hello")
        current = int(row[-1].split()[1])
        if current >= 14 or (previous is not None and current != max(0, previous - 1)):
            raise reader.failure("wrong_app_navigation")
        previous = current
    if previous != 0:
        raise reader.failure("hello_not_selected")
    launch = reader.command(
        b"e", rb"(?:HELLO|KASANE)_FRAME_PRESENTED", timeout=20, step="launch_hello"
    )
    if [line for line in launch if line.startswith(b"APP_ID")] != [b"APP_ID local.hello"]:
        raise reader.failure("wrong_app_identity")
    ready = [index for index, line in enumerate(launch) if line == b"HELLO_READY"]
    if len(ready) != 1 or launch.index(b"APP_ID local.hello") >= ready[0]:
        raise reader.failure("hello_not_ready_or_out_of_order")
    reader.command(b"e", rb"HELLO_COUNT 1", step="count_hello")
    stop_start = len(reader.lines)
    reader.command(b"q", rb"HOME_READY", step="stop_hello")
    reader.step = "settle"
    reader.expected_marker = "TEARDOWN"
    reader.observe(0.3)
    stopped = reader.lines[stop_start:]
    memories = [
        (index, FINAL_MEMORY.fullmatch(line))
        for index, line in enumerate(stopped)
        if line.startswith(b"MEM ")
    ]
    stops = [index for index, line in enumerate(stopped) if line == b"APP_STOPPED"]
    homes = [index for index, line in enumerate(stopped) if line == b"HOME_READY"]
    if len(memories) != 1 or len(stops) != 1 or len(homes) != 1:
        raise reader.failure("teardown_sequence_missing")
    index, match = memories[0]
    if match is None or not index < stops[0] < homes[0]:
        raise reader.failure("teardown_sequence_invalid")
    free, largest, js, frames = map(int, match.groups())
    if not 0 < largest <= free or js != 0 or frames < 1:
        raise reader.failure("teardown_memory_invalid")
    if ANSI.sub(b"", reader.pending).strip():
        raise reader.failure("incomplete_final_line")
    identities = [line for line in reader.lines if line.startswith(b"APP_ID")]
    if identities != [b"APP_ID local.hello"]:
        raise reader.failure("unexpected_app_launch")


FIRMWARE_OBSERVE_SECONDS = 30
RESTART_METHOD = "esp32s3_usb_serial_jtag_rts_v1"
RESTART_HOLD_SECONDS = 0.1
RECONNECT_SECONDS = 8


def observe_firmware(port: SerialPort, clock: Callable[[], float]) -> DeviceObservation:
    """Collect passive device text without interpreting it as protocol commands.

    Panic/fault strings and legacy markers are observations, not reasons to
    suppress console evidence. Only the separate bounded collector retains data.
    The serial read timeout bounds the final in-flight read; bytes returned at
    or after the deadline are discarded and no further read is started.
    """
    try:
        collector = DeviceLogCollector()
        deadline = clock() + FIRMWARE_OBSERVE_SECONDS
        received = 0
        while clock() < deadline and received < MAX_RECEIVE_BYTES:
            requested = min(4096, MAX_RECEIVE_BYTES - received)
            try:
                incoming = port.read(requested)
            except Exception:
                raise SerialFailure("serial_read_failed", step="firmware_observe") from None
            if type(incoming) is not bytes or len(incoming) > requested:
                raise SerialFailure("serial_read_failed", step="firmware_observe")
            # Bound bytes received from the driver, independently of how many
            # bytes the collector accepts before any of its tighter limits.
            received += len(incoming)
            if clock() >= deadline:
                break
            if incoming and not collector.feed(incoming):
                break
        return collector.finish()
    except SerialFailure:
        raise
    except Exception:
        # A malformed return or parser failure must use the same fixed failure
        # path, which clears exception/read-frame references before close waits.
        raise SerialFailure("serial_operation_uncertain", step="firmware_observe") from None


class SerialBackend:
    name = "serial"
    device = "configured-cardputer"

    def __init__(
        self,
        config: SerialSettings,
        *,
        inventory: Callable[[], Iterable[PortInfo]] = enumerate_ports,
        factory: Callable[[], SerialPort] = make_serial,
        clock: Callable[[], float] = time.monotonic,
        pause: Callable[[float], None] = time.sleep,
    ) -> None:
        self.config, self.inventory, self.factory, self.clock = config, inventory, factory, clock
        self.pause = pause
        self.active = self.max_active = self.calls = 0
        self.hardware_connected = False
        self.ownership_uncertain = False
        self._unclosed_port: SerialPort | None = None
        self.last_closed_plan: dict[str, Any] | None = None
        self.smoke_stop = threading.Event()

    def prepare(self, test: TestName) -> dict[str, Any]:
        if test not in (
            TestName.OBSERVE,
            TestName.FIRMWARE,
            TestName.RESTART,
            TestName.HELLO,
            TestName.CAPTURE,
            TestName.DERBY,
            TestName.DERBY_FIXED,
        ):
            raise ValueError("test_not_available_for_backend")
        if (
            test in (TestName.HELLO, TestName.CAPTURE, TestName.DERBY, TestName.DERBY_FIXED)
            and self.config.expected_firmware_sha256 is None
        ):
            raise ValueError("expected_firmware_required")
        inventory = list(self.inventory())
        port = resolve_port(self.config.selector, inventory)
        if test == TestName.RESTART:
            require_restart_identity(port, inventory)
        effects = [
            "Opens and closes USB serial; drivers may glitch control lines or reset device",
            "Exits the current app, selects HELLO, launches once, presses Enter and exits"
            if test == TestName.HELLO
            else "Receives serial bytes without sending commands",
            "No reset command, firmware flash, upload, GRID run or screenshot",
        ]
        if test == TestName.CAPTURE:
            effects = [
                "Operator must verify device is already HOME and firmware is correct; "
                "do not use physical inputs until this job finishes",
                "Opens and closes USB serial; drivers may glitch control lines or reset device",
                "Sends one fixed Esc, requires fresh HOME_READY, then sends one s; "
                "a wrong initial screen can exit an app/editor and lose unsaved state",
                "Forces HOME redraw and transmits rendered pixels, which may contain private data",
                "One frame only, 18-second reader budget, no automatic retry",
                "Render-buffer capture does not prove LCD transfer or coherent snapshot",
                "No reset command, firmware flash, upload or GRID run",
            ]
        if test == TestName.DERBY:
            effects = [
                "Operator verifies HOME and exact normal firmware; no physical input during job",
                "Opens/closes serial; drivers may glitch control lines or reset device",
                "Fixed Esc/a/u x16/d x12/e navigation requires local.derby and DERBY READY",
                "Observe automatic demo up to 120 seconds; whole serial job at most 180 seconds",
                "One s only at a currently observed photo phase; no timed fallback; "
                "no photo before the deadline means no image and an unverified photo result",
                "Reserve 18 seconds for capture, 12 for exit and 5 seconds margin; "
                "record bounded host phase/race/seed and capture timing; pixels may be private",
                "One explicit q exit requiring fresh HOME_READY; game exit may save player state",
                "Cancellation/lease loss stops new writes, including q; game may remain running",
                "Render buffer does not prove LCD transfer; "
                "missing phases do not prove a full cycle",
                "No keytest build, firmware flash, arbitrary keys, retries, or automatic reopen",
            ]
        if test == TestName.DERBY_FIXED:
            effects = [
                "Operator verifies HOME and the exact fixed-workload firmware; no physical input",
                "Opens/closes the owned serial port; control lines may reset the device",
                "Fixed Esc/a/u x16/d x12/e navigation launches local.derby once",
                "Collects at most 64 KiB for 200 seconds with 1024-byte lines and no retry",
                "Requires three ordered repetitions, END, matching owner EXIT, "
                "APP_STOPPED and HOME_READY",
                "Stores raw transport privately for the requesting principal with its SHA-256",
                "Host checks transport and known work; device quality fields are not a receipt",
                "Cancellation or lease loss stops I/O; no automatic reopen or flash operation",
            ]
        if test == TestName.FIRMWARE:
            effects = [
                "Opens and closes USB serial; drivers may glitch control lines or reset device",
                "Receives device console text for at most 30 seconds and 64 KiB; "
                "no commands, reset command, firmware flash or upload; no automatic reopen",
                "Shares a bounded redacted console report and reported firmware metadata "
                "with the authenticated recipient named in the broker plan",
                "Known secret patterns are redacted; redaction is not guaranteed complete",
                "Report remains in broker RAM for at most 5 minutes; no raw log persistence",
                "Device-reported firmware metadata is unverified and does not attest the image",
            ]
        if test == TestName.RESTART:
            effects = [
                "Only the selected ESP32-S3 native USB Serial/JTAG interface "
                "(VID 303A, PID 1001, unique USB serial number) may be restarted",
                "Opening or closing serial may glitch control lines or cause "
                "additional unintended resets, including during the optional reopen",
                "One hardware reset can exit the current app and lose unsaved app state; "
                "one 100 ms RTS pulse with DTR cleared; no reset retry",
                "Discards the pre-reset input buffer once; sends no serial command bytes, "
                "bootloader commands, firmware flash or upload",
                "Receives device console text for at most 30 seconds and 64 KiB, "
                "including any reconnect wait",
                "If disconnected, waits at most 8 seconds after reset and reopens at most once; "
                "requires the same VID, PID, USB serial number and USB location, including None",
                "Early boot logs can be lost during USB re-enumeration; control-line completion "
                "does not prove application restart or successful boot, "
                "including download-mode straps",
                "Shares a bounded redacted console report and reported firmware metadata "
                "with the authenticated recipient named in the broker plan",
                "Known secret patterns are redacted; redaction is not guaranteed complete",
                "Report remains in broker RAM for at most 5 minutes; no raw log persistence",
                "Device-reported firmware metadata is unverified and does not attest the image",
            ]
        protocol: str = self.config.protocol
        verification = "configured_expected_hash_only"
        observe_seconds = self.config.observe_seconds if test == TestName.OBSERVE else None
        if test == TestName.DERBY:
            protocol = "pocketjs-derby-smoke-v1"
        elif test == TestName.DERBY_FIXED:
            protocol = "pocketjs-derby-fixed-v2"
        elif test == TestName.CAPTURE:
            protocol = "pocketjs-legacy-capture-v1"
        elif test in (TestName.FIRMWARE, TestName.RESTART):
            protocol = "esp-idf-startup-log-v1"
            verification = "unverified_device_report"
            observe_seconds = FIRMWARE_OBSERVE_SECONDS
        details: dict[str, Any] = {
            "stages": ["serial_open", test.value, "serial_close"],
            "port": port.port,
            "device_fingerprint": fingerprint(port),
            "protocol": protocol,
            "expected_firmware_sha256": self.config.expected_firmware_sha256,
            "firmware_verification": verification,
            "observe_seconds": observe_seconds,
            "effects": effects,
        }
        if test == TestName.DERBY:
            from .derby_smoke import SMOKE

            details["smoke"] = SMOKE
        if test == TestName.DERBY_FIXED:
            details["fixed_workload"] = dict(FIXED_PLAN)
        if test == TestName.RESTART:
            details["restart"] = {
                "method": RESTART_METHOD,
                "attempts": 1,
                "rts_hold_milliseconds": 100,
                "reconnect_seconds": RECONNECT_SECONDS,
                "max_reopens": 1,
                "stable_usb_fingerprint": stable_usb_fingerprint(port),
                "buffer_policy": "discard_before_reset",
            }
        return details

    async def execute(
        self, plan: dict[str, Any], stage: Callable[[str], None]
    ) -> str | BackendResult:
        return await self._execute(plan, stage)

    def request_smoke_stop(self) -> bool:
        self.smoke_stop.set()
        return bool(self.active)

    async def execute_smoke(
        self, plan: dict[str, Any], stage: Callable[[str], None], live: Callable[[], bool]
    ) -> str | BackendResult:
        self.smoke_stop.clear()
        return await self._execute(plan, stage, live=live)

    async def execute_with_local_probe(
        self, plan: dict[str, Any], stage: Callable[[str], None], probe: HomeLineProbe
    ) -> str | BackendResult:
        """Retired approval opt-in: never open USB or collect a raw HOME line."""
        probe.clear()
        raise SerialFailure("command_not_allowlisted")

    async def _execute(
        self,
        plan: dict[str, Any],
        stage: Callable[[str], None],
        local_home_probe: HomeLineProbe | None = None,
        live: Callable[[], bool] | None = None,
    ) -> str | BackendResult:
        if self.active:
            raise SerialFailure("multiple_device_owners")
        self.active += 1
        self.calls += 1
        self.max_active = max(self.max_active, self.active)
        loop = asyncio.get_running_loop()

        def emit(value: str) -> None:
            loop.call_soon_threadsafe(stage, value)

        async def check_on_loop() -> bool:
            return live is not None and live() is True

        def worker_live() -> bool:
            return asyncio.run_coroutine_threadsafe(check_on_loop(), loop).result(timeout=5)

        worker = loop.run_in_executor(
            None,
            self._execute_sync,
            plan,
            emit,
            local_home_probe,
            worker_live if live is not None else None,
        )
        interrupted = False
        try:
            # asyncio cancellation cannot stop an OS serial read/thread. Wait for
            # close to finish before returning, even when the caller is cancelled.
            while not worker.done():
                try:
                    await asyncio.shield(worker)
                except asyncio.CancelledError:
                    interrupted = True
                    if plan["test"] in (TestName.DERBY.value, TestName.DERBY_FIXED.value):
                        self.smoke_stop.set()
                except Exception:
                    # Retrieve/rethrow the worker's exception after its queued
                    # stage notifications have crossed the same loop barrier.
                    if not worker.done():
                        raise
            # A fast executor can already be done before the first await. Its
            # call_soon_threadsafe notifications must precede both the result
            # and ownership release, including failure and cancellation.
            drained: asyncio.Future[None] = loop.create_future()
            loop.call_soon(drained.set_result, None)
            while not drained.done():
                try:
                    await asyncio.shield(drained)
                except asyncio.CancelledError:
                    interrupted = True
                    if plan["test"] in (TestName.DERBY.value, TestName.DERBY_FIXED.value):
                        self.smoke_stop.set()
            result = worker.result()
            if interrupted:
                failure = SerialFailure("operation_interrupted_review_required")
                if plan["test"] == TestName.DERBY_FIXED.value and isinstance(result, BackendResult):
                    failure.fixed_trial = result.fixed_trial
                raise failure
            return result
        except SerialFailure as failure:
            # Both shield(worker) and worker.result() can raise. Drop serial
            # read-frame locals from retained executor exceptions; only fixed
            # diagnostics and the explicitly expiring probe survive.
            failure.__traceback__ = None
            failure.__context__ = None
            failure.__cause__ = None
            raise failure from None
        finally:
            if not self.ownership_uncertain:
                self.active -= 1

    def _execute_sync(
        self,
        plan: dict[str, Any],
        emit: Callable[[str], None],
        local_home_probe: HomeLineProbe | None = None,
        live: Callable[[], bool] | None = None,
    ) -> str | BackendResult:
        self.last_closed_plan = None
        test = TestName(plan["test"])
        current = self.prepare(test)
        if any(plan.get(key) != value for key, value in current.items()):
            raise SerialFailure("device_or_plan_changed")
        if test == TestName.RESTART:
            return self._execute_restart_sync(current, emit)
        port = self.factory()
        if test in (TestName.DERBY, TestName.DERBY_FIXED):
            from .smoke_port import SmokePort

            port = SmokePort(port, live, self.smoke_stop)
        reader: Reader | None = None
        operation_failure: SerialFailure | None = None
        try:
            port.dtr = False
            port.rts = False
            port.port = current["port"]
            emit("serial_open")
            try:
                port.open()
            except Exception:
                raise SerialFailure("serial_open_failed", step="open_serial") from None
            self.hardware_connected = True
            # Narrow the enumeration/open race. USB identity is not cryptographic.
            try:
                identity = resolve_port(self.config.selector, self.inventory())
            except ValueError:
                raise SerialFailure("device_identity_changed", step="validate_identity") from None
            if fingerprint(identity) != current["device_fingerprint"]:
                raise SerialFailure("device_identity_changed", step="validate_identity")
            if test == TestName.FIRMWARE:
                emit(test.value)
                observation = observe_firmware(port, self.clock)
                # The existing finally block must confirm close before the
                # broker can receive any report, including an empty observation.
                return BackendResult(
                    "firmware_observation_completed_no_commands", observation=observation
                )
            reader = Reader(
                port,
                emit,
                self.clock,
                self.clock()
                + (
                    18
                    if test == TestName.CAPTURE
                    else 180
                    if test == TestName.DERBY
                    else 200
                    if test == TestName.DERBY_FIXED
                    else 90
                ),
                local_home_probe,
            )
            emit(test.value)
            if test == TestName.OBSERVE:
                reader.step = "passive_observe"
                reader.observe(self.config.observe_seconds)
                return "serial_observation_completed_no_commands"
            if test == TestName.DERBY:
                from .derby_smoke import derby_smoke

                image, code = derby_smoke(reader)
                assert reader.derby_observation is not None
                return BackendResult(code, image, derby_observation=reader.derby_observation())
            if test == TestName.DERBY_FIXED:
                from .derby_fixed import execute_fixed

                trial = execute_fixed(reader)
                return BackendResult("derby_fixed_trial_verified", fixed_trial=trial)
            if test == TestName.CAPTURE:
                image = capture_home(reader)
                # Python completes finally before returning this result. The
                # broker never receives an image if closing the port fails.
                return BackendResult("render_buffer_captured_lcd_unverified", image)
            hello_cycle(reader)
            return "hello_markers_passed_physical_checks_not_assessed"
        except SerialFailure as failure:
            # Close itself may block. Release raw read-frame references before
            # entering that ownership-preserving wait, not only at async return.
            operation_failure = failure
            if reader is not None and reader.derby_observation is not None:
                failure.diagnostic.setdefault(
                    "derby_observation",
                    reader.derby_observation().model_dump(mode="json", exclude_none=True),
                )
            if (
                reader is not None
                and reader.fixed_collector is not None
                and failure.fixed_trial is None
            ):
                failure.fixed_trial = reader.fixed_collector.snapshot(
                    self.clock(), disconnect=failure.code == "serial_read_failed"
                )
            failure.__traceback__ = None
            failure.__context__ = None
            failure.__cause__ = None
            raise failure from None
        except Exception:
            uncertain = SerialFailure("serial_operation_uncertain")
            if reader is not None and reader.fixed_collector is not None:
                uncertain.fixed_trial = reader.fixed_collector.snapshot(self.clock())
            raise uncertain from None
        finally:
            derby_summary = (
                reader.derby_observation().model_dump(mode="json", exclude_none=True)
                if reader is not None and reader.derby_observation is not None
                else None
            )
            fixed_summary = (
                reader.fixed_collector.snapshot(self.clock())
                if reader is not None and reader.fixed_collector is not None
                else None
            )
            if reader is not None:
                reader.clear_transient()
            # Exactly this worker owns close. No other consumer opens a second port.
            try:
                port.close()
                if port.is_open:
                    raise SerialFailure("serial_close_unconfirmed", step="close_serial")
            except Exception:
                self.ownership_uncertain = True
                self._unclosed_port = port
                self.hardware_connected = True
                emit("serial_close_unconfirmed")
                close_failure = SerialFailure("serial_close_unconfirmed", step="close_serial")
                if operation_failure is not None:
                    close_failure.diagnostic["prior_failure_code"] = operation_failure.code
                    if "device_fault_category" in operation_failure.diagnostic:
                        close_failure.diagnostic["device_fault_category"] = (
                            operation_failure.diagnostic["device_fault_category"]
                        )
                if derby_summary is not None:
                    close_failure.diagnostic["derby_observation"] = derby_summary
                close_failure.fixed_trial = fixed_summary
                raise close_failure from None
            else:
                self.hardware_connected = False
                if operation_failure is not None and test == TestName.DERBY_FIXED:
                    operation_failure.diagnostic["serial_close_confirmed"] = True
                if port.is_open is False:
                    self.last_closed_plan = json.loads(json.dumps(plan))
                emit("serial_close")

    def _close_restart_port(self, port: SerialPort) -> None:
        """An uncertain close is terminal, including an intermediate reconnect close."""
        try:
            port.close()
            if port.is_open is not False:
                raise SerialFailure("serial_close_unconfirmed", step="close_serial")
        except Exception:
            self.ownership_uncertain = True
            self._unclosed_port = port
            self.hardware_connected = True
            raise SerialFailure("serial_close_unconfirmed", step="close_serial") from None
        self.hardware_connected = False

    def _restart_candidate(self, expected: PortInfo) -> PortInfo | None:
        inventory = list(self.inventory())
        matching = [
            candidate
            for candidate in inventory
            if (candidate.vid, candidate.pid, candidate.serial_number)
            == (expected.vid, expected.pid, expected.serial_number)
        ]
        if not matching:
            return None
        if len(matching) != 1 or stable_usb_fingerprint(matching[0]) != stable_usb_fingerprint(
            expected
        ):
            raise SerialFailure("device_identity_changed", step="reconnect_serial")
        return matching[0]

    def _execute_restart_sync(
        self, current: dict[str, Any], emit: Callable[[str], None]
    ) -> BackendResult:
        """One reset, one reader and at most one reopen, all under the same owner.

        ESP32-S3 TRM v1.8, table 33.4-4 (normal flash boot): clear DTR;
        clear RTS to clear the download flag; assert then clear RTS to reset.
        https://www.espressif.com/sites/default/files/documentation/
        esp32-s3_technical_reference_manual_en.pdf
        The 100 ms native Serial/JTAG pulse is independent of USB-OTG reset.
        Reapplying the cleared DTR state after RTS also supports usbser drivers
        that defer DTR changes until RTS is assigned. No command bytes are sent.
        Successful control calls cannot establish that application code booted.
        """
        owned: SerialPort | None = None
        collector: DeviceLogCollector | None = None
        incoming: bytes = b""
        reopened = False
        try:
            owned = self.factory()
            owned.dtr = False
            owned.rts = False
            owned.port = current["port"]
            emit("serial_open")
            try:
                owned.open()
            except Exception:
                raise SerialFailure("serial_open_failed", step="open_serial") from None
            self.hardware_connected = True
            try:
                inventory = list(self.inventory())
                identity = resolve_port(self.config.selector, inventory)
                require_restart_identity(identity, inventory)
            except ValueError:
                raise SerialFailure("device_identity_changed", step="validate_identity") from None
            if fingerprint(identity) != current["device_fingerprint"]:
                raise SerialFailure("device_identity_changed", step="validate_identity")
            emit(TestName.RESTART.value)
            try:
                owned.reset_input_buffer()
            except Exception:
                raise SerialFailure(
                    "serial_restart_buffer_discard_failed", step="restart_buffer_discard"
                ) from None
            # The observation budget starts before the pulse and includes all
            # reconnect work. Only this pre-reset discard is ever allowed.
            deadline = self.clock() + FIRMWARE_OBSERVE_SECONDS
            emit("serial_restart_requested")
            try:
                owned.dtr = False
                owned.rts = False
                owned.dtr = False
                owned.rts = True
                owned.dtr = False
                self.pause(RESTART_HOLD_SECONDS)
                owned.rts = False
                owned.dtr = False
            except Exception:
                raise SerialFailure(
                    "serial_restart_control_failed", step="restart_control"
                ) from None
            emit("serial_restart_complete")
            reconnect_deadline = min(self.clock() + RECONNECT_SECONDS, deadline)
            collector = DeviceLogCollector()
            received = 0
            emit("firmware_observe")
            while self.clock() < deadline and received < MAX_RECEIVE_BYTES:
                disconnected = not owned.is_open
                if not disconnected:
                    requested = min(4096, MAX_RECEIVE_BYTES - received)
                    try:
                        incoming = owned.read(requested)
                    except OSError:
                        # Leave the driver's exception scope before close waits.
                        disconnected = True
                    except Exception:
                        raise SerialFailure("serial_read_failed", step="restart_observe") from None
                    if not disconnected:
                        if type(incoming) is not bytes or len(incoming) > requested:
                            raise SerialFailure("serial_read_failed", step="restart_observe")
                        received += len(incoming)
                        if self.clock() >= deadline:
                            break
                        if incoming:
                            try:
                                keep_reading = collector.feed(incoming)
                            except Exception:
                                raise SerialFailure(
                                    "serial_operation_uncertain", step="restart_observe"
                                ) from None
                            if not keep_reading:
                                break
                        incoming = b""
                        continue
                incoming = b""
                if reopened or self.clock() >= reconnect_deadline:
                    raise SerialFailure("serial_read_failed", step="restart_observe")
                # Never open a replacement before the old handle is confirmed
                # closed. On uncertain close, finally must not try again.
                collector.mark_gap()
                self._close_restart_port(owned)
                owned = None
                emit("serial_reconnect_wait")
                candidate = None
                while self.clock() < reconnect_deadline:
                    candidate = self._restart_candidate(identity)
                    if self.clock() >= reconnect_deadline:
                        break
                    if candidate is not None:
                        break
                    remaining = reconnect_deadline - self.clock()
                    if remaining <= 0:
                        break
                    self.pause(min(0.05, remaining))
                if candidate is None or self.clock() >= reconnect_deadline:
                    raise SerialFailure("serial_reconnect_timeout", step="reconnect_serial")
                owned = self.factory()
                owned.dtr = False
                owned.rts = False
                owned.port = candidate.port
                reopened = True  # Even a failed open consumes the only attempt.
                if self.clock() >= reconnect_deadline:
                    raise SerialFailure("serial_reconnect_timeout", step="reconnect_serial")
                try:
                    owned.open()
                except Exception:
                    raise SerialFailure("serial_open_failed", step="reconnect_serial") from None
                self.hardware_connected = True
                if self.clock() >= reconnect_deadline:
                    raise SerialFailure("serial_reconnect_timeout", step="reconnect_serial")
                # Check the new COM path as well as stable USB identity to narrow
                # the enumeration/open race. There is no alternate selector.
                confirmed = self._restart_candidate(identity)
                if confirmed is None or confirmed.port != candidate.port:
                    raise SerialFailure("device_identity_changed", step="reconnect_serial")
                if self.clock() >= reconnect_deadline:
                    raise SerialFailure("serial_reconnect_timeout", step="reconnect_serial")
                emit("serial_reopened")
                emit("firmware_observe")
            incoming = b""
            try:
                observation = replace(
                    collector.finish(), reset_sequence_issued=True, capture_gap=reopened
                )
            except Exception:
                raise SerialFailure("serial_operation_uncertain", step="restart_observe") from None
            # The finally block confirms final close before publishing any report.
            return BackendResult(
                "restart_observation_completed_unverified", observation=observation
            )
        except SerialFailure as failure:
            failure.__traceback__ = None
            failure.__context__ = None
            failure.__cause__ = None
            raise failure from None
        except Exception:
            raise SerialFailure("serial_operation_uncertain", step="restart_observe") from None
        finally:
            incoming = b""
            cleanup_failed = False
            if collector is not None:
                try:
                    collector.clear_transient()
                except Exception:
                    # Parser cleanup must never prevent the sole owner closing
                    # the port, including after an unexpected parser failure.
                    cleanup_failed = True
                collector = None
            if self.ownership_uncertain:
                emit("serial_close_unconfirmed")
            else:
                if owned is not None:
                    try:
                        self._close_restart_port(owned)
                    except SerialFailure:
                        emit("serial_close_unconfirmed")
                        raise
                emit("serial_close")
            if cleanup_failed and not self.ownership_uncertain:
                raise SerialFailure("serial_operation_uncertain", step="restart_observe") from None
