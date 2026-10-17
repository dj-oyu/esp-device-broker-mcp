"""Invented records only: diagnostic shape never grants protocol acceptance."""

import json

import pytest
from test_serial_diagnostics_review import make_reader

from cardputer_mcp.serial_backend import SerialFailure
from cardputer_mcp.serial_diagnostics import ReceiveDiagnostics, sanitize_receive_summary


def summary(raw=b"I (1) shell: HOME_READY ", *, partial=False):
    diagnostics = ReceiveDiagnostics()
    diagnostics.begin_command("home_escape", cross_command_partial=partial)
    diagnostics.observe_line(raw)
    return diagnostics, diagnostics.snapshot(0)


@pytest.mark.parametrize(
    "raw,prefix,offset,after,kind",
    [
        (b"I (1) shell: HOME_READY", 1, 0, 0, "exact"),
        (b"\x1b[32mI (1) shell: HOME_READY\x1b[0m\r", 1, 0, 0, "exact"),
        (b"I (1) shell:  HOME_READY\t", 1, 1, 1, "horizontal_padding"),
        (b"I (1) shell: HOME_READY suffix", 1, 0, 7, "other_surrounding"),
        (b"I (1) shell: mention HOME_READY here", 1, 8, 5, "other_surrounding"),
        (b"I (1) shell: I (2) shell: HOME_READY", 2, 13, 0, "repeated_prefix"),
        (b"I(1) shell: HOME_READY", 0, 12, 0, "other_surrounding"),
    ],
)
def test_shapes_distinguish_complete_candidate_mismatches(raw, prefix, offset, after, kind):
    _, value = summary(raw)
    shape = value["home_marker_shape"]
    assert shape == {
        "candidate_lines": 1,
        "standard_prefix_count": prefix,
        "token_offset": offset,
        "after_bytes": after,
        "before_horizontal_only": offset == 0 or kind == "horizontal_padding",
        "after_horizontal_only": after == 0 or kind == "horizontal_padding",
        "cross_command_partial": False,
        "normalization": kind,
    }
    assert sanitize_receive_summary(value) == value


def test_only_first_candidate_is_kept_counts_saturate_and_snapshots_are_detached():
    diagnostics, first = summary(partial=True)
    for _ in range(20):
        diagnostics.observe_line(b"I (2) shell: mention HOME_READY private")
    latest = diagnostics.snapshot(0)
    assert latest["home_marker_shape"] == dict(first["home_marker_shape"], candidate_lines=2)
    first["home_marker_shape"]["normalization"] = "INVENTED_SECRET"
    assert "INVENTED" not in json.dumps(diagnostics.snapshot(0))
    diagnostics.begin_command("home_quit")
    assert "home_marker_shape" not in diagnostics.snapshot(0)
    diagnostics.observe_line(b"HOME_READY")
    assert diagnostics.snapshot(0)["home_marker_shape"]["cross_command_partial"] is False


@pytest.mark.parametrize("command", ["none", "capture_frame", "enter", "select_apps"])
def test_non_home_commands_and_pre_command_records_have_no_shape(command):
    diagnostics = ReceiveDiagnostics()
    diagnostics.observe_line(b"HOME_READY")
    diagnostics.begin_command(command)
    diagnostics.observe_line(b"HOME_READY")
    assert "home_marker_shape" not in diagnostics.snapshot(0)


def test_state_contains_only_bounded_counts_flags_fixed_enums_and_no_wire_material():
    secret = b"credential=INVENTED_SECRET /private/INVENTED_PATH image=01230123 "
    diagnostics, value = summary(secret * 400 + b"HOME_READY " + secret * 400)
    shape = value["home_marker_shape"]
    assert shape["token_offset"] == shape["after_bytes"] == 16384
    state = {key: getattr(diagnostics, key) for key in diagnostics.__slots__}
    for material in ("INVENTED", "private/", "01230123", "credential", "HOME_READY"):
        assert material not in json.dumps(state)
    assert len(json.dumps(shape)) < 300
    _, prefixed = summary(b"I (1) shell: " * 100 + b"HOME_READY")
    assert prefixed["home_marker_shape"]["standard_prefix_count"] == 4


