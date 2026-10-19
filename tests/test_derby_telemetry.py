"""Source-pinned numeric telemetry, discontinuities, redaction and retention caps."""

import json
from dataclasses import FrozenInstanceError

import pytest
from pydantic import ValidationError

from cardputer_mcp import derby_telemetry as telemetry
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


def test_measure_overflow_only_has_no_phase_data_and_collection_is_bounded(monkeypatch):
    c = collector()
    c.feed(b"DERBY_MEASURE v=1 seq=1 us=1000000 overflow=1")
    report = c.snapshot()
    assert report.quality.measurement_missing and report.quality.measurement_overflow_observed
    assert isinstance(report.measurements[0], DerbyMeasureOverflow)
    monkeypatch.setattr(telemetry, "MAX_REPORT_BYTES", len(report.model_dump_json()) + 100)
    c.feed(measure_wire(seq=2))
    report = c.snapshot()
    assert len(report.measurements) == 1
    assert report.quality.report_byte_limit and report.quality.truncated
    assert len(report.model_dump_json().encode()) <= telemetry.MAX_REPORT_BYTES


def test_measure_count_cap_stops_at_180_and_keeps_byte_cap():
    c = collector()
    for seq in range(1, MAX_SAMPLES + 2):
        c.feed(measure_wire(seq=seq))
    report = c.snapshot()
    assert len(report.measurements) == MAX_SAMPLES
    assert report.quality.measurement_samples_truncated and report.quality.truncated
    assert not report.quality.report_byte_limit
    assert len(report.model_dump_json().encode()) <= MAX_REPORT_BYTES


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


def test_uint_extremes_and_nonrace_minus_one_are_supported():
    row = parse_telemetry_line(
        wire(
            run=U32_MAX,
            us=(1 << 63) - 1,
            frame=U32_MAX,
            ctx=U32_MAX,
            seed="FFFFFFFF",
            race=U32_MAX,
            step=U32_MAX,
            phase=3,
            u=-1,
            cap=U32_MAX,
            gu_sample_max=U32_MAX,
            eval_used=U32_MAX,
            eval_cap=U32_MAX,
            refused=U32_MAX,
            io_us_max=U32_MAX,
            intervals=",".join([f"{U32_MAX}:{U64_MAX}"] * 10),
        )
    )
    assert row.intervals[-1].device_us == U64_MAX and row.u == -1


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


def test_other_camera_regression_rejects_window_and_marks_quality():
    c = collector()
    c.feed(
        wire(
            intervals=",".join(
                "29:966667" if i == 8 else "1:100" if i == 7 else "0:0" for i in range(10)
            )
        )
    )
    c.feed(later())
    assert not c.snapshot().cameras
    assert c.snapshot().quality.counter_regressions == 1


def test_quality_separates_seq_context_run_and_refusal_evidence():
    c = collector()
    c.feed(wire(seq=3))
    c.feed(later(seq=5, ctx=61, refused=1))
    c.feed(later(seq=5))
    c.feed(later(seq=2))
    c.feed(later(seq=300, run=2))
    q = c.snapshot().quality
    assert q.seq_gaps == 3 and q.missing_output_attempts == 302
    assert q.seq_duplicates == 1 and q.seq_regressions == 1
    assert q.context_gaps and q.run_changes == 1
    assert q.refused_observed and q.limit_reached_observed and q.counter_regressions


def test_new_run_prefix_gap_counts_known_missing_attempts_without_sequence_regression():
    c = collector()
    c.feed(wire(seq=10))
    c.feed(wire(run=2, seq=5))
    c.feed(wire(run=2, seq=6))
    q = c.snapshot().quality
    assert q.run_changes == 1 and q.seq_gaps == 2
    assert q.missing_output_attempts == 9 + 4
    assert q.seq_regressions == 0 and q.seq_duplicates == 0


def test_unseen_limit_after_retention_stops_remains_only_unobserved(monkeypatch):
    monkeypatch.setattr(telemetry, "MAX_SAMPLES", 1)
    c = collector()
    c.feed(wire())
    c.feed(wire(seq=300))
    report = c.snapshot()
    assert report.quality.samples_truncated and report.quality.truncated
    assert report.quality.limit_reached_observed is False
    assert "limit_reached" not in report.quality.model_dump()
    assert len(report.samples) == 1


@pytest.mark.parametrize(
    "barrier", [b"DERBY_TLM malformed", b"DERBY CAM CLOSE", b"DERBY TIER 2", b"DERBY GO LOADSTALL"]
)
def test_intervening_invalid_record_or_event_breaks_window(barrier):
    c = collector()
    c.feed(wire())
    c.feed(barrier)
    c.feed(later())
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


