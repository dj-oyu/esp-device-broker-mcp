"""Offline receive diagnostics: invented content must never survive observation."""

import json
from typing import Any

import pytest

from cardputer_mcp.serial_diagnostics import ReceiveDiagnostics, sanitize_receive_summary

COUNTERS = (
    "bytes",
    "complete_lines",
    "esp_log_lines",
    "ansi_lines",
    "home_ready_token_lines",
    "home_ready_exact_lines",
    "boot_banner_lines",
    "reset_reason_lines",
    "capture_record_lines",
    "binary_control_lines",
    "unclassified_lines",
)
COMMANDS = (
    "none",
    "home_escape",
    "home_quit",
    "select_apps",
    "select_previous",
    "enter",
    "capture_frame",
)


def counters(**updates: int) -> dict[str, int]:
    return dict(dict.fromkeys(COUNTERS, 0), **updates)


def test_receive_summary_starts_empty_and_has_only_fixed_fields() -> None:
    assert ReceiveDiagnostics().snapshot(0) == {
        "last_command": "none",
        "command_write_completed": False,
        "before_command": counters(),
        "after_command": counters(),
        "pending_bytes": 0,
    }


@pytest.mark.parametrize("chunk_size", [1, 2, 7, 4096])
def test_receive_summary_is_independent_of_byte_chunking(chunk_size: int) -> None:
    lines = (
        b"\x1b[0;32mI (42) shell: HOME_READY\x1b[0m\r",
        b"unknown HOME_READY token=INVENTED_TOKEN",
        b"ESP-ROM:esp32s3-INVENTED_DEVICE_ID",
        b"rst:0x1 (POWERON),boot:0x8 (SPI_FAST_FLASH_BOOT)",
        b"CAPTURE_BEGIN 240 135",
        b"PIX 0 " + b"0123" * 240,
        b"CAPTURE_END",
        b"\x00\xff\x7fprivate=/private/INVENTED_PATH",
        b"serial=INVENTED_SERIAL_NUMBER",
        b"\x1b[31mcolored but otherwise unknown\x1b[0m",
        b"W (7) network: credential=INVENTED_CREDENTIAL",
        b"HOME_READY_EXTRA",
    )
    wire = b"\n".join(lines) + b"\nINVENTED_PENDING_TOKEN"
    diagnostics = ReceiveDiagnostics()
    pending = b""
    # This is a synthetic caller, not an actual serial reader or device port.
    for offset in range(0, len(wire), chunk_size):
        chunk = wire[offset : offset + chunk_size]
        diagnostics.observe_bytes(chunk)
        pending += chunk
        while b"\n" in pending:
            raw, pending = pending.split(b"\n", 1)
            diagnostics.observe_line(raw)
    summary = diagnostics.snapshot(len(pending))
    assert summary["after_command"] == counters(
        bytes=len(wire),
        complete_lines=12,
        esp_log_lines=2,
        ansi_lines=2,
        home_ready_token_lines=2,
        home_ready_exact_lines=1,
        boot_banner_lines=1,
        reset_reason_lines=1,
        capture_record_lines=3,
        binary_control_lines=1,
        unclassified_lines=2,
    )
    assert summary["pending_bytes"] == len(pending)
    public = json.dumps(summary)
    assert "INVENTED" not in public
    assert "01230123" not in public
    assert "private/" not in public
    assert sanitize_receive_summary(summary) == summary


@pytest.mark.parametrize(
    ("line", "indicator"),
    [
        (b"I(42) shell: HOME_READY", "home_ready_token_lines"),
        (b"prefix HOME_READY suffix", "home_ready_token_lines"),
        (b"[HOME_READY]", "home_ready_token_lines"),
        (b"HOME_READY\t", "home_ready_token_lines"),
        (b"HOME_READY!", "home_ready_token_lines"),
        (b"AHOME_READY", "unclassified_lines"),
        (b"HOME_READY1", "unclassified_lines"),
        (b"HOME_READY_SUFFIX", "unclassified_lines"),
        (b"rst:0xNOT_A_HEX_REASON", "unclassified_lines"),
        (b"prefix rst:0x1 (POWERON)", "unclassified_lines"),
        (b"prefix ESP-ROM:esp32s3", "unclassified_lines"),
        (b"\x1b]unrecognized escape", "binary_control_lines"),
        (b"embedded\rcontrol", "binary_control_lines"),
        (b"ordinary\ttab\r", "unclassified_lines"),
    ],
)
def test_receive_summary_classifications_are_conservative(line: bytes, indicator: str) -> None:
    diagnostics = ReceiveDiagnostics()
    diagnostics.observe_line(line)
    assert diagnostics.snapshot(0)["after_command"] == counters(complete_lines=1, **{indicator: 1})


