"""App control v1 bounded native frames; separate from the guest GS B bridge."""

import struct
import zlib
from dataclasses import dataclass
from enum import IntEnum

PREFIX = b"\x1dC"
MAX_PAYLOAD = 480
MAX_WIRE = 995
_HEADER = struct.Struct("<BBHII")
_REQUEST = struct.Struct("<QIIII32sB3x")
_REPLY = struct.Struct("<QIIIBBBBIIHHI32s48s")


class Op(IntEnum):
    CATALOG = 1
    OPEN = 2
    START = 3
    TAP = 4
    STATUS = 5
    SUSPEND = 6
    STOP = 7
    CLOSE = 8


class Result(IntEnum):
    OK = 0
    ACCEPTED = 1
    CONSUMED = 2
    UNKNOWN_APP = 3
    UNSUPPORTED = 4
    STALE_BOOT = 5
    STALE_SESSION = 6
    STALE_LEASE = 7
    STALE_RUN = 8
    OUT_OF_ORDER = 9
    CONFLICT = 10
    QUEUE_FULL = 11
    BLOCKED = 12
    BUSY = 13
    OUTCOME_UNKNOWN = 14
    LIMIT = 15
    EXPIRED = 16
    INVALID = 17
    TEARDOWN_FAILED = 18


class State(IntEnum):
    NO_SESSION = 0
    READY = 1
    RUNNING = 2
    SUSPENDED = 3
    STOPPED = 4
    NEEDS_REVIEW = 5


def _text(value: str, size: int) -> bytes:
    if not isinstance(value, str) or "\0" in value:
        raise ValueError("app_wire_invalid")
    try:
        data = value.encode("ascii")
    except UnicodeError:
        raise ValueError("app_wire_invalid") from None
    if len(data) >= size or any(c < 32 or c > 126 for c in data):
        raise ValueError("app_wire_invalid")
    return data.ljust(size, b"\0")


def _read_text(data: bytes) -> str:
    value, separator, padding = data.partition(b"\0")
    if not separator or any(padding) or any(c < 32 or c > 126 for c in value):
        raise ValueError("app_wire_invalid")
    return value.decode("ascii")


def frame(kind: int, session: int, command_id: int, payload: bytes) -> bytes:
    if len(payload) > MAX_PAYLOAD:
        raise ValueError("app_wire_invalid")
    try:
        body = _HEADER.pack(1, kind, len(payload), session, command_id) + payload
    except (struct.error, OverflowError):
        raise ValueError("app_wire_invalid") from None
    return PREFIX + (body + struct.pack("<I", zlib.crc32(body))).hex().encode("ascii") + b"\n"


def unframe(wire: bytes) -> tuple[int, int, int, bytes]:
    if len(wire) > MAX_WIRE or not wire.startswith(PREFIX) or not wire.endswith(b"\n"):
        raise ValueError("app_wire_invalid")
    encoded = wire[2:-1]
    if len(encoded) % 2 or any(c not in b"0123456789abcdef" for c in encoded):
        raise ValueError("app_wire_invalid")
    raw = bytes.fromhex(encoded.decode("ascii"))
    if len(raw) < _HEADER.size + 4:
        raise ValueError("app_wire_invalid")
    body, crc = raw[:-4], raw[-4:]
    version, kind, size, session, command_id = _HEADER.unpack_from(body)
    if (
        version != 1
        or size > MAX_PAYLOAD
        or len(body) != _HEADER.size + size
        or struct.unpack("<I", crc)[0] != zlib.crc32(body)
    ):
        raise ValueError("app_wire_invalid")
    return kind, session, command_id, body[_HEADER.size :]


@dataclass(frozen=True)
class Request:
    op: Op
    session: int
    command_id: int
    boot: int = 0
    run: int = 0
    seq: int = 0
    fence: int = 0
    ttl: int = 0
    app_id: str = ""
    arg: int = 0

    def encode(self) -> bytes:
        try:
            data = _REQUEST.pack(
                self.boot,
                self.run,
                self.seq,
                self.fence,
                self.ttl,
                _text(self.app_id, 32),
                self.arg,
            )
        except (struct.error, OverflowError):
            raise ValueError("app_wire_invalid") from None
        return frame(int(self.op), self.session, self.command_id, data)

    @classmethod
    def decode(cls, wire: bytes) -> "Request":
        kind, session, command_id, data = unframe(wire)
        if len(data) != 60 or any(data[57:]):
            raise ValueError("app_wire_invalid")
        boot, run, seq, fence, ttl, app_id, arg = _REQUEST.unpack(data)
        return cls(
            Op(kind), session, command_id, boot, run, seq, fence, ttl, _read_text(app_id), arg
        )


