"""Pure synthetic console fixtures; these tests never access a serial device."""

import hashlib
import json
from dataclasses import FrozenInstanceError

import pytest

from cardputer_mcp.device_observation import (
    MAX_LINE_BYTES,
    MAX_RECEIVE_BYTES,
    MAX_RENDERED_BYTES,
    MAX_RETAINED_LINES,
    DeviceLogCollector,
    DeviceObservation,
)


def app_line(body: bytes, timestamp: bytes = b"123") -> bytes:
    return b"I (" + timestamp + b") app_init: " + body + b"\n"


def startup(*, optional: bool = True) -> bytes:
    rows = [b"Application information:", b"Project name: cardputer_test", b"App version: v1.2.3"]
    if optional:
        rows += [b"Secure version: 7", b"Compile time: Oct  3 2026 12:34:56"]
    rows += [b"ELF file SHA256: 0123456789abcdef...", b"ESP-IDF: v6.0.1"]
    return b"".join(app_line(row) for row in rows)


def collect(data: bytes, chunk_size: int = 4096) -> DeviceObservation:
    collector = DeviceLogCollector()
    for offset in range(0, len(data), chunk_size):
        if not collector.feed(data[offset : offset + chunk_size]):
            break
    return collector.finish()


def test_empty_observation_has_fixed_metadata_and_no_identity_claim() -> None:
    report = collect(b"")
    metadata = report.metadata()
    assert metadata["schema_version"] == 1
    assert metadata["bytes_received"] == metadata["complete_lines"] == 0
    assert metadata["retained_lines"] == metadata["rendered_bytes"] == 0
    assert metadata["redacted_lines"] == metadata["omitted_lines"] == 0
    assert metadata["stop_reason"] == "finished"
    assert metadata["partial"] is metadata["truncated"] is False
    assert "logs" not in metadata
    assert report.logs == ()
    assert metadata["firmware"]["status"] == "unavailable"
    assert metadata["firmware"]["reason"] == "no_startup_block"
    assert metadata["firmware"]["binary_sha256_verified"] is False
    assert metadata["firmware"]["source_verified"] is False
    assert "unknown" in metadata["redaction_limitations"].lower()
    assert ".bin" in metadata["firmware"]["limitations"]


def test_gap_never_joins_secret_suffix_or_old_identity_fields() -> None:
    collector = DeviceLogCollector()
    collector.feed(app_line(b"Application information:") + b"password=")
    collector.mark_gap()
    collector.feed(b"SYNTHETIC_PRIVATE_SUFFIX\n" + app_line(b"App version: v2.3.4"))
    report = collector.finish()
    assert report.capture_gap and report.pending_line_omitted
    assert report.omitted_lines == 2
    assert "SYNTHETIC_PRIVATE_SUFFIX" not in json.dumps(report.payload())
    assert report.firmware.status == "unavailable"


def test_gap_accepts_only_fresh_exact_startup_header_as_first_line() -> None:
    collector = DeviceLogCollector()
    collector.feed(startup() + b"uncompleted old line")
    collector.mark_gap()
    collector.feed(startup())
    report = collector.finish()
    assert report.capture_gap and report.metadata()["partial"]
    assert report.firmware.status == "observed"
    assert report.firmware.app_version == "v1.2.3"


@pytest.mark.parametrize("prefix", [b"", b"I (1) app_init: ", b"NOISY "])
def test_gap_unknown_first_line_cannot_expose_its_suffix(prefix: bytes) -> None:
    collector = DeviceLogCollector()
    collector.mark_gap()
    collector.feed(prefix + b"SYNTHETIC_PRIVATE_SUFFIX\nordinary next line\n")
    report = collector.finish()
    assert report.logs == ("ordinary next line",)
    assert report.omitted_lines == 1 and report.capture_gap


@pytest.mark.parametrize(
    "opener", [b"-----BEGIN PRIVATE KEY-----\n", b"-----BEGIN PRIVATE KEY-----"]
)
def test_gap_preserves_pem_state_even_from_pending_opener(opener: bytes) -> None:
    collector = DeviceLogCollector()
    collector.feed(opener)
    collector.mark_gap()
    collector.feed(startup() + b"SYNTHETIC_KEY_BODY\n-----END PRIVATE KEY-----\nordinary\n")
    report = collector.finish()
    assert "SYNTHETIC_KEY_BODY" not in json.dumps(report.payload())
    assert report.firmware.status == "unavailable"
    assert report.logs[-1] == "ordinary"


