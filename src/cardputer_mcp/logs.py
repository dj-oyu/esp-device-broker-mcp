"""Bounded fan-out of generated events, never raw device bytes or request arguments."""

from collections import deque
from collections.abc import Callable
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4

from .models import LogRequest


class Event(StrEnum):
    STARTED = "broker_started"
    ACQUIRED = "session_acquired"
    RELEASED = "session_released"
    EXPIRED = "session_expired"
    QUEUED = "job_queued"
    STAGE = "stage_started"
    PASSED = "job_passed"
    REVIEW = "job_needs_review"
    CANCELLED = "job_cancelled"
    STOPPED = "broker_stopped"


class LogBook:
    def __init__(self, capacity: int, *, on_change: Callable[[], None] | None = None) -> None:
        self.on_change = on_change
        self.epoch = uuid4().hex
        self.sequence = 0
        self.entries: deque[dict[str, Any]] = deque(maxlen=capacity)

    def append(self, event: Event, *, stage: str | None = None) -> None:
        # Deliberately excludes all caller-supplied strings, bearer capabilities,
        # USB identifiers, URLs, exceptions, and raw firmware log content.
        self.sequence += 1
        self.entries.append(
            {
                "sequence": self.sequence,
                "time": datetime.now(UTC).isoformat(),
                "event": event.value,
                "stage": stage,
            }
        )

        if self.on_change is not None:
            try:
                self.on_change()
            except Exception:
                pass

    def read(self, request: LogRequest) -> dict[str, Any]:
        cursor = request.cursor
        reset = cursor is not None and cursor.epoch != self.epoch
        after = 0 if cursor is None or reset else cursor.sequence
        oldest = self.entries[0]["sequence"] if self.entries else self.sequence + 1
        gap = reset or (cursor is not None and after < oldest - 1)
        if after > self.sequence:
            raise ValueError("cursor_ahead")
        entries = [item for item in self.entries if item["sequence"] > after][: request.limit]
        next_sequence = entries[-1]["sequence"] if entries else self.sequence
        return {
            "entries": entries,
            "next_cursor": {"epoch": self.epoch, "sequence": next_sequence},
            "gap": gap,
            "oldest_sequence": oldest,
            "has_more": next_sequence < self.sequence,
        }