@dataclass(frozen=True)
class Reply:
    session: int
    command_id: int
    boot: int
    run: int
    seq: int
    fence: int
    result: Result
    state: State
    op: Op
    arg: int = 0
    next_seq: int = 0
    ttl: int = 0
    catalog_index: int = 0
    catalog_count: int = 0
    flags: int = 0
    app_id: str = ""
    title: str = ""

    @classmethod
    def decode(cls, wire: bytes) -> "Reply":
        kind, session, command_id, data = unframe(wire)
        if kind != 128 or len(data) != 120:
            raise ValueError("app_wire_invalid")
        (
            boot,
            run,
            seq,
            fence,
            result,
            state,
            op,
            arg,
            next_seq,
            ttl,
            index,
            count,
            flags,
            app_id,
            title,
        ) = _REPLY.unpack(data)
        name = app_id.hex() if op == Op.CATALOG and index == 0xFFFF else _read_text(app_id)
        return cls(
            session,
            command_id,
            boot,
            run,
            seq,
            fence,
            Result(result),
            State(state),
            Op(op),
            arg,
            next_seq,
            ttl,
            index,
            count,
            flags,
            name,
            _read_text(title),
        )

    def encode(self) -> bytes:
        """Host fixture encoder; production replies originate at the UI owner."""
        try:
            if self.op == Op.CATALOG and self.catalog_index == 0xFFFF:
                name = bytes.fromhex(self.app_id)
                if len(name) != 32:
                    raise ValueError("app_wire_invalid")
            else:
                name = _text(self.app_id, 32)
            data = _REPLY.pack(
                self.boot,
                self.run,
                self.seq,
                self.fence,
                self.result,
                self.state,
                self.op,
                self.arg,
                self.next_seq,
                self.ttl,
                self.catalog_index,
                self.catalog_count,
                self.flags,
                name,
                _text(self.title, 48),
            )
        except (struct.error, OverflowError):
            raise ValueError("app_wire_invalid") from None
        return frame(128, self.session, self.command_id, data)


class Decoder:
    """Streaming frame/log split, bounded even for an unterminated flood.

    Only actual GS C frames enter the control parser. Plain log text, GS B frames,
    and invalid control frames can never trigger a write or become an ACK.
    """

    def __init__(self) -> None:
        self._frame = bytearray()
        self._line = bytearray()
        self._gs = False
        self._discard = False
        self.dropped_bytes = 0
        self._truncated = False

    def feed(self, data: bytes) -> tuple[list[Reply], list[str]]:
        replies: list[Reply] = []
        logs: list[str] = []
        for c in data:
            if self._discard:
                if c == 10:
                    self._discard = False
                continue
            if self._frame:
                self._frame.append(c)
                if len(self._frame) > MAX_WIRE:
                    self._frame.clear()
                    self._discard = c != 10
                elif c == 10:
                    try:
                        replies.append(Reply.decode(bytes(self._frame)))
                    except ValueError:
                        pass
                    self._frame.clear()
                continue
            if self._gs:
                self._gs = False
                if c == ord("C"):
                    self._frame.extend(PREFIX)
                    continue
                self._append_log(29, logs)
            if c == 29:
                self._gs = True
            else:
                self._append_log(c, logs)
        return replies, logs

    def _append_log(self, c: int, logs: list[str]) -> None:
        if c == 10:
            text = self._line.decode("utf-8", errors="replace")
            if self._truncated:
                text = text.encode("utf-8")[:1000].decode("utf-8", errors="ignore") + " [truncated]"
            logs.append(text.encode("utf-8")[:1024].decode("utf-8", errors="ignore"))
            self._line.clear()
            self._truncated = False
        elif len(self._line) < 1024:
            self._line.append(c)
        else:
            self._truncated = True
            self.dropped_bytes = min(self.dropped_bytes + 1, 2**63 - 1)
