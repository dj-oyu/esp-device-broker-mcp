"""DERBY-only revocation checks around owned IO; close always remains allowed."""

import threading
from collections.abc import Callable
from typing import Any

from .serial_backend import SerialFailure, SerialPort


class SmokePort:
    def __init__(
        self, port: SerialPort, live: Callable[[], bool] | None, stop: threading.Event
    ) -> None:
        self.inner, self.live, self.stop = port, live, stop

    def check(self) -> None:
        if self.stop.is_set() or self.live is None or self.live() is not True:
            raise SerialFailure("operation_interrupted_review_required")

    def __getattr__(self, key: str) -> Any:
        return getattr(self.inner, key)

    @property
    def dtr(self) -> bool:
        return self.inner.dtr

    @dtr.setter
    def dtr(self, value: bool) -> None:
        self.check()
        self.inner.dtr = value

    @property
    def rts(self) -> bool:
        return self.inner.rts

    @rts.setter
    def rts(self, value: bool) -> None:
        self.check()
        self.inner.rts = value

    @property
    def port(self) -> str | None:
        return self.inner.port

    @port.setter
    def port(self, value: str | None) -> None:
        self.check()
        self.inner.port = value

    def open(self) -> None:
        self.check()
        self.inner.open()
        self.check()

    def read(self, size: int = 1) -> bytes:
        self.check()
        data = self.inner.read(size)
        self.check()
        return data

    def write(self, data: bytes) -> int | None:
        self.check()
        return self.inner.write(data)

    def close(self) -> None:
        self.inner.close()
