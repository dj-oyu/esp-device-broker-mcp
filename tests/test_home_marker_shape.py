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


@pytest.mark.parametrize("split", [1, 4096])
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