@pytest.mark.parametrize("level", [b"I", b"W", b"E"])
@pytest.mark.parametrize("color", [b"", b"\x1b[0;32m", b"\x1b[31m", b"\x1b[0;33m"])
@pytest.mark.parametrize("ending", [b"", b"\r"])
def test_source_js_logger_prefix_preserves_only_fixed_events(level, color, ending):
    c = collector()
    for body in (
        b"DERBY CAM WIDE 2",
        b"DERBY TIER 1",
        b"DERBY GO LOADSTALL",
        b"DERBY DEMOFAIL SECRET /private/path",
        b"DERBY FRAMEFAIL photo SECRET token=PRIVATE",
        b"DERBY LOADFAIL PRIVATE_PLAN SECRET",
    ):
        c.feed(color + level + b" (123) js: " + body + (b"\x1b[0m" if color else b"") + ending)
    report = c.snapshot()
    assert [e.kind for e in report.events] == [
        "camera",
        "tier",
        "load_stall",
        "demo_fail",
        "frame_fail",
        "load_fail",
    ]
    assert report.events[0].camera == 8 and report.events[1].tier == 1
    assert report.events[4].phase == "photo"
    public = report.model_dump_json()
    for private in ("SECRET", "PRIVATE", "/private/path", "js:", "token="):
        assert private not in public


@pytest.mark.parametrize("color", [b"", b"\x1b[31m", b"\x1b[0;31m"])
@pytest.mark.parametrize("ending", [b"", b"\r"])
@pytest.mark.parametrize(
    "detail",
    [
        b"n=1 first_req=8192 used=150000",
        b"n=4294967295 first_req=4294967295 used=4294967295",
        b"n=1 first_req=0 used=0",
    ],
)
def test_source_native_app_oom_envelope_retains_only_fixed_category(color, ending, detail):
    c = collector()
    c.feed(wire())
    c.feed(color + b"E (123) app: OOM " + detail + (b"\x1b[0m" if color else b"") + ending)
    c.feed(later())
    report = c.snapshot()
    assert [event.kind for event in report.events] == ["oom"]
    assert report.events[0].camera is None and report.events[0].phase is None
    assert not report.cameras  # The intervening error breaks an apparent valid window.
    assert not report.quality.partial_line_or_fault_possible
    for private in ("app:", "OOM", "first_req=", "used=", "\\u001b"):
        assert private not in report.model_dump_json()


@pytest.mark.parametrize(
    "line",
    [
        b"I (123) app: OOM n=1 first_req=8192 used=150000",
        b"W (123) app: OOM n=1 first_req=8192 used=150000",
        b"E (123) other: OOM n=1 first_req=8192 used=150000",
        b"E (123) app: HEAPPROBE_OOM quota=1 allocator=0",
        b"E (123) app: DERBY CAM WIDE 2",
        b"E (123) app: DERBY_TLM v=1",
        b"E (123) app: OOM_OTHER PRIVATE",
        b"E (123) app: OOM n=01 first_req=8192 used=150000",
        b"E (123) app: OOM n=4294967296 first_req=8192 used=150000",
        b"E (123) app: OOM n=1 first_req=4294967296 used=150000",
        b"E (123) app: OOM n=1 first_req=8192 used=4294967296",
        b"E (123) app: OOM n=1 first_req=8192 used=150000 PRIVATE",
        b"arbitrary E (123) app: OOM n=1 first_req=8192 used=150000",
        b"E (not-a-number) app: OOM n=1 first_req=8192 used=150000",
        b"E (" + b"1" * 21 + b") app: OOM n=1 first_req=8192 used=150000",
        b"\x1b[0;32mE (123) app: OOM n=1 first_req=8192 used=150000\x1b[0m",
        b"\x1b[0;31mE (123) app: OOM n=1 first_req=8192 used=150000",
        b"E (123) app: OOM n=1 first_req=8192 used=150000\x1b[0m",
        b"E (123) app: OOM n=1 first_req=8192 used=150000\r\r",
        b"E (123) app: OOM " + b"S" * 200,
    ],
)
def test_native_app_oom_does_not_broaden_tags_levels_prefixes_or_bounds(line):
    c = collector()
    c.feed(line)
    assert not c.snapshot().events and not c.snapshot().samples


@pytest.mark.parametrize(
    "line",
    [
        b"I (123) other: DERBY CAM WIDE 2",
        b"I (123) app: OOM SECRET",
        b"D (123) js: DERBY CAM WIDE 2",
        b"V (123) js: DERBY CAM WIDE 2",
        b"I (not-a-number) js: DERBY CAM WIDE 2",
        b"I (" + b"1" * 21 + b") js: DERBY CAM WIDE 2",
        b"arbitrary I (123) js: DERBY CAM WIDE 2",
        b"I (123) js: I (123) js: DERBY CAM WIDE 2",
        b"\x1b[999999999mI (123) js: DERBY CAM WIDE 2\x1b[0m",
        b"\x1b[0;32mI (123) js: DERBY CAM WIDE 2",
        b"I (123) js: DERBY CAM WIDE 2\x1b[0m",
        b"I (123) js: DERBY CAM WIDE 2\r\r",
        b"I (123) js: DERBY LOADFAIL " + b"S" * 200,
    ],
)
def test_other_prefixes_substring_search_or_unbounded_normalization_are_rejected(line):
    c = collector()
    c.feed(line)
    assert not c.snapshot().events and not c.snapshot().samples


