"""One short-lived, local-only HOME line preview, independent of serial control.

Only an explicitly opted-in caller may attach this probe to its sole reader.
The caller opens a window after the fixed Esc write, closes it after the HOME
wait, finishes it after the job, and calls ``prune`` while the preview exists.
No method performs I/O or changes protocol acceptance. Metadata never includes
wire content; ``preview`` is exclusively for the local GUI.
"""

import math
import re
import threading
from collections.abc import Callable, Iterator
from itertools import islice
from typing import Any, cast

# Match serial_backend's normalization without importing the hardware backend.
_ANSI = re.compile(rb"\x1b\[[0-?]*[ -/]*[@-~]")
_PREFIX_PATTERN = rb"[IWEVD] \([^\r\n)]*\) [A-Za-z0-9_.-]+: "
_PREFIX = re.compile(rb"^" + _PREFIX_PATTERN)
_PREFIX_AT = re.compile(_PREFIX_PATTERN)
_HOME_TOKEN = re.compile(rb"(?<![A-Za-z0-9_])HOME_READY(?![A-Za-z0-9_])")
_HOME = b"HOME_READY"
_LIMIT_BYTES = 1024
_COUNTER_LIMIT = 65536
_MAX_SECONDS = 120


def _word(byte: int | None) -> bool:
    return byte is not None and (
        65 <= byte <= 90 or 97 <= byte <= 122 or 48 <= byte <= 57 or byte == 95
    )


def _without_ansi(raw: bytes, end: int) -> Iterator[int]:
    """Walk normalized bytes without copying an oversized source line."""
    view = memoryview(raw)
    start = 0
    for match in _ANSI.finditer(raw, 0, end):
        yield from view[start : match.start()]
        start = match.end()
    yield from view[start:end]


def _contains_home(raw: bytes, end: int) -> bool:
    """Find the same ASCII token boundary using constant auxiliary storage."""
    previous: int | None = None
    matched = 0
    for byte in _without_ansi(raw, end):
        if matched == len(_HOME):
            if not _word(byte):
                return True
            matched = 0
        if matched and byte == _HOME[matched]:
            matched += 1
        else:
            matched = int(byte == _HOME[0] and not _word(previous))
        previous = byte
    return matched == len(_HOME)


def _capture_family(line: bytes) -> bool:
    """Also reject padded/repeated-prefix records, never inspecting pixels."""
    at = 0
    while True:
        while at < len(line) and line[at] in (32, 9):
            at += 1
        match = _PREFIX_AT.match(line, at)
        if match is None:
            return line.startswith((b"CAPTURE_", b"PIX"), at)
        at = match.end()


def _shape(line: bytes) -> dict[str, int | bool] | None:
    """Describe a complete, bounded normalized line using fixed counters only."""
    at = 0
    prefix_count = 0
    while prefix_count < 5:
        match = _PREFIX_AT.match(line, at)
        if match is None:
            break
        prefix_count += 1
        at = match.end()
    body = _PREFIX.sub(b"", line, count=1)
    tokens = list(islice(_HOME_TOKEN.finditer(body), 9))
    if not tokens:
        return None
    first = tokens[0]
    before = body[: first.start()]
    after = body[first.end() :]
    return {
        "standard_prefix_count": min(prefix_count, 4),
        "prefix_count_capped": prefix_count > 4,
        "body_bytes": len(body),
        "home_token_count": min(len(tokens), 8),
        "token_count_capped": len(tokens) > 8,
        "token_offset": first.start(),
        "before_bytes": len(before),
        "after_bytes": len(after),
        "before_spaces": before.count(b" "),
        "before_tabs": before.count(b"\t"),
        "after_spaces": after.count(b" "),
        "after_tabs": after.count(b"\t"),
        "before_horizontal_only": not before.strip(b" \t"),
        "after_horizontal_only": not after.strip(b" \t"),
    }


def _escaped(line: bytearray) -> str:
    pieces = []
    for byte in line:
        if byte == 32:
            pieces.append(r"\x20")
        elif byte == 9:
            pieces.append(r"\t")
        elif byte == 13:
            pieces.append(r"\r")
        elif byte == 92:
            pieces.append(r"\\")
        elif 33 <= byte <= 126:
            pieces.append(chr(byte))
        else:
            pieces.append(f"\\x{byte:02x}")
    return "".join(pieces)