@pytest.mark.parametrize(
    "field", ["candidate_lines", "standard_prefix_count", "token_offset", "after_bytes"]
)
@pytest.mark.parametrize("bad", [True, "INVENTED_SECRET", 1.5, None, []])
def test_shape_sanitizer_rejects_untyped_counts(field, bad):
    _, value = summary()
    value["home_marker_shape"][field] = bad
    assert sanitize_receive_summary(value) is None


@pytest.mark.parametrize(
    "field,bad",
    [
        ("normalization", "INVENTED_SECRET"),
        ("before_horizontal_only", 1),
        ("after_horizontal_only", "True"),
        ("cross_command_partial", None),
        ("raw", "INVENTED_SECRET"),
    ],
)
def test_shape_sanitizer_rejects_unrecognized_fields_or_categories(field, bad):
    _, value = summary()
    value["home_marker_shape"][field] = bad
    assert sanitize_receive_summary(value) is None


def test_shape_sanitizer_bounds_copies_and_rejects_missing_or_custom_fields():
    _, value = summary()
    shape = value["home_marker_shape"]
    for key in ("candidate_lines", "standard_prefix_count", "token_offset", "after_bytes"):
        shape[key] = 2**100
    clean = sanitize_receive_summary(value)
    assert clean["home_marker_shape"]["candidate_lines"] == 2
    assert clean["home_marker_shape"]["standard_prefix_count"] == 4
    assert clean["home_marker_shape"]["token_offset"] == 16384
    shape["normalization"] = "INVENTED_SECRET"
    assert "INVENTED" not in json.dumps(clean)
    del shape["normalization"]
    assert sanitize_receive_summary(value) is None


@pytest.mark.parametrize("change", ["container", "count", "category", "key", "command"])
def test_shape_sanitizer_rejects_subclasses_and_wrong_command(change):
    class CustomDict(dict):
        pass

    class CustomInt(int):
        pass

    class CustomStr(str):
        pass

    _, value = summary()
    shape = value["home_marker_shape"]
    if change == "container":
        value["home_marker_shape"] = CustomDict(shape)
    elif change == "count":
        shape["candidate_lines"] = CustomInt(1)
    elif change == "category":
        shape["normalization"] = CustomStr("exact")
    elif change == "key":
        shape[CustomStr("token_offset")] = shape.pop("token_offset")
    else:
        value["last_command"] = "capture_frame"
    assert sanitize_receive_summary(value) is None


@pytest.mark.parametrize("split", [1, 3, 4096])
def test_reader_timeout_exports_shape_without_accepting_noisy_home(split):
    reader, port, events = make_reader(b"I (1) shell: HOME_READY INVENTED_SECRET\r\n", split=split)
    with pytest.raises(SerialFailure) as error:
        reader.command(b"q", rb"HOME_READY", timeout=5)
    assert error.value.code == "serial_marker_timeout"
    shape = error.value.diagnostic["receive_summary"]["home_marker_shape"]
    assert shape["normalization"] == "other_surrounding"
    assert shape["after_bytes"] == 16
    assert "INVENTED" not in json.dumps(error.value.diagnostic)
    assert port.writes == [b"q"]
    assert events == ["serial_wait_command"]


def test_reader_marks_cross_command_partial_candidate_without_accepting_it():
    reader, port, _ = make_reader(b"READY\n", preloaded=(b"HOME_",))
    with pytest.raises(SerialFailure):
        reader.command(b"q", rb"HOME_READY", timeout=2)
    shape = reader.failure("serial_marker_timeout").diagnostic["receive_summary"][
        "home_marker_shape"
    ]
    assert shape["normalization"] == "exact"
    assert shape["cross_command_partial"] is True
    assert port.writes == [b"q"]
