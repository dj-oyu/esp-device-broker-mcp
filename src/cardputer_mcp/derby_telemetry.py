"""Bounded numeric DERBY diagnostics for firmware 9db14c8825b389fba98b541b4b7b712574f9346e.

One complete bare line enters at a time. No raw console content is retained.
Sampling and USB output perturb this diagnostic firmware's measured throughput.
"""

import math
import re
from collections.abc import Callable
from time import monotonic
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

SOURCE_COMMIT: Literal["9db14c8825b389fba98b541b4b7b712574f9346e"] = (
    "9db14c8825b389fba98b541b4b7b712574f9346e"
)
MAX_SAMPLES = 180
MAX_EVENTS = 64
MAX_REPORT_BYTES = 120 * 1024  # Reserve 8 KiB for the broker's private report envelope.
U32_MAX = (1 << 32) - 1
U64_MAX = (1 << 64) - 1
U32 = Annotated[int, Field(ge=0, le=U32_MAX)]
U64 = Annotated[int, Field(ge=0, le=U64_MAX)]
DeviceTime = Annotated[int, Field(ge=0, le=(1 << 63) - 1)]
HostTime = Annotated[int, Field(ge=0, le=180000)]
Camera = Annotated[int, Field(ge=0, le=9)]
Tier = Annotated[int, Field(ge=0, le=2)]
Total = Annotated[int, Field(ge=0, le=MAX_SAMPLES * U64_MAX)]
Phase = Literal["pad", "gate", "race", "photo", "res"]


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class DerbyInterval(_Frozen):
    count: U32
    device_us: U64


class DerbyTelemetrySample(_Frozen):
    at_ms: HostTime
    continuity_epoch: U32
    v: Literal[1]
    run: U32
    us: DeviceTime
    begin: DeviceTime
    frame: U32
    ctx: U32
    seed: str = Field(pattern=r"^[0-9A-F]{8}$")
    race: U32
    phase: int = Field(ge=0, le=4)
    cam: Camera
    tier: Tier
    step: U32
    u: int = Field(ge=-1, le=1000000)
    gu_sample_max: U32
    cap: U32
    eval_used: U32
    eval_cap: U32
    refused: U32
    seq: int = Field(ge=1, le=300)
    limit: Literal[300]
    io_us_max: U32
    intervals: tuple[DerbyInterval, ...] = Field(min_length=10, max_length=10)


class DerbyDiagnosticEvent(_Frozen):
    at_ms: HostTime
    kind: Literal[
        "camera", "tier", "load_stall", "demo_fail", "frame_fail", "load_fail", "degrade", "oom"
    ]
    camera: Camera | None = None
    tier: Tier | None = None
    phase: Phase | None = None


class DerbyQuality(_Frozen):
    missing: bool
    parse_errors: U32
    seq_gaps: U32
    missing_output_attempts: U32
    seq_duplicates: U32
    seq_regressions: U32
    context_gaps: U32
    counter_regressions: U32
    run_changes: U32
    refused_observed: bool
    limit_reached_observed: bool
    samples_truncated: bool
    events_truncated: bool
    report_byte_limit: bool
    truncated: bool
    invalid_clock: bool
    partial_line_or_fault_possible: bool
    final_write_outcome_unknown: Literal[True] = True


class DerbyCameraSummary(_Frozen):
    camera: Camera
    tier: Tier
    valid_window_count: int = Field(ge=1, le=MAX_SAMPLES - 1)
    valid_interval_count: Total
    valid_device_us: Total
    weighted_diagnostic_fps: float = Field(ge=0, allow_inf_nan=False)
    max_window_diagnostic_fps: float = Field(ge=0, allow_inf_nan=False)
    io_us_max_prior_writes: U32


class DerbyEvaluationCheckpoint(_Frozen):
    run: U32
    used_bytes: U32 | None = None
    cap_bytes: U32 | None = None
    checkpoint_headroom_bytes: U32 | None = None


