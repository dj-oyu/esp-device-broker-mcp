"""Bounded, RAM-only observations of untrusted serial console output.

This collector has no serial, filesystem, logging, or broker-event dependencies.
Its output requires the caller's separate authorization before delivery. Startup
text is a device-reported claim, never proof of the currently running binary.
"""

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

MAX_RECEIVE_BYTES = 64 * 1024
MAX_LINE_BYTES = 1024
MAX_RETAINED_LINES = 256
MAX_RENDERED_BYTES = 32 * 1024

REDACTION_LIMITATIONS = (
    "Best-effort redaction of recognizable credential patterns and PEM blocks; "
    "unknown, encoded, obfuscated, or unlabeled secrets and other private data may remain."
)
FIRMWARE_LIMITATIONS = (
    "Observed startup text is unverified device-reported information. The ELF SHA256 "
    "prefix is neither a full ELF digest nor a .bin digest, and does not establish "
    "the identity of the currently running binary."
)

_ANSI = re.compile(rb"\x1b\[[0-?]*[ -/]*[@-~]")
_LOG_PREFIX = re.compile(rb"^[IWEVD] \([0-9]{1,10}\) [A-Za-z0-9_.-]+: ")
_APP_PREFIX = re.compile(rb"^I \([0-9]{1,10}\) app_init: ")
_BINARY = re.compile(rb"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_CREDENTIAL = re.compile(
    rb"(?:\b(?:authorization|proxy-authorization)\b"
    rb"|\bbearer\s+"
    rb"|\b(?:[a-z][a-z0-9]*[_-])*(?:password|passwd|pwd|passphrase|secret|credential|token|"
    rb"(?:access|refresh|auth|id|session)[_ -]?token|"
    rb"(?:api|private|secret|access)[_ -]?key)\b[\"']?\s*(?:[:=]|\s+is\s+)"
    rb"|\b(?:sk-(?:proj-|ant-)?|rk-(?:live|test)-|(?:sk|rk)_(?:live|test)_|"
    rb"gh[pousr]_|github_pat_|"
    rb"glpat-|xox[baprs]-|AKIA|ASIA|AIza|SG\.|eyJ)[A-Za-z0-9_./+-]{6,}"
    rb"|[a-z][a-z0-9+.-]*://[^\s/:]+:[^\s/@]+@)",
    re.IGNORECASE,
)
_PEM_MARKER = re.compile(rb"-----(BEGIN|END) ([A-Z0-9][A-Z0-9 -]{0,63}?)-----")
_REDACTED = "[REDACTED: credential-like content]"

# ESP-IDF 6 startup grammar, including 32-byte NUL-terminated description fields.
# app_init's 17-byte buffer prints min(CONFIG_APP_RETRIEVE_LEN_ELF_SHA, 16)
# lowercase hex characters plus "...". The config range is 8..64 (default 9),
# so only 8..16-character prefixes are supported, never a full digest.
# https://github.com/espressif/esp-idf/blob/v6.0.1/components/esp_app_format/esp_app_desc.c
# https://github.com/espressif/esp-idf/blob/v6.0.1/components/esp_app_format/Kconfig.projbuild
_FIELD_NAMES = {
    b"Project name": "project_name",
    b"App version": "app_version",
    b"Secure version": "secure_version",
    b"Compile time": "compile_time",
    b"ELF file SHA256": "elf_sha256_prefix",
    b"ESP-IDF": "idf_version",
}
_FIELD_ORDER = {name: index for index, name in enumerate(_FIELD_NAMES.values())}
_REQUIRED_FIELDS = frozenset({"project_name", "app_version", "elf_sha256_prefix", "idf_version"})
_PROJECT = re.compile(rb"[A-Za-z0-9_][A-Za-z0-9_.-]{0,30}")
_VERSION = re.compile(rb"[A-Za-z0-9][A-Za-z0-9_./+-]{0,30}")
_IDF_VERSION = re.compile(rb"v?[0-9]+\.[0-9]+(?:\.[0-9]+)?(?:[-+][A-Za-z0-9_.+-]+)?")
_ELF_PREFIX = re.compile(rb"([0-9a-f]{8,16})\.\.\.")
_COMPILE_TIME = re.compile(
    rb"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) "
    rb"(?: [1-9]|[12][0-9]|3[01]) [0-9]{4} [0-2][0-9]:[0-5][0-9]:[0-5][0-9]"
)
_MONTHS = (
    b"Jan",
    b"Feb",
    b"Mar",
    b"Apr",
    b"May",
    b"Jun",
    b"Jul",
    b"Aug",
    b"Sep",
    b"Oct",
    b"Nov",
    b"Dec",
)


