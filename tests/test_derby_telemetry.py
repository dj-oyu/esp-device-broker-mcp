"""Source-pinned numeric telemetry, discontinuities, redaction and retention caps."""

from dataclasses import FrozenInstanceError

import pytest
from pydantic import ValidationError

from cardputer_mcp.derby_telemetry import (
    MAX_EVENTS,
    MAX_REPORT_BYTES,
    MAX_SAMPLES,
    MEASURE_SOURCE_COMMIT,
    SOURCE_COMMIT,
    U32_MAX,
    U64_MAX,
    DerbyDiagnostics,
    DerbyDiagnosticsCollector,
    DerbyMeasureOverflow,
    DerbyMeasureSample,
    parse_measure_line,
    parse_telemetry_line,
)


def wire(**changes):
    values = dict(
        v=1,
        run=1,
        us=1000000,
        begin=0,
        frame=30,
        ctx=30,
        seed="00000002",
        race=1,
        phase=2,
        cam=8,
        tier=1,
        step=391,
        u=500000,
        gu_sample_max=130000,
        cap=163840,
        eval_used=120000,
        eval_cap=163840,
        refused=0,
        seq=1,
        limit=300,
        io_us_max=200,
        intervals=",".join("29:966667" if i == 8 else "0:0" for i in range(10)),
    )
    values.update(changes)
    return ("DERBY_TLM " + " ".join(f"{key}={value}" for key, value in values.items())).encode()


def later(**changes):
    return wire(
        **dict(
            us=2000000,
            frame=60,
            ctx=60,
            seq=2,
            intervals=",".join("59:1966667" if i == 8 else "0:0" for i in range(10)),
        )
        | changes
    )


def measure_wire(**changes):
    values = dict(
        v=1,
        seq=1,
        us=1000000,
        heap_sample_max=130000,
        cap=163840,
        scalar=4,
        pie=3,
        ctx=30,
        cont=2,
        done=29,
        dispatch_fail=0,
        bytes=8192,
        tx_ok=2,
        tx_fail=0,
        capture=1,
        refused=0,
        faults=0,
        clock_min=1,
        clock_max=2,
        pairs=20,
    )
    values.update({f"s{i}": "10:8:2:6:5" for i in range(10)})
    values.update(changes)
    return ("DERBY_MEASURE " + " ".join(f"{key}={value}" for key, value in values.items())).encode()


def collector():
    return DerbyDiagnosticsCollector(clock=lambda: 10.0, origin=10.0)


def test_measure_exact_wire_and_backward_compatible_report():
    row = parse_measure_line(measure_wire(s9=f"{U64_MAX}:8:2:6:5"), at_ms=123)
    assert isinstance(row, DerbyMeasureSample)
    assert row.stages[9][0] == U64_MAX and row.at_ms == 123
    c = collector()
    c.feed(measure_wire())
    c.feed(b"DERBY_MEASURE v=1 seq=2 us=2000000 overflow=1")
    report = c.snapshot()
    assert report.measurement_wire_contract_commit == MEASURE_SOURCE_COMMIT
    assert report.measurement_stage_order == (
        "guest",
        "bridge",
        "native",
        "proc_build",
        "vm",
        "points",
        "raster",
        "transfer",
        "output",
        "observer",
    )
    assert report.measurement_scope == "instrumented_cumulative_phase_accounting_only"
    assert len(report.measurements) == 2
    assert isinstance(report.measurements[1], DerbyMeasureOverflow)
    assert report.quality.measurement_overflow_observed
    assert not report.quality.measurement_missing
    assert DerbyDiagnostics.model_validate_json(report.model_dump_json()) == report
    old = report.model_dump()
    for name in (
        "measurement_wire_contract_commit",
        "measurement_stage_order",
        "measurement_scope",
        "measurements",
    ):
        old.pop(name)
    for name in (
        "measurement_missing",
        "measurement_parse_errors",
        "measurement_overflow_observed",
        "measurement_samples_truncated",
    ):
        old["quality"].pop(name)
    assert DerbyDiagnostics.model_validate(old).measurements == ()