class DerbyDiagnostics(_Frozen):
    version: Literal[1] = 1
    wire_contract_commit: Literal["9db14c8825b389fba98b541b4b7b712574f9346e"] = SOURCE_COMMIT
    time_basis: Literal["host_ms_since_serial_operation_start"] = (
        "host_ms_since_serial_operation_start"
    )
    started_at_ms: Literal[0] = 0
    finished_at_ms: HostTime
    samples: tuple[DerbyTelemetrySample, ...] = Field(max_length=MAX_SAMPLES)
    events: tuple[DerbyDiagnosticEvent, ...] = Field(max_length=MAX_EVENTS)
    quality: DerbyQuality
    cameras: tuple[DerbyCameraSummary, ...] = Field(max_length=30)
    sampled_heap_lower_bound_bytes: U32 | None = None
    evaluation_checkpoints: tuple[DerbyEvaluationCheckpoint, ...] = Field(max_length=MAX_SAMPLES)
    scope: Literal["diagnostic_completed_context_throughput_and_sampled_guest_malloc_bytes"] = (
        "diagnostic_completed_context_throughput_and_sampled_guest_malloc_bytes"
    )
    transport_perturbation: Literal[
        "USB_output_may_block_about_100ms_plus_tick_rounding_no_normal_firmware_extrapolation"
    ] = "USB_output_may_block_about_100ms_plus_tick_rounding_no_normal_firmware_extrapolation"
    physical_lcd_verified: Literal[False] = False
    allocator_peak_verified: Literal[False] = False
    complete_error_count_verified: Literal[False] = False


_FIELDS = (
    "v run us begin frame ctx seed race phase cam tier step u gu_sample_max cap "
    "eval_used eval_cap refused seq limit io_us_max intervals"
).split()
_UNSIGNED = re.compile(rb"(?:0|[1-9][0-9]{0,19})\Z")
_SEED = re.compile(rb"[0-9A-F]{8}\Z")
# main/ui/jsconsole.c at wire_contract_commit emits console.log via ESP_LOGI
# with tag "js" and at most 200 message bytes. Do not generalize logger tags.
_JS_LOG_PREFIX = re.compile(rb"^(?P<color>\x1b\[(?:0;)?3[123]m)?[IWE] \([0-9]{1,20}\) js: ")
_COLOR_RESET = b"\x1b[0m"
_CAMERAS = {
    b"WIDE": 0,
    b"CLOSE": 1,
    b"FIELD": 2,
    b"FINISH": 3,
    b"VISION": 5,
    b"HEAD ON": 6,
    b"WIDE 1": 7,
    b"WIDE 2": 8,
    b"WIDE 3": 9,
}
_PHASES: dict[bytes, Phase] = {
    b"pad": "pad",
    b"gate": "gate",
    b"race": "race",
    b"photo": "photo",
    b"res": "res",
}


def _number(raw: bytes) -> int:
    if _UNSIGNED.fullmatch(raw) is None:
        raise ValueError("invalid_derby_telemetry")
    return int(raw)


def parse_telemetry_line(
    line: bytes, *, at_ms: int = 0, continuity_epoch: int = 0
) -> DerbyTelemetrySample:
    """Parse the exact ESP32-S3 wire grammar, without LF; errors never echo input."""
    try:
        if len(line) + 1 >= 768 or not line.startswith(b"DERBY_TLM "):
            raise ValueError
        parts = line[len(b"DERBY_TLM ") :].split(b" ")
        if len(parts) != len(_FIELDS):
            raise ValueError
        values: dict[str, Any] = {"at_ms": at_ms, "continuity_epoch": continuity_epoch}
        for name, part in zip(_FIELDS, parts, strict=True):
            key, separator, value = part.partition(b"=")
            if separator != b"=" or key != name.encode("ascii"):
                raise ValueError
            if name == "seed":
                if _SEED.fullmatch(value) is None:
                    raise ValueError
                values[name] = value.decode("ascii")
            elif name == "intervals":
                pairs = value.split(b",")
                if len(pairs) != 10:
                    raise ValueError
                intervals = []
                for pair in pairs:
                    count, colon, duration = pair.partition(b":")
                    if colon != b":":
                        raise ValueError
                    intervals.append(
                        DerbyInterval(count=_number(count), device_us=_number(duration))
                    )
                values[name] = tuple(intervals)
            else:
                values[name] = -1 if name == "u" and value == b"-1" else _number(value)
        sample = DerbyTelemetrySample.model_validate(values)
        if sample.begin > sample.us or (sample.phase not in (1, 2) and sample.u != -1):
            raise ValueError
        return sample
    except (ValueError, TypeError, OverflowError):
        raise ValueError("invalid_derby_telemetry") from None


