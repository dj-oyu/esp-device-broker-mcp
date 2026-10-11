"""Content-free, bounded receive indicators; never a protocol acceptance decision.

The sole reader supplies byte chunks and complete lines separately. Nothing here
frames, retains, decodes, logs, or returns incoming content. Line indicators may
overlap; an unclassified line has none of those indicators. Counter saturation
also bounds command deltas, which are cumulative totals minus the command baseline.
"""

import re
from typing import Any

# Keep these expressions identical to serial_backend.ANSI and .PREFIX without
# importing the hardware backend (which itself consumes these diagnostics).
_ANSI = re.compile(rb"\x1b\[[0-?]*[ -/]*[@-~]")
_PREFIX = re.compile(rb"^[IWEVD] \([^\r\n)]*\) [A-Za-z0-9_.-]+: ")
_HOME_TOKEN = re.compile(rb"(?<![A-Za-z0-9_])HOME_READY(?![A-Za-z0-9_])")
_RESET_REASON = re.compile(rb"^rst:0x[0-9a-fA-F]+(?:[,( \t]|$)")
_BINARY_CONTROL = re.compile(rb"[^\x09\x20-\x7e]")

_COMMANDS = frozenset(
    {"none", "home_escape", "home_quit", "select_apps", "select_previous", "enter", "capture_frame"}
)
_COUNTERS = (
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
_SUMMARY_KEYS = frozenset(
    {"last_command", "command_write_completed", "before_command", "after_command", "pending_bytes"}
)
_MAX_BYTES = 1048576
_MAX_LINES = 65536
_MAX_PENDING_BYTES = 16384


def _count_limit(name: str) -> int:
    return _MAX_BYTES if name == "bytes" else _MAX_LINES


def _bounded_count(value: int, limit: int) -> int:
    return max(0, min(value, limit))


class ReceiveDiagnostics:
    """Maintain fixed counters only, independent of serial protocol state.

    ``observe_line`` expects a line without its LF separator, as supplied by the
    reader. Known ANSI sequences and trailing CRs are normalized like protocol
    lines. Other control bytes and non-ASCII bytes count as binary/control noise;
    tabs are allowed. These indicators make no claims about the text's meaning.
    """

    __slots__ = ("_counts", "_before_command", "_last_command", "_write_completed")

    def __init__(self) -> None:
        self._counts = dict.fromkeys(_COUNTERS, 0)
        self._before_command = self._counts.copy()
        self._last_command = "none"
        self._write_completed = False

    def _increment(self, name: str, amount: int = 1) -> None:
        self._counts[name] = _bounded_count(self._counts[name] + amount, _count_limit(name))

    def observe_bytes(self, data: bytes) -> None:
        """Count bytes without retaining a chunk or interpreting its contents."""
        self._increment("bytes", len(data))

    def observe_line(self, raw: bytes) -> None:
        """Classify one complete line, retaining only fixed bounded counters."""
        self._increment("complete_lines")
        without_ansi = _ANSI.sub(b"", raw).rstrip(b"\r")
        line = _PREFIX.sub(b"", without_ansi, count=1)
        indicators = {
            "esp_log_lines": _PREFIX.match(without_ansi) is not None,
            "ansi_lines": _ANSI.search(raw) is not None,
            "home_ready_token_lines": _HOME_TOKEN.search(line) is not None,
            "home_ready_exact_lines": line == b"HOME_READY",
            "boot_banner_lines": line.startswith(b"ESP-ROM:"),
            "reset_reason_lines": _RESET_REASON.match(line) is not None,
            # Record-family prefixes only: never parse or retain pixels/headers.
            "capture_record_lines": line.startswith((b"CAPTURE_", b"PIX")),
            "binary_control_lines": _BINARY_CONTROL.search(without_ansi) is not None,
        }
        for name, present in indicators.items():
            if present:
                self._increment(name)
        if not any(indicators.values()):
            self._increment("unclassified_lines")

    def begin_command(self, name: str) -> None:
        """Snapshot the receive baseline immediately before a command write."""
        self._last_command = name if type(name) is str and name in _COMMANDS else "none"
        self._before_command = self._counts.copy()
        self._write_completed = False

    def write_completed(self) -> None:
        """Mark only a successfully completed command write."""
        self._write_completed = True

    def snapshot(self, pending_bytes: int) -> dict[str, Any]:
        """Return detached fixed-schema data, without sharing internal state."""
        pending = pending_bytes if type(pending_bytes) is int else 0
        return {
            "last_command": self._last_command,
            "command_write_completed": self._write_completed,
            "before_command": self._before_command.copy(),
            "after_command": {
                name: self._counts[name] - self._before_command[name] for name in _COUNTERS
            },
            "pending_bytes": _bounded_count(pending, _MAX_PENDING_BYTES),
        }


def _sanitize_counters(value: object) -> dict[str, int] | None:
    if type(value) is not dict or any(type(key) is not str for key in value):
        return None
    if value.keys() != set(_COUNTERS):
        return None
    result: dict[str, int] = {}
    for name in _COUNTERS:
        count = value[name]
        if type(count) is not int:
            return None
        result[name] = _bounded_count(count, _count_limit(name))
    return result


def sanitize_receive_summary(value: object) -> dict[str, Any] | None:
    """Accept only the exact fixed schema, copying and bounding every counter.

    Missing/extra keys, unknown commands, bool counts, and custom scalar/container
    subclasses invalidate the whole summary. No arbitrary object's conversion,
    string representation, or data is ever copied into diagnostics.
    """
    if type(value) is not dict or any(type(key) is not str for key in value):
        return None
    if value.keys() != _SUMMARY_KEYS:
        return None
    command = value["last_command"]
    completed = value["command_write_completed"]
    pending = value["pending_bytes"]
    if type(command) is not str or command not in _COMMANDS:
        return None
    if type(completed) is not bool or type(pending) is not int:
        return None
    before = _sanitize_counters(value["before_command"])
    after = _sanitize_counters(value["after_command"])
    if before is None or after is None:
        return None
    return {
        "last_command": command,
        "command_write_completed": completed,
        "before_command": before,
        "after_command": after,
        "pending_bytes": _bounded_count(pending, _MAX_PENDING_BYTES),
    }