def test_gap_omitted_first_pem_opener_still_redacts_its_body() -> None:
    collector = DeviceLogCollector()
    collector.mark_gap()
    collector.feed(b"-----BEGIN PRIVATE KEY-----\nSYNTHETIC_KEY_BODY\n-----END PRIVATE KEY-----\n")
    report = collector.finish()
    assert report.omitted_lines == 1
    assert report.redacted_lines == 2
    assert "SYNTHETIC_KEY_BODY" not in json.dumps(report.payload())


def test_gap_does_not_reopen_sealed_collector_or_renew_receive_budget() -> None:
    collector = DeviceLogCollector()
    collector.feed(b"ordinary\n")
    report = collector.finish()
    collector.mark_gap()
    assert collector.feed(startup()) is False
    assert collector.finish() is report and not report.capture_gap


@pytest.mark.parametrize("chunk_size", [1, 2, 7, 1024, 65536])
def test_results_are_independent_of_byte_chunking(chunk_size: int) -> None:
    data = (
        b"normal line\n\x1b[32mcolored\x1b[0m\r\n"
        + startup()
        + b"password=SYNTHETIC_CREDENTIAL\nPIX 0 1234\n"
        + "\tutf8 café\r\n".encode()
        + b"unfinished"
    )
    assert collect(data, chunk_size) == collect(data)


def test_startup_parser_accepts_every_possible_split_boundary() -> None:
    data = startup()
    expected = collect(data)
    for split in range(len(data) + 1):
        collector = DeviceLogCollector()
        assert collector.feed(data[:split])
        assert collector.feed(data[split:])
        assert collector.finish() == expected


@pytest.mark.parametrize("optional", [True, False])
def test_reads_only_strict_startup_fields(optional: bool) -> None:
    report = collect(startup(optional=optional))
    firmware = report.metadata()["firmware"]
    assert firmware["status"] == "observed"
    assert firmware["reason"] == "complete_startup_block"
    assert firmware["project_name"] == "cardputer_test"
    assert firmware["app_version"] == "v1.2.3"
    assert firmware["elf_sha256_prefix"] == "0123456789abcdef"
    assert firmware["idf_version"] == "v6.0.1"
    assert firmware["compile_time"] == ("Oct  3 2026 12:34:56" if optional else None)
    assert firmware["secure_version"] == (7 if optional else None)


def test_known_ansi_and_interleaved_other_logs_do_not_change_identity() -> None:
    rows = startup().splitlines()
    data = b"".join(
        b"\x1b[0;32m"
        + row
        + b"\x1b[0m\r\n"
        + b"I (124) shell: unrelated\n"
        + b"prefix I (124) app_init: App version: v9.9.9\n"
        + b"E (124) app_init: App version: v8.8.8\n"
        for row in rows
    )
    assert collect(data).firmware == collect(startup()).firmware


@pytest.mark.parametrize(
    "header",
    [
        b"Application information:\n",
        b"I (123) other: Application information:\n",
        b"W (123) app_init: Application information:\n",
        b"I(123) app_init: Application information:\n",
        b"I (abc) app_init: Application information:\n",
        b"I (123) app_init: Application information: extra\n",
        b"prefix I (123) app_init: Application information:\n",
    ],
)
def test_header_substrings_or_wrong_prefix_never_establish_identity(header: bytes) -> None:
    data = header + startup().split(b"\n", 1)[1]
    assert collect(data).firmware.status == "unavailable"


