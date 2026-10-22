"""Single-owner app control exchange on an already opened serial port.

This module never opens, closes, resets, reconnects or replays a mutation. The
caller retains the existing worker, lease, cleanup and serial ownership barrier.
"""

import threading
import time
from collections.abc import Callable
from dataclasses import replace
from typing import Protocol

from .app_wire import Decoder, Op, Reply, Request, Result, State


class AppPort(Protocol):
    def read(self, size: int = 1) -> bytes: ...
    def write(self, data: bytes) -> int | None: ...


class AppTransportError(ValueError):
    """Allowlisted uncertainty only; never expose serial exception messages."""

    def __init__(self, code: str = "outcome_unknown") -> None:
        self.code = code if code in {"outcome_unknown", "app_transport_busy"} else "outcome_unknown"
        super().__init__(self.code)


class ControlConnection:
    def __init__(
        self,
        port: AppPort,
        *,
        clock: Callable[[], float] = time.monotonic,
        pause: Callable[[float], None] = time.sleep,
        log: Callable[[str], None] | None = None,
        control: Callable[[Reply], None] | None = None,
        timeout: float = 1.0,
    ) -> None:
        self.port = port
        self.clock = clock
        self.pause = pause
        self.log = log
        self.control = control
        self.timeout = max(0.05, min(timeout, 2.0))
        self.decoder = Decoder()
        self._lock = threading.Lock()

    def _pump(self) -> list[Reply]:
        try:
            data = self.port.read(512)
        except Exception:
            raise AppTransportError() from None
        if not isinstance(data, bytes) or len(data) > 512:
            raise AppTransportError()
        replies, lines = self.decoder.feed(data)
        if self.log is not None:
            for line in lines:
                self.log(line)
        return replies

    def _write(self, request: Request) -> None:
        wire = request.encode()
        try:
            count = self.port.write(wire)
        except Exception:
            raise AppTransportError() from None
        if count != len(wire):
            # Even a partial mutation is uncertain; a new ID must never replace it.
            raise AppTransportError()

    @staticmethod
    def _matches(reply: Reply, request: Request, expected_op: Op | None) -> bool:
        catalog = request.op == Op.CATALOG and request.session == 0
        if (
            not catalog and reply.session != request.session
        ) or reply.command_id != request.command_id:
            return False
        if expected_op is not None and reply.op != expected_op:
            return False
        if request.op == Op.CATALOG and reply.catalog_index != request.seq:
            return False
        success = reply.result in (Result.OK, Result.ACCEPTED, Result.CONSUMED)
        if success and request.boot and reply.boot != request.boot:
            # Treat a changed boot as lost evidence. Never accept a new boot's ACK.
            raise AppTransportError()
        if success and request.fence and reply.fence != request.fence:
            raise AppTransportError()
        if request.op != Op.CATALOG and request.command_id and reply.seq != request.seq:
            return False
        if (
            success
            and request.run
            and reply.run != request.run
            and not (reply.op in (Op.STOP, Op.CLOSE) and reply.state == State.STOPPED)
        ):
            raise AppTransportError()
        return True

    def _receive(self, request: Request, expected_op: Op | None, deadline: float) -> Reply | None:
        # A hostile log flood cannot suppress the host deadline or grow RAM.
        while self.clock() < deadline:
            for reply in self._pump():
                if self._matches(reply, request, expected_op):
                    if self.control is not None:
                        self.control(reply)
                    return reply
            self.pause(0.001)
        return None

    def send(
        self,
        op: Op,
        session: int,
        command_id: int,
        *,
        boot: int = 0,
        run: int = 0,
        seq: int = 0,
        fence: int = 0,
        ttl: int = 0,
        app_id: str = "",
        arg: int = 0,
    ) -> Reply:
        return self.exchange(
            Request(op, session, command_id, boot, run, seq, fence, ttl, app_id, arg)
        )

    def exchange(self, request: Request) -> Reply:
        if not self._lock.acquire(blocking=False):
            raise AppTransportError("app_transport_busy")
        try:
            self._write(request)
            expected_op = None if request.op == Op.STATUS and request.command_id else request.op
            reply = self._receive(request, expected_op, self.clock() + self.timeout)
            if request.op in (Op.CATALOG, Op.STATUS):
                if reply is None:
                    raise AppTransportError()
                return reply
            if reply is not None and reply.result != Result.ACCEPTED:
                return reply

            # ACK absent or admitted but not consumed: query its ledger only.
            # OPEN has no ledger entry; STATUS id0 identifies the original session.
            query = replace(request, op=Op.STATUS, ttl=0, app_id="", arg=0)
            expected_op = Op.STATUS if request.op == Op.OPEN else request.op
            deadline = self.clock() + self.timeout
            while self.clock() < deadline:
                self._write(query)
                reply = self._receive(query, expected_op, min(deadline, self.clock() + 0.1))
                if reply is not None and reply.result != Result.ACCEPTED:
                    return reply
                self.pause(0.01)
            raise AppTransportError()
        finally:
            self._lock.release()

    def poll(self, timeout: float = 0.05) -> None:
        """Drain console output on the same worker; no control writes are made."""
        if not self._lock.acquire(blocking=False):
            raise AppTransportError("app_transport_busy")
        try:
            deadline = self.clock() + max(0.0, min(timeout, 0.1))
            while self.clock() < deadline:
                self._pump()
                self.pause(0.001)
        finally:
            self._lock.release()

    def catalog(self) -> list[Reply]:
        result: list[Reply] = []
        first = self.send(Op.CATALOG, 0, 0)
        if (
            first.result != Result.OK
            or not first.boot
            or not first.session
            or not 1 <= first.catalog_count <= 64
        ):
            raise AppTransportError()
        for index in range(first.catalog_count):
            item = first if index == 0 else self.send(Op.CATALOG, 0, 0, seq=index)
            if (
                item.result != Result.OK
                or item.boot != first.boot
                or item.session != first.session
                or item.catalog_count != first.catalog_count
                or item.catalog_index != index
                or not item.app_id
                or any(old.app_id == item.app_id for old in result)
            ):
                raise AppTransportError()
            result.append(item)
        return result
