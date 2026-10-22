"""Private bounded fan-out: console flood cannot evict structured control ACKs."""

import threading
import time
from collections import deque
from typing import Any
from uuid import uuid4

from .app_models import AppEventsRequest
from .build_logs import BuildLogs, redact_log_text
from .models import BuildLogRequest, LogCursor


class AppEvents:
    def __init__(self, principal: str, log_bytes: int) -> None:
        self.principal = principal
        self.epoch = uuid4().hex
        self.logs = BuildLogs(principal, max_bytes=log_bytes, max_lines=256)
        self.control: deque[dict[str, Any]] = deque(maxlen=128)
        self.sequence = 0
        self.deadline = time.monotonic() + 300
        self.lock = threading.RLock()

    def emit(self, kind: str, **fields: Any) -> None:
        with self.lock:
            self.sequence += 1
            self.control.append(dict(sequence=self.sequence, kind=kind, **fields))
            self.deadline = time.monotonic() + 300

    def log(self, text: str) -> None:
        safe = redact_log_text(text, self.logs.redactions)
        bounded = safe.encode("utf-8")[:1024].decode("utf-8", errors="ignore")
        self.logs.append(bounded, "observed_during_run")

    def read(self, principal: str, request: AppEventsRequest) -> dict[str, Any]:
        if principal != self.principal:
            raise ValueError("app_events_not_available")
        with self.lock:
            cursor = request.cursor
            reset = cursor is not None and cursor.epoch != self.epoch
            control_after = 0 if cursor is None or reset else cursor.control
            log_after = 0 if cursor is None or reset else cursor.log
            if control_after > self.sequence:
                raise ValueError("cursor_ahead")
            expired = time.monotonic() >= self.deadline
            if expired:
                self.control.clear()
            oldest = self.control[0]["sequence"] if self.control else self.sequence + 1
            entries = [e for e in self.control if e["sequence"] > control_after][: request.limit]
            next_control = entries[-1]["sequence"] if entries else self.sequence
            log_result = self.logs.read(
                principal,
                BuildLogRequest(
                    job_id=request.job_id,
                    cursor=LogCursor(epoch=self.logs.epoch, sequence=log_after),
                    levels=["error", "warning", "info", "debug", "unknown"],
                    context=0,
                    limit=request.limit,
                ),
            )
            next_log = log_result["next_cursor"]["sequence"]
            log_oldest = log_result.get("oldest_sequence", self.logs.sequence + 1)
            return dict(
                entries=entries,
                logs=log_result["entries"],
                log_attribution="observed_during_run",
                oldest_seq=dict(control=oldest, log=log_oldest),
                next_cursor=dict(epoch=self.epoch, control=next_control, log=next_log),
                has_more=next_control < self.sequence or log_result["has_more"],
                gap=reset or expired or control_after < oldest - 1 or log_result["gap"],
                dropped_count=dict(control=max(0, oldest - 1), log=max(0, log_oldest - 1)),
                expired=expired or log_result["expired"],
                expiry_seconds=max(0, int(self.deadline - time.monotonic())),
            )