@pytest.mark.parametrize(
    ("original", "invalid"),
    [
        (b"Project name: cardputer_test", b"Project name: " + b"a" * 32),
        (b"Project name: cardputer_test", b"Project name: ../private"),
        (b"Project name: cardputer_test", b"Project name: cardputer_test extra"),
        (b"App version: v1.2.3", b"App version: " + b"v1" * 16),
        (b"App version: v1.2.3", b"App version: arbitraryword"),
        (b"App version: v1.2.3", b"App version:\tv1.2.3"),
        (b"App version: v1.2.3", b"App version: v1.2.3 "),
        (b"Compile time: Oct  3 2026 12:34:56", b"Compile time: Oct 33 2026 12:34:56"),
        (b"Compile time: Oct  3 2026 12:34:56", b"Compile time: Feb 30 2026 12:34:56"),
        (b"Compile time: Oct  3 2026 12:34:56", b"Compile time: Oct  3 2026 29:34:56"),
        (b"ELF file SHA256: 0123456789abcdef...", b"ELF file SHA256: 0123456789abcdeF..."),
        (b"ELF file SHA256: 0123456789abcdef...", b"ELF file SHA256: 0123456..."),
        (b"ELF file SHA256: 0123456789abcdef...", b"ELF file SHA256: " + b"a" * 64),
        (b"ESP-IDF: v6.0.1", b"ESP-IDF: SYNTHETIC_IDENTIFIER"),
        (b"ESP-IDF: v6.0.1", b"ESP-IDF: v6.0.1-" + b"a" * 30),
        (b"Secure version: 7", b"Secure version: 4294967296"),
    ],
)
def test_invalid_fields_produce_partial_identity(original: bytes, invalid: bytes) -> None:
    report = collect(startup().replace(original, invalid))
    assert report.firmware.status == "partial"
    assert report.firmware.reason == "invalid_startup_field"


@pytest.mark.parametrize(
    "version", [b"1", b"v1.2.3-4-gabc1234-dirty", b"abcdefa", b"abcdefa-dirty"]
)
def test_plausible_version_and_git_description_values(version: bytes) -> None:
    report = collect(startup().replace(b"v1.2.3", version))
    assert report.firmware.status == "observed"
    assert report.firmware.app_version == version.decode()


def test_missing_or_out_of_order_fields_do_not_claim_complete_identity() -> None:
    missing = startup().replace(app_line(b"App version: v1.2.3"), b"")
    report = collect(missing)
    assert report.firmware.status == "partial"
    assert report.firmware.reason == "incomplete_startup_block"
    assert report.firmware.app_version is None
    rows = startup().splitlines(keepends=True)
    rows[1], rows[2] = rows[2], rows[1]
    assert collect(b"".join(rows)).firmware.status == "partial"


@pytest.mark.parametrize(
    ("extra", "reason"),
    [
        (startup(), "multiple_startup_blocks"),
        (app_line(b"App version: v9.9.9"), "contradictory_startup_fields"),
        (app_line(b"App version: v1.2.3"), "duplicate_startup_fields"),
    ],
)
def test_ambiguous_identity_clears_all_candidate_fields(extra: bytes, reason: str) -> None:
    firmware = collect(startup() + extra).metadata()["firmware"]
    assert firmware["status"] == "ambiguous"
    assert firmware["reason"] == reason
    assert all(
        firmware[name] is None
        for name in (
            "project_name",
            "app_version",
            "compile_time",
            "elf_sha256_prefix",
            "idf_version",
            "secure_version",
        )
    )


@pytest.mark.parametrize(
    "line",
    [
        b"Authorization: Basic SYNTHETIC_CREDENTIAL",
        b"Bearer SYNTHETIC_CREDENTIAL",
        b"PASSWORD = SYNTHETIC_CREDENTIAL",
        b'{"password": "SYNTHETIC_CREDENTIAL"}',
        b"token=SYNTHETIC_CREDENTIAL",
        b"api_key: SYNTHETIC_CREDENTIAL",
        b"API key=SYNTHETIC_CREDENTIAL",
        b"access_token=SYNTHETIC_CREDENTIAL",
        b"AWS_SECRET_ACCESS_KEY=SYNTHETIC_CREDENTIAL",
        b"client_secret=SYNTHETIC_CREDENTIAL",
        b"wifi_password=SYNTHETIC_CREDENTIAL",
        b"sk-proj-SYNTHETIC_CREDENTIAL",
        b"sk-ant-SYNTHETIC_CREDENTIAL",
        b"sk_test_SYNTHETIC_CREDENTIAL",
        b"ghp_SYNTHETIC_CREDENTIAL",
        b"github_pat_SYNTHETIC_CREDENTIAL",
        b"glpat-SYNTHETIC_CREDENTIAL",
        b"xoxb-SYNTHETIC_CREDENTIAL",
        b"AKIASYNTHETICCREDENTIAL",
        b"AIzaSYNTHETICCREDENTIAL",
        b"SG.SYNTHETIC_CREDENTIAL",
        b"https://synthetic:SYNTHETIC_CREDENTIAL@example.invalid/",
        b"pass\x1b[32mword=SYNTHETIC_CREDENTIAL",
    ],
)
def test_credentials_redact_entire_lines_without_touching_public_repr(line: bytes) -> None:
    report = collect(b"before\n" + line + b"\nafter\n")
    assert report.redacted_lines == 1
    assert report.logs[0] == "before"
    assert report.logs[-1] == "after"
    assert "SYNTHETIC" not in json.dumps(report.payload())
    assert "SYNTHETIC" not in repr(report)
    assert report.logs[1].startswith("[REDACTED:")