@dataclass(frozen=True, slots=True)
class FirmwareObservation:
    """Only allowlisted startup fields; never retain arbitrary firmware text."""

    status: str = "unavailable"
    reason: str = "no_startup_block"
    project_name: str | None = field(default=None, repr=False)
    app_version: str | None = field(default=None, repr=False)
    compile_time: str | None = field(default=None, repr=False)
    elf_sha256_prefix: str | None = field(default=None, repr=False)
    idf_version: str | None = field(default=None, repr=False)
    secure_version: int | None = field(default=None, repr=False)

    def metadata(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "reason": self.reason,
            "project_name": self.project_name,
            "app_version": self.app_version,
            "compile_time": self.compile_time,
            "elf_sha256_prefix": self.elf_sha256_prefix,
            "idf_version": self.idf_version,
            "secure_version": self.secure_version,
            "source_verified": False,
            "binary_sha256_verified": False,
            "limitations": FIRMWARE_LIMITATIONS,
        }


@dataclass(frozen=True, slots=True)
class DeviceObservation:
    """Immutable redacted result. Its repr and metadata omit the console logs.

    ``sha256`` hashes ``payload()`` encoded as ASCII JSON with sorted keys and
    separators (",", ":"). It binds this redacted observation, not a firmware
    image. The digest itself is deliberately outside the hashed payload.

    ``bytes_received`` counts bytes inspected by the collector, up to its fixed
    limits. It does not count an unprocessed suffix of a serial read chunk after
    a collection limit is reached, or bytes discarded by the external reader.
    """

    logs: tuple[str, ...] = field(repr=False)
    bytes_received: int
    complete_lines: int
    redacted_lines: int
    omitted_lines: int
    rendered_bytes: int
    truncated: bool
    pending_line_omitted: bool
    stop_reason: str
    firmware: FirmwareObservation
    reset_sequence_issued: bool = False
    capture_gap: bool = False
    sha256: str = field(init=False)

    def __post_init__(self) -> None:
        canonical = json.dumps(self.payload(), sort_keys=True, separators=(",", ":"))
        object.__setattr__(self, "sha256", hashlib.sha256(canonical.encode("ascii")).hexdigest())

    def metadata(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "bytes_received": self.bytes_received,
            "complete_lines": self.complete_lines,
            "retained_lines": len(self.logs),
            "redacted_lines": self.redacted_lines,
            "omitted_lines": self.omitted_lines,
            "rendered_bytes": self.rendered_bytes,
            "truncated": self.truncated,
            "pending_line_omitted": self.pending_line_omitted,
            "partial": self.truncated
            or self.pending_line_omitted
            or self.omitted_lines > 0
            or self.capture_gap,
            "reset_sequence_issued": self.reset_sequence_issued,
            "capture_gap": self.capture_gap,
            "stop_reason": self.stop_reason,
            "redaction_limitations": REDACTION_LIMITATIONS,
            "firmware": self.firmware.metadata(),
        }

    def payload(self) -> dict[str, Any]:
        """Return detached delivery data for an independently authorized caller."""
        return {**self.metadata(), "logs": list(self.logs)}


