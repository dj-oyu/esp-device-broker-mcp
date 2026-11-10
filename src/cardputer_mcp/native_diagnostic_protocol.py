"""Bounded native diagnostic frames; no arbitrary commands or firmware authority."""

import re
from dataclasses import dataclass

ZERO_ID = "0" * 32
ZERO_BOOT = "0" * 16
HOST_LIMIT = 64
DEVICE_LIMIT = 80
ERROR = "native_diagnostic_protocol_invalid"
_STATES = "PENDING|RUNNING|CANCEL_REQUESTED|COMPLETED|CANCELLED|REFUSED|QUARANTINED"
_RESPONSE = re.compile(
    rb"\x1dND1 ([0-9a-f]{32}) (" + _STATES.encode() + rb") ([0-3][0-9a-f]) ([0-9a-f]{16}) ([01])\n"
)


def command(operation: str, request_id: str, boot: str) -> bytes:
    if (
        operation not in ("REQUEST", "STATUS", "CANCEL")
        or re.fullmatch(r"[0-9a-f]{32}", request_id) is None
        or re.fullmatch(r"[0-9a-f]{16}", boot) is None
        or (
            (request_id == ZERO_ID or boot == ZERO_BOOT)
            and (operation, request_id, boot) != ("STATUS", ZERO_ID, ZERO_BOOT)
        )
    ):
        raise ValueError(ERROR)
    raw = f"\x1dD1 {operation} {request_id} {boot}\n".encode("ascii")
    if len(raw) > HOST_LIMIT:
        raise ValueError(ERROR)
    return raw


@dataclass(frozen=True)
class Snapshot:
    request_id: str
    state: str
    flags: int
    boot: str
    enabled: bool

    @property
    def terminal(self) -> bool:
        return self.state in ("COMPLETED", "CANCELLED", "REFUSED", "QUARANTINED")

    @property
    def completed(self) -> bool:
        return self.state == "COMPLETED" and self.flags == 0x1F

    @property
    def cancelled(self) -> bool:
        return self.state == "CANCELLED" and self.flags in (0x1F, 0x3E)


def parse(raw: bytes) -> Snapshot:
    match = _RESPONSE.fullmatch(raw) if len(raw) <= DEVICE_LIMIT else None
    if match is None:
        raise ValueError(ERROR)
    request_id, state, flags, boot, enabled = (part.decode("ascii") for part in match.groups())
    bits = int(flags, 16)
    if boot == ZERO_BOOT or bits & 1 and bits & 0x20:
        raise ValueError(ERROR)
    if (
        state == "COMPLETED"
        and bits != 0x1F
        or state == "CANCELLED"
        and bits not in (0x1F, 0x3E)
        or state == "REFUSED"
        and bits not in (0, 0x20)
    ):
        raise ValueError(ERROR)
    return Snapshot(request_id, state, bits, boot, enabled == "1")


class Decoder:
    """Ignore ordinary console text, cap reserved frames and resync on the next GS.

    Reserved malformed ND1 frames fail closed. Partial data is bounded across reads;
    no console or raw protocol bytes are retained in public job diagnostics.
    """

    def __init__(self) -> None:
        self.pending = bytearray()

    def feed(self, data: bytes) -> list[Snapshot]:
        result = []
        for byte in data:
            if byte == 0x1D:
                self.pending = bytearray([byte])
            elif self.pending:
                self.pending.append(byte)
                if len(self.pending) > DEVICE_LIMIT:
                    reserved = self.pending.startswith(b"\x1dND1")
                    self.pending.clear()
                    if reserved:
                        raise ValueError(ERROR)
                elif byte == 10:
                    raw = bytes(self.pending)
                    self.pending.clear()
                    if raw.startswith(b"\x1dND1"):
                        result.append(parse(raw))
        return result
