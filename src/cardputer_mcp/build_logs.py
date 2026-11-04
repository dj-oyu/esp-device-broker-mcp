"""Bounded redacted RAM fan-out. Readers never consume each other's entries."""

import re
import threading
import time
from collections import deque
from collections.abc import Callable
from typing import Any
from uuid import uuid4

from .console_logs import console_line

_SECRETS = re.compile(
    r"(?i)(?:cpk_[a-f0-9]{32}\.[a-f0-9]{64}|bearer\s+\S+|"
    r"(?:password|token|secret|api[_-]?key)\s*[:=]\s*[^\s,;]+)"
)


def redact_log_text(line: str, redactions: tuple[str, ...] = ()) -> str:
    """Redact bounded input, including credential prefixes cut by transport limits."""
    text = line[:4096]
    for secret in redactions:
        text = text.replace(secret, "[REDACTED]")
    text = _SECRETS.sub("[REDACTED]", text)
    return re.sub(r"(?i)cpk_[a-f0-9.]*", "[REDACTED]", text)[:4096]


def select_page(
    items: list[dict[str, Any]], request: Any, after: int, last: int
) -> tuple[list[dict[str, Any]], int, int]:
    """Choose one page of a build log; return (entries, next sequence, remaining).

    Without ``from_line`` this is the filtered forward read: entries at the
    requested levels (and progress lines) plus context, after the cursor.
    With ``from_line`` it is a raw read of lines ``from_line..to_line`` (the
    line number is the entry sequence); nothing is filtered. ``remaining``
    counts the lines of that selection beyond this page.
    """
    from_line = getattr(request, "from_line", None)
    if from_line is not None:
        to_line = request.to_line if request.to_line is not None else last
        chosen = [item for item in items if from_line <= item["sequence"] <= to_line]
        page = chosen[: request.limit]
        sequence = page[-1]["sequence"] if page else min(from_line - 1, last)
        return page, sequence, len(chosen) - len(page)
    selected: set[int] = set()
    for index, item in enumerate(items):
        if item["level"] in request.levels or item["progress"]:
            selected.update(
                range(max(0, index - request.context), min(len(items), index + request.context + 1))
            )
    chosen = [
        item for index, item in enumerate(items) if index in selected and item["sequence"] > after
    ]
    page = chosen[: request.limit]
    sequence = page[-1]["sequence"] if len(page) == request.limit else last
    return page, sequence, len(chosen) - len(page)


class BuildLogs:
    def __init__(
        self,
        principal: str,
        *,
        max_bytes: int = 262144,
        max_lines: int = 2048,
        retention: float = 300,
        job_id: str | None = None,
        on_change: Callable[[], None] | None = None,
    ) -> None:
        self._lock = threading.RLock()
        self.on_change = on_change
        self.principal = principal
        self.epoch = uuid4().hex
        self.sequence = 0
        self.entries: deque[dict[str, Any]] = deque()
        self.bytes = 0
        self.max_bytes = max_bytes
        self.max_lines = max_lines
        self.deadline = time.monotonic() + retention
        self.retention = retention
        self.redactions: tuple[str, ...] = ()
        self.job_id = job_id
        self._console_progress_at = float("-inf")
        # Optional durable tee (job_logs.JobLog); RAM behaviour is unchanged.
        self.durable: Any = None
        self.durable_stream = "build"

    def append(self, line: str, stream: str = "stdout", *, console_output: bool = True) -> None:
        with self._lock:
            self._append(line, stream, console_output)

    def _append(self, line: str, stream: str, console_output: bool) -> None:
        # Bound the original line too; never retain raw output or stream buffers.
        text = redact_log_text(line, self.redactions)
        level = "unknown"
        for candidate in ("error", "warning", "debug", "info"):
            if re.search(r"\b" + candidate + r"\b", text, re.I):
                level = candidate
                break
        progress = bool(
            re.match(
                r"\[\d+/\d+\]|(?:-- |Executing action|Build complete|info: ROM read \d+/\d+ bytes)",
                text,
            )
        )
        self.sequence += 1
        entry = dict(
            sequence=self.sequence, text=text, level=level, stream=stream, progress=progress
        )
        if self.durable is not None:
            try:
                self.durable.append(self.durable_stream, text, level)
            except Exception:
                pass
        size = len(text.encode("utf-8"))
        if size <= self.max_bytes:
            self.entries.append(entry)
            self.bytes += size
        while self.bytes > self.max_bytes or len(self.entries) > self.max_lines:
            self.bytes -= len(self.entries.popleft()["text"].encode("utf-8"))
        now = time.monotonic()
        self.deadline = now + self.retention
        if (
            console_output
            and self.job_id is not None
            and (
                level in ("warning", "error") or (progress and now - self._console_progress_at >= 2)
            )
        ):
            if progress:
                self._console_progress_at = now
            try:
                console_line(self.job_id, text)
            except Exception:
                pass

        if self.on_change is not None:
            try:
                self.on_change()
            except Exception:
                pass

    def expire(self) -> None:
        """Clear expired private text even when no reader asks for it."""
        with self._lock:
            if time.monotonic() >= self.deadline:
                self.entries.clear()
                self.bytes = 0

    def read(self, principal: str, request: Any) -> dict[str, Any]:
        with self._lock:
            return self._read(principal, request)

    def _read(self, principal: str, request: Any) -> dict[str, Any]:
        if principal != self.principal:
            raise ValueError("build_log_not_available")
        if time.monotonic() >= self.deadline:
            self.entries.clear()
            self.bytes = 0
            return dict(
                entries=[],
                expired=True,
                gap=True,
                next_cursor=dict(epoch=self.epoch, sequence=self.sequence),
                has_more=False,
            )
        cursor = request.cursor
        reset = cursor is not None and cursor.epoch != self.epoch
        after = 0 if cursor is None or reset else cursor.sequence
        ranged = getattr(request, "from_line", None) is not None
        if after > self.sequence and not ranged:
            raise ValueError("cursor_ahead")
        oldest = self.entries[0]["sequence"] if self.entries else self.sequence + 1
        result, sequence, remaining = select_page(list(self.entries), request, after, self.sequence)
        return dict(
            entries=result,
            expired=False,
            gap=(request.from_line < oldest) if ranged else (reset or after < oldest - 1),
            oldest_sequence=oldest,
            last_sequence=self.sequence,
            remaining=remaining,
            next_cursor=dict(epoch=self.epoch, sequence=sequence),
            has_more=remaining > 0 if ranged else sequence < self.sequence,
        )
