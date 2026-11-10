"""One fixed native diagnostic request with bounded status/cancel observation."""

import hashlib
import json
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .native_diagnostic_protocol import ZERO_BOOT, ZERO_ID, Decoder, Snapshot, command

if TYPE_CHECKING:
    from .serial_backend import SerialPort

CONTRACT = "pocketjs-native-diagnostic-v1"


class RequestNotAdmitted(Exception):
    """Cancel won the serialized cutoff before any REQUEST write was attempted."""


@dataclass
class Progress:
    requested: bool = False
    terminal: bool = False


def request_identity(plan: dict[str, Any]) -> str:
    """The immutable prepared job, including caller lease/epoch, owns one request ID."""
    raw = json.dumps(plan, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(b"native-diagnostic-v1\0" + raw).hexdigest()[:32]


def run(
    port: "SerialPort",
    plan: dict[str, Any],
    clock: Callable[[], float],
    cancel: threading.Event,
    emit: Callable[[str], None],
    tap: Callable[[bytes], None] | None = None,
    progress: Progress | None = None,
) -> Snapshot | None:
    # Local import: SerialBackend dispatches here after it has opened and guarded IO.
    from .serial_backend import SerialFailure

    decoder = Decoder()
    progress = progress if progress is not None else Progress()
    identity = request_identity(plan)
    deadline = clock() + 90
    received = 0

    def send(operation: str, request_id: str, boot: str) -> None:
        raw = command(operation, request_id, boot)
        try:
            written = port.write(raw)
        except RequestNotAdmitted:
            raise
        except SerialFailure:
            raise
        except Exception:
            raise SerialFailure("serial_write_failed") from None
        if written != len(raw):
            raise SerialFailure("incomplete_serial_write")

    def receive() -> list[Snapshot]:
        nonlocal received
        try:
            raw = port.read(256)
        except SerialFailure:
            raise
        except Exception:
            raise SerialFailure("serial_read_failed") from None
        received += len(raw)
        if received > 1024 * 1024:
            raise SerialFailure("serial_capture_limit")
        if tap is not None:
            tap(raw)
        try:
            return decoder.feed(raw)
        except ValueError:
            raise SerialFailure("native_diagnostic_protocol_invalid") from None

    emit("native_diagnostic_bootstrap")
    send("STATUS", ZERO_ID, ZERO_BOOT)
    bootstrap_deadline = min(deadline, clock() + 3)
    bootstrap: Snapshot | None = None
    while clock() < bootstrap_deadline and bootstrap is None:
        if cancel.is_set():
            emit("native_diagnostic_not_started")
            return None
        for snapshot in receive():
            if snapshot.request_id != ZERO_ID or snapshot.state != "REFUSED" or snapshot.flags != 0:
                raise SerialFailure("native_diagnostic_protocol_invalid")
            bootstrap = snapshot
    if cancel.is_set():
        emit("native_diagnostic_not_started")
        return None
    if bootstrap is None:
        raise SerialFailure("native_diagnostic_timeout")
    if not bootstrap.enabled:
        raise SerialFailure("native_diagnostic_not_enabled")
    # There is exactly one REQUEST write. Neither missing admission nor boot change
    # retries it. All subsequent traffic refers to this fixed nonce and request ID.
    boot = bootstrap.boot
    emit("native_diagnostic_request")
    progress.requested = True
    try:
        send("REQUEST", identity, boot)
    except RequestNotAdmitted:
        progress.requested = False
        emit("native_diagnostic_not_started")
        return None
    cancel_sent = False
    next_status = clock() + 0.25
    while clock() < deadline:
        if cancel.is_set() and not cancel_sent:
            emit("native_diagnostic_cancel_requested")
            send("CANCEL", identity, boot)
            cancel_sent = True
        if clock() >= next_status:
            send("STATUS", identity, boot)
            next_status = clock() + 0.25
        for snapshot in receive():
            if snapshot.boot != boot:
                raise SerialFailure("native_diagnostic_boot_changed")
            if snapshot.request_id != identity or not snapshot.enabled:
                raise SerialFailure("native_diagnostic_protocol_invalid")
            if snapshot.completed or snapshot.cancelled:
                progress.terminal = True
                emit("native_diagnostic_terminal")
                return snapshot
            if snapshot.state == "REFUSED" and snapshot.flags == 0x20:
                progress.terminal = True
                emit("native_diagnostic_terminal")
                return snapshot
            if snapshot.terminal:
                raise SerialFailure(
                    "native_diagnostic_quarantined"
                    if snapshot.state == "QUARANTINED"
                    else "native_diagnostic_state_unknown"
                )
    raise SerialFailure("native_diagnostic_timeout")