class _StartupIdentity:
    __slots__ = ("_blocks", "_fields", "_invalid", "_ambiguous", "_order")

    def __init__(self) -> None:
        self._blocks = 0
        self._fields: dict[str, str | int] = {}
        self._invalid = False
        self._ambiguous: str | None = None
        self._order = -1

    def observe(self, line: bytes) -> None:
        prefix = _APP_PREFIX.match(line)
        if prefix is None:
            return
        body = line[prefix.end() :]
        if body == b"Application information:":
            self._blocks += 1
            if self._blocks > 1:
                self._ambiguous = "multiple_startup_blocks"
                self._fields.clear()
            return
        if self._blocks != 1 or self._ambiguous is not None:
            return
        label, separator, raw_value = body.partition(b":")
        name = _FIELD_NAMES.get(label)
        if name is None or not separator:
            return
        # Only ASCII spaces used by the upstream format, not arbitrary stripping
        # or substring searches that could reinterpret noisy output as identity.
        spaces = len(raw_value) - len(raw_value.lstrip(b" "))
        if not 1 <= spaces <= 16:
            self._invalid = True
            return
        value = self._parse_value(name, raw_value[spaces:])
        if value is None:
            self._invalid = True
            return
        if name in self._fields:
            self._ambiguous = (
                "contradictory_startup_fields"
                if self._fields[name] != value
                else "duplicate_startup_fields"
            )
            self._fields.clear()
            return
        if _FIELD_ORDER[name] <= self._order:
            self._invalid = True
            return
        self._order = _FIELD_ORDER[name]
        self._fields[name] = value

    @staticmethod
    def _parse_value(name: str, value: bytes) -> str | int | None:
        if name == "project_name":
            valid = _PROJECT.fullmatch(value) is not None
        elif name == "app_version":
            plausible = any(48 <= item <= 57 for item in value) or bool(
                re.fullmatch(rb"[0-9a-f]{7,31}(?:-dirty)?", value)
            )
            valid = _VERSION.fullmatch(value) is not None and plausible
        elif name == "idf_version":
            valid = len(value) <= 31 and _IDF_VERSION.fullmatch(value) is not None
        elif name == "elf_sha256_prefix":
            match = _ELF_PREFIX.fullmatch(value)
            return None if match is None else match.group(1).decode("ascii")
        elif name == "secure_version":
            if not re.fullmatch(rb"[0-9]{1,10}", value):
                return None
            number = int(value)
            return number if number <= 0xFFFFFFFF else None
        elif name == "compile_time":
            if not _COMPILE_TIME.fullmatch(value):
                return None
            try:
                # Avoid process locale changing the English __DATE__ grammar.
                datetime(
                    int(value[7:11]),
                    _MONTHS.index(value[:3]) + 1,
                    int(value[4:6]),
                    int(value[12:14]),
                    int(value[15:17]),
                    int(value[18:20]),
                )
            except ValueError:
                return None
            valid = True
        else:
            return None
        return value.decode("ascii") if valid else None

    def finish(self) -> FirmwareObservation:
        if self._ambiguous is not None:
            result = FirmwareObservation(status="ambiguous", reason=self._ambiguous)
        elif self._blocks == 0:
            result = FirmwareObservation()
        else:
            complete = not self._invalid and _REQUIRED_FIELDS.issubset(self._fields)
            values: dict[str, Any] = dict(self._fields)
            result = FirmwareObservation(
                status="observed" if complete else "partial",
                reason=(
                    "complete_startup_block"
                    if complete
                    else "invalid_startup_field"
                    if self._invalid
                    else "incomplete_startup_block"
                ),
                **values,
            )
        self._fields.clear()
        return result


