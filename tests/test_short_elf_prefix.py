"""Synthetic ESP-IDF startup prefixes; no device logs or hardware access."""

import pytest

from cardputer_mcp.device_observation import DeviceLogCollector, DeviceObservation

_SYNTHETIC_HEX = b"abcdef0123456789"


def _app_line(body: bytes) -> bytes:
    return b"I (123) app_init: " + body + b"\n"


def _startup(elf_line: bytes) -> bytes:
    return (
        _app_line(b"Application information:")
        + _app_line(b"Project name: synthetic_test")
        + _app_line(b"App version: v1.2.3")
        + elf_line
        + _app_line(b"ESP-IDF: v6.0.1")
    )


def _collect(data: bytes) -> DeviceObservation:
    collector = DeviceLogCollector()
    assert collector.feed(data)
    return collector.finish()


@pytest.mark.parametrize("length", range(8, 17))
def test_all_source_supported_prefix_lengths_remain_unverified(length: int) -> None:
    prefix = _SYNTHETIC_HEX[:length]
    report = _collect(_startup(_app_line(b"ELF file SHA256:  " + prefix + b"...")))
    firmware = report.metadata()["firmware"]
    assert firmware["status"] == "observed"
    assert firmware["reason"] == "complete_startup_block"
    assert firmware["elf_sha256_prefix"] == prefix.decode("ascii")
    assert firmware["source_verified"] is False
    assert firmware["binary_sha256_verified"] is False
    assert "neither a full ELF digest nor a .bin digest" in firmware["limitations"]
    # The only firmware digest field retains exactly the observed prefix.
    assert {key for key in firmware if "sha256" in key} == {
        "elf_sha256_prefix",
        "binary_sha256_verified",
    }


@pytest.mark.parametrize("length", [0, 1, 7, 17, 32, 63, 64, 65])
def test_prefix_length_outside_printer_bounds_is_invalid(length: int) -> None:
    value = (_SYNTHETIC_HEX * 5)[:length] + b"..."
    firmware = _collect(_startup(_app_line(b"ELF file SHA256:  " + value))).firmware
    assert firmware.status == "partial"
    assert firmware.reason == "invalid_startup_field"
    assert firmware.elf_sha256_prefix is None


@pytest.mark.parametrize(
    "value",
    [
        b"ABCDEF012...",
        b"abcdef01A...",
        b"abcdef01g...",
        b"0xabcdef012...",
        b"abcdef012",
        b"abcdef012..",
        b"abcdef012....",
        b"abcdef012\xe2\x80\xa6",
        b"abcdef012... suffix",
        b"prefix abcdef012...",
        b"abcdef012... ",
        b"abcdef012...\t",
        b"abcdef012...\r",
        b"abcd ef012...",
        b"abcd\tef012...",
        _SYNTHETIC_HEX * 4,
    ],
)
def test_prefix_requires_exact_lowercase_hex_and_three_periods(value: bytes) -> None:
    # An embedded CR, unlike a single CRLF line ending, cannot end the value.
    suffix = b"extra" if value.endswith(b"\r") else b""
    firmware = _collect(_startup(_app_line(b"ELF file SHA256:  " + value + suffix))).firmware
    assert firmware.status == "partial"
    assert firmware.reason == "invalid_startup_field"
    assert firmware.elf_sha256_prefix is None


@pytest.mark.parametrize("padding", [b"", b"\t", b" \t", b" " * 17])
def test_prefix_does_not_relax_field_whitespace(padding: bytes) -> None:
    firmware = _collect(
        _startup(_app_line(b"ELF file SHA256:" + padding + b"abcdef012..."))
    ).firmware
    assert firmware.status == "partial"
    assert firmware.reason == "invalid_startup_field"
    assert firmware.elf_sha256_prefix is None


@pytest.mark.parametrize("padding", [b" ", b"  ", b" " * 16])
def test_existing_bounded_ascii_padding_is_supported(padding: bytes) -> None:
    firmware = _collect(
        _startup(_app_line(b"ELF file SHA256:" + padding + b"abcdef012..."))
    ).firmware
    assert firmware.status == "observed"
    assert firmware.elf_sha256_prefix == "abcdef012"


@pytest.mark.parametrize(
    "line_prefix",
    [
        b"",
        b"I (123) other: ",
        b"W (123) app_init: ",
        b"I(123) app_init: ",
        b"I (bad) app_init: ",
        b"I (123) app_init_extra: ",
        b"prefix I (123) app_init: ",
    ],
)
def test_prefix_lookalikes_never_become_startup_fields(line_prefix: bytes) -> None:
    firmware = _collect(_startup(line_prefix + b"ELF file SHA256:  abcdef012...\n")).firmware
    assert firmware.status == "partial"
    assert firmware.reason == "incomplete_startup_block"
    assert firmware.elf_sha256_prefix is None


@pytest.mark.parametrize(
    ("second_prefix", "reason"),
    [
        (b"abcdef012", "duplicate_startup_fields"),
        (b"abcdef013", "contradictory_startup_fields"),
        (b"abcdef0123456789", "contradictory_startup_fields"),
    ],
)
def test_duplicate_or_extended_prefixes_clear_candidate_identity(
    second_prefix: bytes, reason: str
) -> None:
    elf_lines = _app_line(b"ELF file SHA256:  abcdef012...") + _app_line(
        b"ELF file SHA256:  " + second_prefix + b"..."
    )
    firmware = _collect(_startup(elf_lines)).firmware
    assert firmware.status == "ambiguous"
    assert firmware.reason == reason
    assert firmware.elf_sha256_prefix is None
    assert firmware.project_name is None
    assert firmware.app_version is None
    assert firmware.idf_version is None


def test_default_nine_character_prefix_survives_every_byte_split() -> None:
    data = _startup(_app_line(b"ELF file SHA256:  abcdef012..."))
    expected = _collect(data)
    for split in range(len(data) + 1):
        collector = DeviceLogCollector()
        assert collector.feed(data[:split])
        assert collector.feed(data[split:])
        assert collector.finish() == expected


def test_prefix_remains_unavailable_without_startup_header() -> None:
    firmware = _collect(_app_line(b"ELF file SHA256:  abcdef012...")).firmware
    assert firmware.status == "unavailable"
    assert firmware.reason == "no_startup_block"
    assert firmware.elf_sha256_prefix is None