def test_pem_block_redaction_survives_chunking_and_mismatched_end() -> None:
    data = (
        b"-----BEGIN PRIVATE KEY-----\nSYNTHETIC_BODY_1\n"
        b"-----END CERTIFICATE-----\nSYNTHETIC_BODY_2\n"
        b"-----END PRIVATE KEY-----\nnormal\n"
    )
    report = collect(data, 1)
    assert report.redacted_lines == 5
    assert report.logs[-1] == "normal"
    assert "SYNTHETIC" not in json.dumps(report.payload())


def test_pem_markers_on_same_line_are_processed_in_wire_order() -> None:
    data = (
        b"-----END PRIVATE KEY----- -----BEGIN PRIVATE KEY-----\n"
        b"SYNTHETIC_BODY\n-----END PRIVATE KEY-----\nnormal\n"
    )
    report = collect(data)
    assert report.redacted_lines == 3
    assert report.logs[-1] == "normal"
    assert "SYNTHETIC" not in json.dumps(report.payload())


def test_secret_like_identity_fields_are_not_exposed_in_metadata() -> None:
    data = startup().replace(
        b"Project name: cardputer_test", b"Project name: ghp_SYNTHETIC_CREDENTIAL"
    )
    report = collect(data)
    assert report.firmware.status == "partial"
    assert report.firmware.project_name is None
    assert "SYNTHETIC" not in json.dumps(report.payload())


@pytest.mark.parametrize(
    "line",
    [
        b"CAPTURE_BEGIN 240 135",
        b"CAPTURE_END",
        b"PIX 0 1234",
        b"PIX_EXTRA",
        b"I (1) capture: PIX 0 1234",
        b"\x1b[32mCAPTURE_OTHER value\x1b[0m",
        b"binary\x00SYNTHETIC_DATA",
        b"binary\x01SYNTHETIC_DATA",
        b"\xff\xfeSYNTHETIC_DATA",
        b"\x1b]unknown escape",
        b"PIX password=SYNTHETIC_CREDENTIAL",
        b"CAPTURE_OTHER token=SYNTHETIC_CREDENTIAL",
        b"\x00password=SYNTHETIC_CREDENTIAL",
        b"\xffpassword=SYNTHETIC_CREDENTIAL",
    ],
)
def test_capture_family_and_binary_lines_are_omitted_whole(line: bytes) -> None:
    report = collect(line + b"\n")
    assert report.logs == ()
    assert report.omitted_lines == 1
    assert report.metadata()["partial"] is True


def test_rendering_escapes_controls_unicode_and_literal_backslashes() -> None:
    report = collect("\tmessage café ⛄\rmid\\n\r\n".encode())
    assert report.logs == (r"\tmessage caf\xe9 \u26c4\rmid\\n",)
    assert report.rendered_bytes == len(report.logs[0]) + 1
    assert all(32 <= ord(character) <= 126 for character in report.logs[0])


def test_unfinished_line_is_never_returned_as_a_potential_credential_prefix() -> None:
    report = collect(b"normal\nSYNTHETIC_UNFINISHED")
    assert report.logs == ("normal",)
    assert report.pending_line_omitted is True
    assert report.omitted_lines == 1
    assert report.metadata()["partial"] is True
    assert "SYNTHETIC" not in json.dumps(report.payload())


def test_overlong_line_stops_collection_and_never_returns_partial_text() -> None:
    collector = DeviceLogCollector()
    assert collector.feed(b"x" * MAX_LINE_BYTES)
    assert len(collector._pending) == MAX_LINE_BYTES
    assert not collector.feed(b"x\nSYNTHETIC_AFTER_LIMIT\n")
    report = collector.finish()
    assert report.logs == ()
    assert report.bytes_received == MAX_LINE_BYTES + 1
    assert report.stop_reason == "line_limit"
    assert report.truncated is report.pending_line_omitted is True
    assert report.omitted_lines == 1
    assert len(collector._pending) == 0