def _event_body(line: bytes) -> bytes | None:
    # Only non-TLM events have this source-defined normalization. Bound before
    # slicing, never scan a raw line for a marker inside arbitrary text.
    if len(line) > 256:
        return None
    line = line.removesuffix(b"\r")
    prefix = _JS_LOG_PREFIX.match(line)
    if prefix is None:
        return line
    body = line[prefix.end() :]
    if prefix.group("color") is not None:
        if not body.endswith(_COLOR_RESET):
            return None
        body = body[: -len(_COLOR_RESET)]
    elif body.endswith(_COLOR_RESET):
        return None
    return body if len(body) <= 200 and body.startswith(b"DERBY ") else None


def _event(line: bytes, at_ms: int) -> DerbyDiagnosticEvent | None:
    body = _event_body(line)
    if body is None:
        return None
    line = body
    if line.startswith(b"DERBY CAM "):
        if len(line) > 20:
            return None
        camera = _CAMERAS.get(line[10:])
        return (
            DerbyDiagnosticEvent(at_ms=at_ms, kind="camera", camera=camera)
            if camera is not None
            else None
        )
    if line in (b"DERBY TIER 0", b"DERBY TIER 1", b"DERBY TIER 2"):
        return DerbyDiagnosticEvent(at_ms=at_ms, kind="tier", tier=int(line[-1:]))
    if line == b"DERBY GO LOADSTALL":
        return DerbyDiagnosticEvent(at_ms=at_ms, kind="load_stall")
    for prefix, kind in (
        (b"DERBY DEMOFAIL ", "demo_fail"),
        (b"DERBY LOADFAIL ", "load_fail"),
        (b"DERBY DEGRADE ", "degrade"),
        (b"app: OOM", "oom"),
    ):
        if line.startswith(prefix):
            return DerbyDiagnosticEvent.model_validate(dict(at_ms=at_ms, kind=kind))
    if line.startswith(b"DERBY FRAMEFAIL "):
        phase = line[16:22].partition(b" ")[0]
        return DerbyDiagnosticEvent(at_ms=at_ms, kind="frame_fail", phase=_PHASES.get(phase))
    return None


def _window(a: DerbyTelemetrySample, b: DerbyTelemetrySample) -> tuple[int, int] | None:
    """Source-pinned camera_windows, with an additional malformed/event boundary."""
    if any(
        getattr(a, key) != getattr(b, key)
        for key in ("run", "begin", "seed", "race", "phase", "cam", "tier", "continuity_epoch")
    ):
        return None
    delta, elapsed = b.frame - a.frame, b.us - a.us
    na, nb = a.intervals[b.cam], b.intervals[b.cam]
    if (
        b.seq != a.seq + 1
        or elapsed < 1000000
        or delta <= 0
        or b.ctx - a.ctx != delta
        or nb.count - na.count != delta
        or nb.device_us <= na.device_us
        or nb.device_us - na.device_us != elapsed
        or a.refused != 0
        or b.refused != 0
        or b.seq >= b.limit
        or any(
            y.count < x.count or y.device_us < x.device_us
            for x, y in zip(a.intervals, b.intervals, strict=True)
        )
        or any(
            x != y
            for i, (x, y) in enumerate(zip(a.intervals, b.intervals, strict=True))
            if i != b.cam
        )
    ):
        return None
    return delta, elapsed


