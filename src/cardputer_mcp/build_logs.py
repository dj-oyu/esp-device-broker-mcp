"""Bounded redacted RAM fan-out. Readers never consume each other's entries."""

import re
import time
from collections import deque
from typing import Any
from uuid import uuid4

_SECRETS = re.compile(
    r"(?i)(?:cpk_[a-f0-9]{32}\.[a-f0-9]{64}|bearer\s+\S+|"
    r"(?:password|token|secret|api[_-]?key)\s*[:=]\s*[^\s,;]+)"
)


class BuildLogs:
    def __init__(self, principal: str, *, max_bytes: int = 262144,
                 max_lines: int = 2048, retention: float = 300) -> None:
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

    def append(self, line: str, stream: str = "stdout") -> None:
        # Bound the original line too; never retain raw output or stream buffers.
        text = line[:4096]
        for secret in self.redactions:
            text = text.replace(secret, "[REDACTED]")
        text = _SECRETS.sub("[REDACTED]", text)
        level = "unknown"
        for candidate in ("error", "warning", "debug", "info"):
            if re.search(r"\b" + candidate + r"\b", text, re.I):
                level = candidate
                break
        progress = bool(re.match(r"\[\d+/\d+\]|(?:-- |Executing action|Build complete)", text))
        self.sequence += 1
        entry = dict(sequence=self.sequence, text=text, level=level,
                     stream=stream, progress=progress)
        size = len(text.encode("utf-8"))
        if size <= self.max_bytes:
            self.entries.append(entry)
            self.bytes += size
        while self.bytes > self.max_bytes or len(self.entries) > self.max_lines:
            self.bytes -= len(self.entries.popleft()["text"].encode("utf-8"))
        self.deadline = time.monotonic() + self.retention

    def read(self, principal: str, request: Any) -> dict[str, Any]:
        if principal != self.principal:
            raise ValueError("build_log_not_available")
        if time.monotonic() >= self.deadline:
            self.entries.clear()
            self.bytes = 0
            return dict(entries=[], expired=True, gap=True,
                        next_cursor=dict(epoch=self.epoch, sequence=self.sequence), has_more=False)
        cursor = request.cursor
        reset = cursor is not None and cursor.epoch != self.epoch
        after = 0 if cursor is None or reset else cursor.sequence
        if after > self.sequence:
            raise ValueError("cursor_ahead")
        oldest = self.entries[0]["sequence"] if self.entries else self.sequence + 1
        items = list(self.entries)
        selected: set[int] = set()
        for i, entry in enumerate(items):
            if entry["level"] in request.levels or entry["progress"]:
                selected.update(range(max(0, i-request.context), min(len(items), i+request.context+1)))
        result = [entry for i, entry in enumerate(items)
                  if i in selected and entry["sequence"] > after][:request.limit]
        sequence = result[-1]["sequence"] if len(result) == request.limit else self.sequence
        return dict(entries=result, expired=False, gap=reset or after < oldest-1,
                    oldest_sequence=oldest, next_cursor=dict(epoch=self.epoch, sequence=sequence),
                    has_more=sequence < self.sequence)