class HomeLineProbe:
    """Select at most one complete HOME token line for a bounded local preview.

    ``observe_line`` expects LF-free framed bytes, with CR retained. A single
    terminal LF is also accepted and excluded from the wire-byte count. Other
    embedded LFs and non-bytes inputs are ignored; this is not a line framer.
    No selected outcome or escaped content is published until ``finish`` closes
    the job. Expiry is enforced by every public operation, including preview
    retrieval. A caller must discard any returned preview when its TTL expires.
    """

    __slots__ = (
        "_clock",
        "_deadline",
        "_last_clock",
        "_lock",
        "_state",
        "_window",
        "_finished",
        "_line",
        "_wire_bytes",
        "_shape",
    )

    def __init__(self, clock: Callable[[], float], deadline: float) -> None:
        self._clock = clock
        self._deadline = 0.0
        self._last_clock = 0.0
        self._lock = threading.Lock()
        self._state = "capturing"
        self._window = False
        self._finished = False
        self._line = bytearray()
        self._wire_bytes = 0
        self._shape: dict[str, int | bool] | None = None
        now = self._read_clock()
        limit = self._finite_number(deadline)
        if now is None or limit is None or limit <= now:
            self._expire_locked()
        else:
            self._last_clock = now
            self._deadline = min(limit, now + _MAX_SECONDS)
            if self._deadline <= now:
                self._expire_locked()

    def __repr__(self) -> str:
        # Neither the private line nor caller-supplied clock may appear in repr.
        return "HomeLineProbe()"

    @staticmethod
    def _finite_number(value: object) -> float | None:
        if type(value) not in (int, float):
            return None
        try:
            number = float(cast(int | float, value))
        except (OverflowError, ValueError):
            return None
        return number if math.isfinite(number) else None

    def _read_clock(self) -> float | None:
        try:
            return self._finite_number(self._clock())
        except BaseException:
            # A caller-supplied source must not leak arbitrary exception text or
            # leave private data alive when its deadline cannot be established.
            return None

    def _erase_locked(self) -> None:
        self._line[:] = b"\0" * len(self._line)
        self._line.clear()

    def _expire_locked(self) -> None:
        self._erase_locked()
        self._state = "expired"
        self._window = False
        self._finished = True

    def _prune_locked(self) -> None:
        if self._state in ("expired", "cleared"):
            return
        now = self._read_clock()
        if now is None or now < self._last_clock or now >= self._deadline:
            self._expire_locked()
        else:
            self._last_clock = now

    def _metadata_locked(self) -> dict[str, Any]:
        remaining = 0.0
        if self._state not in ("expired", "cleared"):
            remaining = max(0.0, min(float(_MAX_SECONDS), self._deadline - self._last_clock))
        return {
            "state": "capturing" if not self._finished else self._state,
            "remaining_seconds": remaining,
            "wire_bytes": self._wire_bytes,
            "limit_bytes": _LIMIT_BYTES,
            "shape": None if self._shape is None else self._shape.copy(),
        }

    def begin_window(self) -> None:
        with self._lock:
            self._prune_locked()
            if self._state == "capturing" and not self._finished:
                self._window = True

    def observe_line(self, raw: bytes) -> None:
        with self._lock:
            self._prune_locked()
            if not self._window or self._finished or self._state != "capturing":
                return
            if type(raw) is not bytes:
                return
            end = len(raw) - int(raw.endswith(b"\n"))
            if raw.find(b"\n", 0, end) >= 0 or not _contains_home(raw, end):
                return
            # Selection is consumed even when the first candidate is too long
            # or forbidden. Never fall through to a later, more convenient line.
            self._window = False
            self._wire_bytes = min(end, _COUNTER_LIMIT)
            bounded = raw[: min(end, _COUNTER_LIMIT)]
            normalized = _ANSI.sub(b"", bounded).rstrip(b"\r")
            self._shape = _shape(normalized) if end <= _COUNTER_LIMIT else None
            if _capture_family(normalized):
                self._state = "line_rejected"
            elif end > _LIMIT_BYTES:
                self._state = "line_too_long"
            else:
                self._line.extend(raw[:end])
                self._state = "available"
            # Also fail closed if processing crossed the deadline or the clock
            # became unsafe, before making the candidate available to the GUI.
            self._prune_locked()

    def end_window(self) -> None:
        with self._lock:
            self._prune_locked()
            self._window = False

    def finish(self) -> None:
        with self._lock:
            self._prune_locked()
            self._window = False
            self._finished = True
            if self._state == "capturing":
                self._state = "no_matching_line"

    def prune(self) -> None:
        with self._lock:
            self._prune_locked()

    def clear(self) -> None:
        with self._lock:
            self._erase_locked()
            self._window = False
            self._finished = True
            self._state = "cleared"

    def metadata(self) -> dict[str, Any]:
        with self._lock:
            self._prune_locked()
            return self._metadata_locked()

    def preview(self) -> dict[str, Any]:
        with self._lock:
            self._prune_locked()
            escaped = (
                _escaped(self._line) if self._state == "available" and self._finished else None
            )
            self._prune_locked()
            snapshot = self._metadata_locked()
            if escaped is not None and self._state == "available":
                snapshot["escaped_line"] = escaped
            return snapshot