def _summaries(samples: list[DerbyTelemetrySample]) -> tuple[DerbyCameraSummary, ...]:
    totals: dict[tuple[int, int], tuple[int, int, int, float, int]] = {}
    for a, b in zip(samples, samples[1:], strict=False):
        window = _window(a, b)
        if window is None:
            continue
        count, duration = window
        key = b.cam, b.tier
        windows, frames, us, maximum, io = totals.get(key, (0, 0, 0, 0.0, 0))
        totals[key] = (
            windows + 1,
            frames + count,
            us + duration,
            max(maximum, count * 1e6 / duration),
            max(io, b.io_us_max),
        )
    return tuple(
        DerbyCameraSummary(
            camera=cam,
            tier=tier,
            valid_window_count=n,
            valid_interval_count=count,
            valid_device_us=us,
            weighted_diagnostic_fps=count * 1e6 / us,
            max_window_diagnostic_fps=maximum,
            io_us_max_prior_writes=io,
        )
        for (cam, tier), (n, count, us, maximum, io) in sorted(totals.items())
    )


class DerbyDiagnosticsCollector:
    """No I/O: bounded parsed values only. Snapshot never consults device state."""

    def __init__(self, clock: Callable[[], float] = monotonic, origin: float | None = None):
        self.clock = clock
        self._last_ms = 0
        self._epoch = 0
        self._invalid_clock = False
        self.origin = origin if origin is not None else self._clock()
        self.clear()

    def _clock(self) -> float:
        try:
            value = self.clock()
            if math.isfinite(value):
                return value
        except Exception:
            pass
        self._invalid_clock = True
        return 0.0

    def _at_ms(self) -> int:
        elapsed = self._clock() - self.origin
        if not math.isfinite(elapsed) or elapsed < 0:
            self._invalid_clock = True
            return self._last_ms
        current = 180000 if elapsed >= 180 else int(elapsed * 1000)
        if current < self._last_ms:
            self._invalid_clock = True
        self._last_ms = max(current, self._last_ms)
        return self._last_ms

    def clear(self) -> None:
        """Drop parsed references; never retain or expose raw received content."""
        self._samples: list[DerbyTelemetrySample] = []
        self._events: list[DerbyDiagnosticEvent] = []
        self._counts = dict.fromkeys(
            (
                "parse_errors",
                "seq_gaps",
                "missing_output_attempts",
                "seq_duplicates",
                "seq_regressions",
                "context_gaps",
                "counter_regressions",
                "run_changes",
            ),
            0,
        )
        self._epoch = 0
        self._last_ms = 0
        self._refused = self._limit = self._sample_full = self._event_full = False
        self._byte_full = self._partial = False

    def _increment(self, name: str, amount: int = 1) -> None:
        self._counts[name] = min(U32_MAX, self._counts[name] + amount)

    def mark_partial_line_or_fault(self) -> None:
        self._partial = True
        self._epoch = min(U32_MAX, self._epoch + 1)

    def _check_sequence(self, sample: DerbyTelemetrySample) -> None:
        self._refused |= sample.refused > 0
        self._limit |= sample.seq == sample.limit
        if not self._samples:
            if sample.seq > 1:
                self._increment("seq_gaps")
                self._increment("missing_output_attempts", sample.seq - 1)
            return
        previous = self._samples[-1]
        if previous.run != sample.run:
            self._increment("run_changes")
            if sample.seq > 1:
                self._increment("seq_gaps")
                self._increment("missing_output_attempts", sample.seq - 1)
            return
        difference = sample.seq - previous.seq
        if difference == 0:
            self._increment("seq_duplicates")
        elif difference < 0:
            self._increment("seq_regressions")
        elif difference > 1:
            self._increment("seq_gaps")
            self._increment("missing_output_attempts", difference - 1)
        if sample.ctx - previous.ctx != sample.frame - previous.frame:
            self._increment("context_gaps")
        if (
            previous.begin != sample.begin
            or any(
                getattr(sample, key) < getattr(previous, key)
                for key in ("us", "frame", "ctx", "refused", "gu_sample_max", "io_us_max")
            )
            or any(
                b.count < a.count or b.device_us < a.device_us
                for a, b in zip(previous.intervals, sample.intervals, strict=True)
            )
        ):
            self._increment("counter_regressions")
            self._epoch = min(U32_MAX, self._epoch + 1)

    def _fits_report(self) -> bool:
        # Measure actual JSON with the worst remaining counter/time/flag widths.
        # Later clock ticks or malformed input cannot push an accepted report over cap.
        report = self.snapshot()
        quality = report.quality.model_copy(
            update={
                **dict.fromkeys(self._counts, U32_MAX),
                **{
                    name: False
                    for name, value in report.quality.model_dump().items()
                    if type(value) is bool and name != "final_write_outcome_unknown"
                },
            }
        )
        report = report.model_copy(update={"finished_at_ms": 180000, "quality": quality})
        return len(report.model_dump_json().encode("utf-8")) <= MAX_REPORT_BYTES

    def feed(self, line: bytes) -> None:
        """Consume a complete bare line without LF; unknown lines leave no references."""
        if self._byte_full:
            return
        at_ms = self._at_ms()
        if line.startswith(b"DERBY_TLM"):
            if self._sample_full:
                return
            if len(self._samples) >= MAX_SAMPLES:
                self._sample_full = True
                return
            try:
                sample = parse_telemetry_line(line, at_ms=at_ms, continuity_epoch=self._epoch)
            except ValueError:
                self._increment("parse_errors")
                self._epoch = min(U32_MAX, self._epoch + 1)
                return
            self._check_sequence(sample)
            sample = sample.model_copy(update={"continuity_epoch": self._epoch})
            self._samples.append(sample)
            if not self._fits_report():
                self._samples.pop()
                self._byte_full = True
        else:
            event = _event(line, at_ms)
            if event is None:
                return
            self._epoch = min(U32_MAX, self._epoch + 1)
            if self._event_full:
                return
            if len(self._events) >= MAX_EVENTS:
                self._event_full = True
                return
            self._events.append(event)
            if not self._fits_report():
                self._events.pop()
                self._byte_full = True

    def snapshot(self) -> DerbyDiagnostics:
        checkpoints: dict[int, DerbyEvaluationCheckpoint] = {}
        for sample in self._samples:
            used, cap = sample.eval_used or None, sample.eval_cap or None
            checkpoints.setdefault(
                sample.run,
                DerbyEvaluationCheckpoint(
                    run=sample.run,
                    used_bytes=used,
                    cap_bytes=cap,
                    checkpoint_headroom_bytes=cap - used
                    if cap is not None and used is not None and cap >= used
                    else None,
                ),
            )
        return DerbyDiagnostics(
            finished_at_ms=self._at_ms(),
            samples=tuple(self._samples),
            events=tuple(self._events),
            quality=DerbyQuality.model_validate(
                dict(
                    missing=not self._samples,
                    **self._counts,
                    refused_observed=self._refused,
                    limit_reached_observed=self._limit,
                    samples_truncated=self._sample_full,
                    events_truncated=self._event_full,
                    report_byte_limit=self._byte_full,
                    truncated=self._sample_full or self._event_full or self._byte_full,
                    invalid_clock=self._invalid_clock,
                    partial_line_or_fault_possible=self._partial,
                )
            ),
            cameras=_summaries(self._samples),
            sampled_heap_lower_bound_bytes=max(
                (s.gu_sample_max for s in self._samples if s.gu_sample_max), default=None
            ),
            evaluation_checkpoints=tuple(checkpoints.values()),
        )