@pytest.mark.parametrize(
    "line",
    [
        b"I (10) js: " + measure_wire(),
        b" " + measure_wire(),
        measure_wire() + b"\r",
        measure_wire() + b"\n",
        measure_wire() + b" private=SECRET",
        measure_wire().replace(b"v=1 ", b"v=2 "),
        measure_wire().replace(b"seq=1 ", b"seq=01 "),
        measure_wire().replace(b"seq=1 ", b"seq=0 "),
        measure_wire().replace(b"seq=1 ", b"seq=301 "),
        measure_wire().replace(b"us=1000000 ", b"us=-1 "),
        measure_wire().replace(b"us=1000000 ", b"us=18446744073709551616 "),
        measure_wire().replace(b"scalar=4 pie=3 ", b"pie=3 scalar=4 "),
        measure_wire().replace(b"s0=10:8:2:6:5", b"s0=10:8:2:6"),
        measure_wire().replace(b"s0=10:8:2:6:5", b"s0=10:8:02:6:5"),
        measure_wire().replace(b"s0=10:8:2:6:5", b"s0=10:8:2:6:5:0"),
        measure_wire().replace(b"s9=10:8:2:6:5", b"s8=10:8:2:6:5"),
        b"DERBY_MEASURE v=1 seq=1 us=1000000 overflow=0",
        b"DERBY_MEASURE v=1 seq=1 us=1000000 overflow=1 secret=SECRET",
        b"DERBY_MEASURE v=1 seq=1 us=1000000 overflow=1\r",
        b"DERBY_MEASURE " + b"9" * 1024,
    ],
)
def test_measure_invalid_line_has_fixed_error_and_no_raw_retention(line):
    with pytest.raises(ValueError, match="^invalid_derby_measure$"):
        parse_measure_line(line)
    c = collector()
    c.feed(line)
    result = c.snapshot()
    assert result.measurements == ()
    assert result.quality.measurement_parse_errors == int(line.startswith(b"DERBY_MEASURE"))
    assert "SECRET" not in result.model_dump_json()


def test_exact_wire_model_and_immutable_json_roundtrip():
    c = collector()
    c.feed(wire())
    report = c.snapshot()
    assert report.wire_contract_commit == SOURCE_COMMIT
    sample = report.samples[0]
    assert sample.seed == "00000002" and sample.intervals[8].count == 29
    assert sample.intervals[8].device_us == 966667
    assert report.sampled_heap_lower_bound_bytes == 130000
    checkpoint = report.evaluation_checkpoints[0]
    assert checkpoint.checkpoint_headroom_bytes == 43840
    assert report.quality.final_write_outcome_unknown
    assert not report.physical_lcd_verified and not report.allocator_peak_verified
    assert "no_normal_firmware_extrapolation" in report.transport_perturbation
    assert DerbyDiagnostics.model_validate_json(report.model_dump_json()) == report
    with pytest.raises((ValidationError, FrozenInstanceError)):
        report.samples[0].ctx = 1
    with pytest.raises(ValidationError):
        DerbyDiagnostics.model_validate({**report.model_dump(), "private_raw": "SECRET"})


@pytest.mark.parametrize(
    "line",
    [
        b"I (10) app: " + wire(),
        b" " + wire(),
        wire() + b"\r",
        wire() + b"\n",
        wire() + b" extra=SECRET",
        wire() + b" seq=1",
        wire().replace(b"v=1 ", b"v=2 "),
        wire().replace(b"run=1 ", b"run=01 "),
        wire().replace(b"run=1 ", b"run=+1 "),
        wire().replace(b"run=1 ", b"run=-1 "),
        wire().replace(b"run=1 ", b"run=1  "),
        wire().replace(b"run=1 ", b"run=1.0 "),
        wire().replace(b"ctx=30 ", b""),
        wire().replace(b"run=1 us=1000000", b"us=1000000 run=1"),
        wire(seed="abcdef00"),
        wire(seed="000000000"),
        wire(seed="0000000G"),
        wire(run=U32_MAX + 1),
        wire(us=1 << 63),
        wire(begin=1000001),
        wire(phase=5),
        wire(cam=10),
        wire(tier=3),
        wire(u=-2),
        wire(u=1000001),
        wire(phase=3, u=0),
        wire(cap=U32_MAX + 1),
        wire(limit=301),
        wire(seq=0),
        wire(seq=301),
        wire(intervals="0:0," * 9),
        wire(intervals="0:0," * 10 + "0:0"),
        wire(intervals=",".join([f"{U32_MAX + 1}:0"] + ["0:0"] * 9)),
        wire(intervals=",".join([f"0:{U64_MAX + 1}"] + ["0:0"] * 9)),
        wire(intervals="0:0:" + ",0:0" * 9),
        b"DERBY_TLM " + b"9" * 10000,
    ],
)
def test_malformed_or_unbounded_wire_never_echoes_input(line):
    with pytest.raises(ValueError, match="^invalid_derby_telemetry$"):
        parse_telemetry_line(line)


