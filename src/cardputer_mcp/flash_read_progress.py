"""Bounded structured ROM read progress; never a backup or authorization receipt."""

import json
import threading
import time
from collections.abc import Callable

from .flash_lifecycle import FlashFailure


class ReadProgress:
    def __init__(
        self, size: int, hard_seconds: float, *, idle_seconds: float = 30,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.size = size
        self.hard_seconds = hard_seconds
        self.idle_seconds = idle_seconds
        self.clock = clock
        self.started = self.last_advanced = clock()
        self.done = 0
        self.invalid = False
        self._lock = threading.Lock()

    def accept(self, line: str) -> str | None:
        with self._lock:
            try:
                value = json.loads(line)
                if (
                    not isinstance(value, dict) or set(value) != {"bytes", "total"}
                    or type(value["bytes"]) is not int or type(value["total"]) is not int
                    or value["total"] != self.size or not self.done < value["bytes"] <= self.size
                ):
                    raise ValueError
                self.done = value["bytes"]
                self.last_advanced = self.clock()
                elapsed = max(0, int(self.last_advanced - self.started))
                return f"info: ROM read {self.done}/{self.size} bytes; elapsed {elapsed} s"
            except (ValueError, TypeError, KeyError):
                self.invalid = True
                return None

    def check(self) -> None:
        with self._lock:
            now = self.clock()
            if self.invalid:
                raise FlashFailure("flash_read_progress_invalid_needs_review")
            if now - self.started >= self.hard_seconds:
                raise FlashFailure("flash_read_deadline_needs_review")
            if now - self.last_advanced >= self.idle_seconds:
                raise FlashFailure("flash_read_stalled_needs_review")
