"""Revision-only notifications; threads never project state or retain output."""

import asyncio
import threading
from collections.abc import Callable


class OperatorNotifications:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._revision = 0
        self._logs_revision = 0
        self._loop: asyncio.AbstractEventLoop | None = None
        self._pending = False
        self._readers: set[Callable[[], None]] = set()

    def revision(self) -> tuple[int, int]:
        with self._lock:
            return self._revision, self._logs_revision

    def notify(self) -> None:
        self._notify(False)

    def notify_logs(self) -> None:
        self._notify(True)

    def _notify(self, logs: bool) -> None:
        with self._lock:
            self._revision += 1
            self._logs_revision += int(logs)
            loop = self._loop
            if loop is None or loop.is_closed() or self._pending:
                return
            self._pending = True
        try:
            loop.call_soon_threadsafe(self._wake)
        except RuntimeError:
            with self._lock:
                self._pending = False

    def _wake(self) -> None:
        with self._lock:
            self._pending = False
            readers = tuple(self._readers)
        for reader in readers:
            reader()

    def subscribe(self, reader: Callable[[], None]) -> Callable[[], None]:
        with self._lock:
            self._loop = asyncio.get_running_loop()
            self._readers.add(reader)

        def unsubscribe() -> None:
            with self._lock:
                self._readers.discard(reader)

        return unsubscribe
