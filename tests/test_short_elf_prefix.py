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


@pytest.mark.parametrize("length", [8, 12, 16])
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
    data = _startup(_app_line(b"ELF file SHA256:  " + prefix + b"..."))
    for split in range(len(data) + 1):
        collector = DeviceLogCollector()
        assert collector.feed(data[:split])
        assert collector.feed(data[split:])
        assert collector.finish() == _collect(data)
    unavailable = _collect(_app_line(b"ELF file SHA256:  " + prefix + b"...")).firmware
    assert unavailable.status == "unavailable" and unavailable.elf_sha256_prefix is None


@pytest.mark.parametrize(
    "value",
    [
        b"ABCDEF012...",
        b"abcdef0...",
        b"abcdef0123456789a...",
        b"abcdef012..",
        b"abcdef012... suffix",
        _SYNTHETIC_HEX * 4,
    ],
)
def test_prefix_requires_exact_lowercase_hex_and_three_periods(value: bytes) -> None:
    firmware = _collect(_startup(_app_line(b"ELF file SHA256:  " + value))).firmware
    assert firmware.status == "partial"
    assert firmware.reason == "invalid_startup_field"
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
