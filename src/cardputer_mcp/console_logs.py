"""Best-effort bounded console diagnostics. Never write protocol stdout."""

import os
import queue
import re
import threading
from collections.abc import Callable


def _stderr(message: str) -> None:
    # A slow daemon must not hold Python stream locks during interpreter shutdown.
    os.write(2, (message + "\n").encode("utf-8", errors="backslashreplace"))


class ConsoleOutput:
    """One lossy daemon writer; slow/closed consoles cannot hold a job open."""

    def __init__(self, writer: Callable[[str], None] = _stderr, *, capacity: int = 64) -> None:
        self._queue: queue.Queue[str] = queue.Queue(max(1, min(capacity, 64)))
        self._writer = writer
        self._disabled = False
        self.dropped = 0
        self._thread = threading.Thread(target=self._write, name="job-console", daemon=True)
        try:
            self._thread.start()
        except Exception:
            self._disabled = True

    def emit(self, message: str) -> None:
        if self._disabled:
            return
        try:
            # Escape terminal controls AFTER BuildLogs redaction, before clipping.
            text = "".join(
                char if char.isprintable() else f"\\x{ord(char):02x}" for char in message[:4096]
            )[:2048]
            text = text.encode("utf-8")[:2048].decode("utf-8", errors="ignore")
            self._queue.put_nowait(text)
        except queue.Full:
            # Prefer recent status (especially completion) over stale queued output.
            self.dropped = min(self.dropped + 1, 1000000)
            try:
                self._queue.get_nowait()
            except queue.Empty:
                pass
            else:
                self._queue.task_done()
            try:
                self._queue.put_nowait(text)
            except queue.Full:
                pass
        except Exception:
            pass

    def _write(self) -> None:
        while True:
            message = self._queue.get()
            try:
                self._writer(message)
            except Exception:
                # Do not reflect a console exception or retry on another stream.
                self._disabled = True
                return
            finally:
                self._queue.task_done()


_CONSOLE = ConsoleOutput()


def console_line(job_id: str, text: str) -> None:
    """Accept only a bounded public job ID and already-sanitized diagnostic text."""
    if re.fullmatch(r"[A-Za-z0-9_-]{1,64}", job_id) is not None:
        _CONSOLE.emit(f"[job {job_id}] {text}")