@pytest.mark.parametrize("severity", [b"I", b"W", b"E", b"V", b"D"])
def test_receive_summary_esp_log_normalization_matches_control(severity: bytes) -> None:
    # Import locally to keep the pure diagnostic module independent of hardware.
    from cardputer_mcp.serial_backend import clean_line

    line = b"\x1b[0m\x1b[32m" + severity + b" (123) app.name-1: HOME_READY\x1b[0m\r"
    assert clean_line(line) == b"HOME_READY"
    diagnostics = ReceiveDiagnostics()
    diagnostics.observe_line(line)
    assert diagnostics.snapshot(0)["after_command"] == counters(
        complete_lines=1,
        esp_log_lines=1,
        ansi_lines=1,
        home_ready_token_lines=1,
        home_ready_exact_lines=1,
    )


def test_receive_summary_bytes_and_lines_are_independent() -> None:
    diagnostics = ReceiveDiagnostics()
    diagnostics.observe_bytes(b"HOME_READY\n")
    assert diagnostics.snapshot(0)["after_command"] == counters(bytes=11)
    diagnostics.observe_line(b"unclassified")
    assert diagnostics.snapshot(0)["after_command"] == counters(
        bytes=11, complete_lines=1, unclassified_lines=1
    )


@pytest.mark.parametrize("command", COMMANDS)
def test_receive_summary_snapshots_command_baselines_and_write_completion(command: str) -> None:
    diagnostics = ReceiveDiagnostics()
    diagnostics.observe_bytes(b"prior\n")
    diagnostics.observe_line(b"prior")
    diagnostics.begin_command(command)
    before = counters(bytes=6, complete_lines=1, unclassified_lines=1)
    assert diagnostics.snapshot(0) == {
        "last_command": command,
        "command_write_completed": False,
        "before_command": before,
        "after_command": counters(),
        "pending_bytes": 0,
    }
    diagnostics.write_completed()
    diagnostics.observe_bytes(b"HOME_READY\n")
    diagnostics.observe_line(b"HOME_READY")
    summary = diagnostics.snapshot(0)
    assert summary["before_command"] == before
    assert summary["command_write_completed"] is True
    assert summary["after_command"] == counters(
        bytes=11, complete_lines=1, home_ready_token_lines=1, home_ready_exact_lines=1
    )
    diagnostics.begin_command("capture_frame")
    latest = diagnostics.snapshot(0)
    assert latest["before_command"] == counters(
        bytes=17,
        complete_lines=2,
        unclassified_lines=1,
        home_ready_token_lines=1,
        home_ready_exact_lines=1,
    )
    assert latest["after_command"] == counters()
    assert latest["command_write_completed"] is False
    assert summary["before_command"] == before


def test_receive_summary_snapshots_are_detached() -> None:
    diagnostics = ReceiveDiagnostics()
    diagnostics.observe_bytes(b"old")
    diagnostics.begin_command("home_escape")
    snapshot = diagnostics.snapshot(0)
    snapshot["before_command"]["bytes"] = 9000
    snapshot["after_command"]["bytes"] = 9000
    snapshot["last_command"] = "INVENTED_SECRET"
    diagnostics.observe_bytes(b"new")
    latest = diagnostics.snapshot(0)
    assert latest["before_command"]["bytes"] == 3
    assert latest["after_command"]["bytes"] == 3
    assert latest["last_command"] == "home_escape"


def test_receive_summary_retains_no_input_in_attributes() -> None:
    diagnostics = ReceiveDiagnostics()
    secret = b"token=INVENTED_SECRET serial=INVENTED_DEVICE /private/INVENTED_PATH"
    diagnostics.observe_bytes(secret)
    diagnostics.observe_line(secret)
    diagnostics.begin_command(secret.decode("ascii"))
    diagnostics.observe_line(b"CAPTURE_BEGIN " + secret)
    diagnostics.observe_line(b"PIX " + secret)
    state = {name: getattr(diagnostics, name) for name in ReceiveDiagnostics.__slots__}
    assert state["_last_command"] == "none"
    assert set(state) == {"_counts", "_before_command", "_last_command", "_write_completed"}
    assert all(type(value) is int for value in state["_counts"].values())
    assert all(type(value) is int for value in state["_before_command"].values())
    assert "INVENTED" not in json.dumps(state)
    assert not hasattr(diagnostics, "__dict__")


def test_receive_summary_observed_counters_saturate() -> None:
    diagnostics = ReceiveDiagnostics()
    diagnostics.observe_bytes(b"x" * (1048576 + 1))
    for _ in range(65537):
        diagnostics.observe_line(b"I (1) shell: HOME_READY")
    assert diagnostics.snapshot(2**100)["after_command"] == counters(
        bytes=1048576,
        complete_lines=65536,
        esp_log_lines=65536,
        home_ready_token_lines=65536,
        home_ready_exact_lines=65536,
    )
    assert diagnostics.snapshot(2**100)["pending_bytes"] == 16384
    assert diagnostics.snapshot(-3)["pending_bytes"] == 0
    diagnostics.begin_command("home_escape")
    diagnostics.observe_bytes(b"x")
    diagnostics.observe_line(b"HOME_READY")
    assert diagnostics.snapshot(0)["after_command"] == counters()