class DeviceLogCollector:
    """Incrementally collect bytes without any hardware operations or persistence.

    Limits are fixed: 64 KiB input, 1024 wire bytes per LF-delimited line, 256
    retained lines, and 32 KiB rendered text including one LF per retained line.
    Reaching a limit stops collection and ``feed`` returns False thereafter.
    Oversized and unfinished lines are omitted whole, never partially revealed.
    Known ANSI CSI sequences and a single terminal CR are removed. Other binary
    control lines and non-UTF-8 input are omitted; retained tabs, embedded CRs,
    backslashes, and non-ASCII characters use ASCII backslash escapes.
    """

    __slots__ = (
        "_pending",
        "_logs",
        "_bytes",
        "_complete",
        "_redacted",
        "_omitted",
        "_rendered",
        "_truncated",
        "_pending_omitted",
        "_stop_reason",
        "_pem_label",
        "_identity",
        "_result",
        "_after_gap",
        "_capture_gap",
    )

    def __init__(self) -> None:
        self._pending = bytearray()
        self._logs: list[str] = []
        self._bytes = 0
        self._complete = 0
        self._redacted = 0
        self._omitted = 0
        self._rendered = 0
        self._truncated = False
        self._pending_omitted = False
        self._stop_reason: str | None = None
        self._pem_label: bytes | None = None
        self._identity = _StartupIdentity()
        self._result: DeviceObservation | None = None
        self._after_gap = False
        self._capture_gap = False

    def mark_gap(self) -> None:
        """Discard incomplete framing before a port handoff; never join two handles.

        The first later line may be a suffix whose credential label was lost.
        Only the exact, data-free startup-block header can establish its boundary.
        Existing PEM redaction survives, including markers in the omitted prefix.
        """
        if self._result is not None or self._stop_reason is not None:
            return
        pending = _ANSI.sub(b"", bytes(self._pending))
        for marker in _PEM_MARKER.finditer(pending):
            if marker.group(1) == b"BEGIN" and self._pem_label is None:
                self._pem_label = marker.group(2)
            elif marker.group(1) == b"END" and marker.group(2) == self._pem_label:
                self._pem_label = None
        self._omit_pending()
        self._after_gap = True
        self._capture_gap = True
        self._identity = _StartupIdentity()

    def feed(self, data: bytes) -> bool:
        """Inspect a bounded prefix of a chunk and return whether collection may continue."""
        if self._stop_reason is not None or self._result is not None:
            return False
        if type(data) is not bytes:
            raise TypeError("device_log_bytes_required")
        for byte in data:
            self._bytes += 1
            if byte == 10:
                self._complete += 1
                self._observe_line(bytes(self._pending))
                self._pending.clear()
            elif len(self._pending) == MAX_LINE_BYTES:
                self._omit_pending()
                self._stop("line_limit")
            else:
                self._pending.append(byte)
            if self._bytes == MAX_RECEIVE_BYTES:
                self._stop("receive_limit")
            if self._stop_reason is not None:
                return False
        return True

    def _stop(self, reason: str) -> None:
        if self._stop_reason is None:
            self._stop_reason = reason
        self._truncated = True

    def _omit_pending(self) -> None:
        if self._pending:
            self._pending_omitted = True
            self._omitted += 1
            self._pending.clear()

    def _observe_line(self, raw: bytes) -> None:
        line = _ANSI.sub(b"", raw)
        if line.endswith(b"\r"):
            line = line[:-1]
        if self._after_gap:
            self._after_gap = False
            prefix = _APP_PREFIX.match(line)
            if prefix is None or line[prefix.end() :] != b"Application information:":
                # Apply a visible PEM opener/closer conservatively, even though
                # no fragment text or firmware fields will be returned.
                for marker in _PEM_MARKER.finditer(line):
                    if marker.group(1) == b"BEGIN" and self._pem_label is None:
                        self._pem_label = marker.group(2)
                    elif marker.group(1) == b"END" and marker.group(2) == self._pem_label:
                        self._pem_label = None
                self._omitted += 1
                return
        sensitive = self._pem_label is not None
        # Process in wire order: an END before a BEGIN must not clear the newly
        # opened block, nor may an unrelated END reveal its subsequent body.
        for marker in _PEM_MARKER.finditer(line):
            sensitive = True
            if marker.group(1) == b"BEGIN" and self._pem_label is None:
                self._pem_label = marker.group(2)
            elif marker.group(1) == b"END" and marker.group(2) == self._pem_label:
                self._pem_label = None
        body = _LOG_PREFIX.sub(b"", line, count=1).lstrip(b" \t")
        if body.startswith((b"PIX", b"CAPTURE")) or _BINARY.search(line):
            self._omitted += 1
            return
        try:
            text = line.decode("utf-8")
        except UnicodeDecodeError:
            self._omitted += 1
            return
        sensitive = sensitive or _CREDENTIAL.search(line) is not None
        if sensitive:
            self._redacted += 1
            rendered = _REDACTED
        else:
            rendered = text.encode("unicode_escape").decode("ascii")
            self._identity.observe(line)
        length = len(rendered) + 1
        if self._rendered + length > MAX_RENDERED_BYTES:
            self._omitted += 1
            self._stop("rendered_text_limit")
            return
        self._logs.append(rendered)
        self._rendered += length
        if len(self._logs) == MAX_RETAINED_LINES:
            self._stop("retained_line_limit")
        elif self._rendered == MAX_RENDERED_BYTES:
            self._stop("rendered_text_limit")

    def finish(self) -> DeviceObservation:
        if self._result is None:
            self._omit_pending()
            self._result = DeviceObservation(
                logs=tuple(self._logs),
                bytes_received=self._bytes,
                complete_lines=self._complete,
                redacted_lines=self._redacted,
                omitted_lines=self._omitted,
                rendered_bytes=self._rendered,
                truncated=self._truncated,
                pending_line_omitted=self._pending_omitted,
                stop_reason=self._stop_reason or "finished",
                firmware=self._identity.finish(),
                capture_gap=self._capture_gap,
            )
            self._logs.clear()
            self._pem_label = None
        return self._result

    def clear_transient(self) -> None:
        """Finalize and release transient raw buffers before a potentially blocking close.

        This seals the collector: resuming after erasing PEM state could otherwise
        disclose the body of a block whose opening marker has been discarded.
        """
        self.finish()
