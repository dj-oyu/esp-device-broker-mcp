"""Offline receive diagnostics: invented content must never survive observation."""

import json

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


def counters(**updates: int) -> dict[str, int]:
    return dict(dict.fromkeys(COUNTERS, 0), **updates)


@pytest.mark.parametrize("chunk_size", [1, 7, 4096])
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
    assert set(state) == {
        "_counts",
        "_before_command",
        "_last_command",
        "_write_completed",
        "_home_shape",
        "_home_candidates",
        "_cross_command_partial",
    }
    assert all(type(value) is int for value in state["_counts"].values())
    assert all(type(value) is int for value in state["_before_command"].values())
    assert "INVENTED" not in json.dumps(state)
    assert not hasattr(diagnostics, "__dict__")


class CustomString(str):
    pass


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