def test_weighted_aggregate_and_fastest_window_are_distinct():
    c = collector()
    c.feed(wire())
    c.feed(later())
    c.feed(
        wire(
            us=4000000,
            frame=80,
            ctx=80,
            seq=3,
            intervals=",".join("79:3966667" if i == 8 else "0:0" for i in range(10)),
        )
    )
    summary = c.snapshot().cameras[0]
    assert (summary.camera, summary.tier, summary.valid_window_count) == (8, 1, 2)
    assert summary.valid_interval_count == 50 and summary.valid_device_us == 3000000
    assert summary.weighted_diagnostic_fps == pytest.approx(50 / 3)
    assert summary.max_window_diagnostic_fps == 30
    assert summary.io_us_max_prior_writes == 200


@pytest.mark.parametrize(
    "changes",
    [
        {"ctx": 61},
        {"seq": 3},
        {"seq": 1},
        {"refused": 1},
        {"run": 2},
        {"cam": 7},
        {"tier": 2},
        {"phase": 1},
        {"race": 2},
        {"seed": "00000003"},
        {"begin": 1},
        {"us": 1},
        {"frame": 30},
        {"us": 1500000},
        {"seq": 300},
        {"gu_sample_max": 129999},
        {"io_us_max": 199},
        {"intervals": ",".join("58:1966667" if i == 8 else "0:0" for i in range(10))},
        {"intervals": ",".join("59:1066667" if i == 8 else "0:0" for i in range(10))},
        {
            "intervals": ",".join(
                "59:1966667" if i == 8 else "1:10" if i == 7 else "0:0" for i in range(10)
            )
        },
    ],
)
def test_gaps_transitions_and_incomplete_windows_never_become_fps(changes):
    c = collector()
    c.feed(wire())
    c.feed(later(**changes))
    assert not c.snapshot().cameras


def test_events_strip_all_arbitrary_error_content_and_ignore_unknown_lines():
    c = collector()
    inputs = [
        b"DERBY CAM WIDE 2",
        b"DERBY TIER 2",
        b"DERBY GO LOADSTALL",
        b"DERBY DEMOFAIL SECRET credential=/private/path",
        b"DERBY LOADFAIL PRIVATE_PLAN SECRET",
        b"DERBY FRAMEFAIL gate SECRET",
        b"DERBY DEGRADE SECRET",
        b"app: OOM SECRET",
    ]
    for line in inputs + [b"PIX SECRET", b"DERBY CAM SECRET", b"unrelated SECRET"]:
        c.feed(line)
    report = c.snapshot()
    assert [e.kind for e in report.events] == [
        "camera",
        "tier",
        "load_stall",
        "demo_fail",
        "load_fail",
        "frame_fail",
        "degrade",
        "oom",
    ]
    assert report.events[0].camera == 8 and report.events[5].phase == "gate"
    assert "SECRET" not in report.model_dump_json() and "PRIVATE" not in report.model_dump_json()
    assert report.quality.missing and not report.cameras


# Level, colour and ending are independent regex/strip stages: every level, every
# accepted colour and both endings appear at least once.


# Colour/ending are prefix-stage choices independent of the numeric detail.


def test_record_limits_stop_retaining_without_overwriting_earlier_records():
    c = collector()
    for seq in range(MAX_SAMPLES + 5):
        c.feed(wire(seq=seq + 1))
    for _ in range(MAX_EVENTS + 5):
        c.feed(b"DERBY CAM WIDE")
    report = c.snapshot()
    assert len(report.samples) == MAX_SAMPLES and len(report.events) == MAX_EVENTS
    assert report.samples[0].seq == 1 and report.samples[-1].seq == MAX_SAMPLES
    assert report.quality.samples_truncated and report.quality.events_truncated
    assert report.quality.truncated
    assert len(report.model_dump_json().encode()) <= MAX_REPORT_BYTES