@pytest.mark.parametrize(
    "line",
    [
        b"I (123) js: " + wire(),
        b"W (123) js: " + wire(),
        b"\x1b[0;32mI (123) js: " + wire() + b"\x1b[0m\r",
        b"\x1b[0;32m" + wire() + b"\x1b[0m",
        wire() + b"\r",
    ],
)
def test_logger_event_normalization_never_admits_prefixed_or_colored_tlm(line):
    c = collector()
    c.feed(line)
    assert not c.snapshot().samples and not c.snapshot().events
    assert c.snapshot().quality.missing
    with pytest.raises(ValueError, match="^invalid_derby_telemetry$"):
        parse_telemetry_line(line)


def test_source_prefixed_camera_event_breaks_cross_camera_window():
    c = collector()
    c.feed(wire())
    c.feed(b"I (123) js: DERBY CAM CLOSE")
    c.feed(later())
    assert not c.snapshot().cameras


def test_missing_heap_and_checkpoint_values_stay_unavailable():
    c = collector()
    assert c.snapshot().sampled_heap_lower_bound_bytes is None
    c.feed(wire(gu_sample_max=0, eval_used=0, eval_cap=0))
    report = c.snapshot()
    assert report.sampled_heap_lower_bound_bytes is None
    checkpoint = report.evaluation_checkpoints[0]
    assert checkpoint.used_bytes is None and checkpoint.cap_bytes is None
    assert checkpoint.checkpoint_headroom_bytes is None
    # Existing persisted diagnostics deliberately omit None-valued fields.
    assert DerbyDiagnostics.model_validate_json(report.model_dump_json(exclude_none=True)) == report


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


def test_actual_json_byte_limit_reserves_future_quality_and_time_growth(monkeypatch):
    now = [0.0]
    c = DerbyDiagnosticsCollector(clock=lambda: now[0], origin=0.0)
    empty_bytes = len(c.snapshot().model_dump_json().encode())
    monkeypatch.setattr(telemetry, "MAX_REPORT_BYTES", empty_bytes + 1800)
    for seq in range(1, 20):
        c.feed(wire(seq=seq))
    first = c.snapshot()
    assert first.quality.report_byte_limit and first.quality.truncated
    assert 0 < len(first.samples) < 19
    now[0] = 180.0
    c.mark_partial_line_or_fault()
    for _ in range(100):
        c.feed(b"DERBY_TLM SECRET")
    final = c.snapshot()
    assert final.samples == first.samples
    assert len(final.model_dump_json().encode()) <= telemetry.MAX_REPORT_BYTES
    assert final.quality.partial_line_or_fault_possible


def test_real_byte_cap_with_maximum_width_native_fields_and_events():
    c = collector()
    for _ in range(MAX_EVENTS):
        c.feed(b"DERBY FRAMEFAIL photo PRIVATE_SECRET")
    for seq in range(1, MAX_SAMPLES + 1):
        c.feed(
            wire(
                run=seq,
                seq=seq,
                us=(1 << 63) - 1,
                frame=U32_MAX,
                ctx=U32_MAX,
                seed="FFFFFFFF",
                race=U32_MAX,
                step=U32_MAX,
                cap=U32_MAX,
                gu_sample_max=U32_MAX,
                eval_used=U32_MAX,
                eval_cap=U32_MAX,
                refused=U32_MAX,
                io_us_max=U32_MAX,
                intervals=",".join([f"{U32_MAX}:{U64_MAX}"] * 10),
            )
        )
    report = c.snapshot()
    assert report.quality.report_byte_limit and report.quality.truncated
    assert 0 < len(report.samples) < MAX_SAMPLES
    assert len(report.model_dump_json().encode("utf-8")) <= MAX_REPORT_BYTES
    assert "PRIVATE_SECRET" not in report.model_dump_json()


def test_finite_extreme_clock_is_clamped_before_multiplication():
    c = DerbyDiagnosticsCollector(clock=lambda: 1e308, origin=0.0)
    c.feed(wire())
    assert c.snapshot().finished_at_ms == 180000


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -1.0])
def test_invalid_clock_never_raises_or_becomes_nonfinite_json(bad):
    c = DerbyDiagnosticsCollector(clock=lambda: bad, origin=0.0)
    c.feed(wire())
    report = c.snapshot()
    assert report.quality.invalid_clock
    assert report.finished_at_ms == 0
    json.loads(report.model_dump_json())


def test_clock_failure_backwards_partial_and_clear_drop_references():
    now = [1.0]
    c = DerbyDiagnosticsCollector(clock=lambda: now[0], origin=0.0)
    c.feed(wire())
    now[0] = 0.5
    c.feed(later())
    assert c.snapshot().quality.invalid_clock
    c.mark_partial_line_or_fault()
    assert c.snapshot().quality.partial_line_or_fault_possible
    snapshot = c.snapshot()
    c.clear()
    assert not c.snapshot().samples and not c.snapshot().events
    assert len(snapshot.samples) == 2

    def broken():
        raise RuntimeError("PRIVATE")

    c = DerbyDiagnosticsCollector(clock=broken)
    assert c.snapshot().quality.invalid_clock
    assert "PRIVATE" not in c.snapshot().model_dump_json()