class Unrepresentable:
    def __str__(self) -> str:
        raise AssertionError("must not stringify arbitrary input")

    def __repr__(self) -> str:
        raise AssertionError("must not represent arbitrary input")

    def __int__(self) -> int:
        raise AssertionError("must not coerce arbitrary input")


class CustomInt(int):
    pass


class CustomString(str):
    pass


class CustomDict(dict[str, Any]):
    pass


def test_receive_summary_sanitizer_copies_and_clamps_valid_counts() -> None:
    source = ReceiveDiagnostics().snapshot(0)
    source["pending_bytes"] = 2**100
    source["before_command"] = dict.fromkeys(COUNTERS, -100)
    source["after_command"] = dict.fromkeys(COUNTERS, 2**100)
    result = sanitize_receive_summary(source)
    assert result is not None
    assert result["before_command"] == counters()
    assert result["after_command"] == {
        name: 1048576 if name == "bytes" else 65536 for name in COUNTERS
    }
    assert result["pending_bytes"] == 16384
    source["after_command"]["bytes"] = "INVENTED_SECRET"
    source["before_command"]["bytes"] = "INVENTED_SECRET"
    assert "INVENTED" not in json.dumps(result)
    source["pending_bytes"] = -1
    source["after_command"] = counters()
    source["before_command"] = counters()
    assert sanitize_receive_summary(source)["pending_bytes"] == 0  # type: ignore[index]


@pytest.mark.parametrize("command", COMMANDS)
def test_receive_summary_sanitizer_accepts_all_fixed_commands(command: str) -> None:
    source = ReceiveDiagnostics().snapshot(0)
    source["last_command"] = command
    assert sanitize_receive_summary(source) == source


@pytest.mark.parametrize("invalid", [None, [], "INVENTED_SECRET", 1, True, Unrepresentable()])
def test_receive_summary_sanitizer_rejects_invalid_container(invalid: object) -> None:
    assert sanitize_receive_summary(invalid) is None


@pytest.mark.parametrize(
    ("field", "invalid"),
    [
        ("last_command", "token=INVENTED_SECRET"),
        ("last_command", ["home_escape"]),
        ("last_command", CustomString("home_escape")),
        ("last_command", Unrepresentable()),
        ("command_write_completed", 1),
        ("command_write_completed", "True"),
        ("command_write_completed", Unrepresentable()),
        ("pending_bytes", True),
        ("pending_bytes", 1.0),
        ("pending_bytes", "100"),
        ("pending_bytes", CustomInt(1)),
        ("pending_bytes", Unrepresentable()),
        ("before_command", None),
        ("before_command", {"bytes": "INVENTED_SECRET"}),
        ("after_command", {"bytes": {"secret": "INVENTED_SECRET"}}),
        ("after_command", CustomDict(counters())),
    ],
)
def test_receive_summary_sanitizer_rejects_invalid_fields(field: str, invalid: object) -> None:
    source = ReceiveDiagnostics().snapshot(0)
    source[field] = invalid
    assert sanitize_receive_summary(source) is None


@pytest.mark.parametrize("field", ["before_command", "after_command"])
@pytest.mark.parametrize("invalid", [True, "12", 12.0, None, [], CustomInt(1), Unrepresentable()])
def test_receive_summary_sanitizer_never_coerces_counts(field: str, invalid: object) -> None:
    source = ReceiveDiagnostics().snapshot(0)
    source[field]["bytes"] = invalid
    assert sanitize_receive_summary(source) is None


@pytest.mark.parametrize("container", [None, "before_command", "after_command"])
@pytest.mark.parametrize("change", ["missing", "extra", "custom_key"])
def test_receive_summary_sanitizer_rejects_schema_changes(
    container: str | None, change: str
) -> None:
    source = ReceiveDiagnostics().snapshot(0)
    target = source if container is None else source[container]
    field = "pending_bytes" if container is None else "bytes"
    if change == "missing":
        target.pop(field)
    elif change == "extra":
        target["private_path"] = "/private/INVENTED_SECRET"
    else:
        value = target.pop(field)
        target[CustomString(field)] = value
    assert sanitize_receive_summary(source) is None


def test_receive_summary_rejects_custom_outer_dict_without_conversion() -> None:
    assert sanitize_receive_summary(CustomDict(ReceiveDiagnostics().snapshot(0))) is None


def test_receive_summary_invalid_runtime_command_or_pending_count_is_not_retained() -> None:
    diagnostics = ReceiveDiagnostics()
    diagnostics.begin_command(Unrepresentable())  # type: ignore[arg-type]
    assert diagnostics.snapshot(True)["pending_bytes"] == 0
    assert diagnostics.snapshot(Unrepresentable())["pending_bytes"] == 0  # type: ignore[arg-type]
    assert diagnostics.snapshot(0)["last_command"] == "none"