def test_exact_line_limit_with_newline_is_accepted() -> None:
    report = collect(b"x" * MAX_LINE_BYTES + b"\n")
    assert report.logs == ("x" * MAX_LINE_BYTES,)
    assert report.truncated is False


def test_receive_limit_stops_exactly_without_an_additional_read() -> None:
    collector = DeviceLogCollector()
    # Omitted binary lines consume wire bytes without exhausting the text budget.
    data = (b"\x00" * 1023 + b"\n") * 64
    assert len(data) == MAX_RECEIVE_BYTES
    assert not collector.feed(data)
    assert not collector.feed(b"SYNTHETIC_AFTER_LIMIT\n")
    report = collector.finish()
    assert report.bytes_received == MAX_RECEIVE_BYTES
    assert report.complete_lines == report.omitted_lines == 64
    assert report.stop_reason == "receive_limit"
    assert report.logs == ()


def test_line_count_limit_stops_exactly() -> None:
    collector = DeviceLogCollector()
    assert not collector.feed(b"line\n" * MAX_RETAINED_LINES + b"SYNTHETIC_AFTER_LIMIT\n")
    report = collector.finish()
    assert len(report.logs) == MAX_RETAINED_LINES
    assert report.complete_lines == MAX_RETAINED_LINES
    assert report.stop_reason == "retained_line_limit"


def test_rendered_budget_counts_escape_expansion_and_newlines() -> None:
    collector = DeviceLogCollector()
    line = "⛄" * 300
    assert not collector.feed((line + "\n").encode() * 30)
    report = collector.finish()
    assert report.stop_reason == "rendered_text_limit"
    assert report.rendered_bytes <= MAX_RENDERED_BYTES
    assert report.rendered_bytes == sum(len(line) + 1 for line in report.logs)
    assert report.omitted_lines == 1
    assert all(line == r"\u26c4" * 300 for line in report.logs)


def test_finish_is_immutable_idempotent_and_clears_raw_buffers() -> None:
    collector = DeviceLogCollector()
    collector.feed(b"safe\nSYNTHETIC_PENDING")
    report = collector.finish()
    assert collector.finish() is report
    assert not collector.feed(b"later\n")
    assert not collector._pending
    assert not collector._logs
    assert collector._pem_label is None
    assert not collector._identity._fields
    with pytest.raises(FrozenInstanceError):
        report.truncated = True  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        report.firmware.status = "observed"  # type: ignore[misc]
    payload = report.payload()
    payload["logs"].append("modified")
    payload["firmware"]["status"] = "modified"
    assert report.logs == ("safe",)
    assert report.firmware.status == "unavailable"
    assert "safe" not in repr(report)


def test_clear_transient_seals_collector_so_pem_state_cannot_be_resumed() -> None:
    collector = DeviceLogCollector()
    collector.feed(b"-----BEGIN PRIVATE KEY-----\nSYNTHETIC_PENDING")
    collector.clear_transient()
    assert not collector._pending
    assert collector._pem_label is None
    assert not collector.feed(b"SYNTHETIC_BODY\n")
    assert "SYNTHETIC" not in json.dumps(collector.finish().payload())


def test_sha256_binds_canonical_redacted_payload_and_omits_itself() -> None:
    first = collect(b"token=SYNTHETIC_ONE\n")
    second = collect(b"token=SYNTHETIC_TWO\n")
    canonical = json.dumps(first.payload(), sort_keys=True, separators=(",", ":")).encode("ascii")
    assert first.sha256 == hashlib.sha256(canonical).hexdigest()
    assert first.sha256 == second.sha256
    assert "sha256" not in first.payload()
    assert first.sha256 != collect(b"different normal line\n").sha256


def test_invalid_input_raises_fixed_exception_without_stringifying() -> None:
    class Unrepresentable:
        def __repr__(self) -> str:
            raise AssertionError("must not stringify input")

    collector = DeviceLogCollector()
    with pytest.raises(TypeError, match="^device_log_bytes_required$"):
        collector.feed(Unrepresentable())  # type: ignore[arg-type]
