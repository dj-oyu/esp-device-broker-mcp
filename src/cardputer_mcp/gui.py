"""Native operator window and headless, independently testable queue controller.

Tkinter is imported only by run(). The GUI never opens serial ports, reads the
broker database, receives capability tokens, or stops the broker when it closes.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from pathlib import Path
from textwrap import wrap
from typing import Any, Literal, Protocol

from .http_diagnostics import DenialReason
from .mcp_diagnostics import parse_summary
from .operator_client import (
    OperatorClient,
    OperatorClientError,
    operator_base_url,
    parse_http_diagnostics,
)


class OperatorAPI(Protocol):
    def state(self) -> dict[str, Any]: ...
    def timeline(self) -> dict[str, Any]: ...
    def cancel_job(self, job_id: str, plan_hash: str, broker_epoch: str) -> dict[str, Any]: ...
    def diagnostics(self) -> dict[str, Any]: ...
    def catalog(self) -> dict[str, Any]: ...
    def cancel(self, request_id: str) -> dict[str, Any]: ...
    def reorder(self, request_id: str, position: int, expected_revision: str) -> dict[str, Any]: ...
    def release(self, lease_id: str) -> dict[str, Any]: ...
    def images(self) -> dict[str, Any]: ...
    def image(self, job_id: str) -> dict[str, Any]: ...


def _text(value: Any, *, optional: bool = False) -> str:
    if optional and value is None:
        return ""
    if not isinstance(value, str) or not value or len(value) > 512:
        raise ValueError("invalid_state")
    # Names may be user-provided. Avoid embedded control characters in tables.
    return "".join(character if character.isprintable() else " " for character in value)


def _seconds(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("invalid_state")
    if not math.isfinite(value) or value < 0:
        raise ValueError("invalid_state")
    return float(value)


def _flag(value: Any) -> bool:
    if not isinstance(value, bool):
        raise ValueError("invalid_state")
    return value


def ttl(value: float | None) -> str:
    """TTLs are displayed as snapshots, without implying local renewal."""
    return "--" if value is None else f"{math.ceil(value)} s"


def http_diagnostics_summary(data: dict[str, Any]) -> str:
    data = parse_http_diagnostics(data)
    lines = [
        "HTTP denials since this broker started (schema 1)",
        f"Total denials: {data['total_denials']}",
        "",
    ]
    nonzero = [
        f"{reason.value}: {data['counts'][reason.value]}"
        for reason in DenialReason
        if data["counts"][reason.value]
    ]
    lines.extend(nonzero or ["No denial counters recorded."])
    lines.append("")
    last = data["last_denial"]
    if last is None:
        lines.append("Last denial: none")
    else:
        lines.extend(
            (
                f"Last reason: {last['reason']}",
                f"Layer: {last['layer']} | Route: {last['route']} | HTTP: {last['http_status']}",
            )
        )
        context = last.get("context")
        if context is None:
            suffix = " (older broker)" if "context_version" not in data else ""
            lines.append(f"Request details unavailable{suffix}.")
        else:
            lines.extend(
                (
                    f"Entry: {context['entry']} | Stage: {context['stage']}",
                    f"Method: {context['method']} | Content-Type: {context['content_type']}",
                )
            )
            for label, key in (
                ("Forwarding headers", "forwarding_headers"),
                ("Duplicate headers", "duplicate_headers"),
            ):
                names = ", ".join(context[key]) or "none"
                lines.extend(wrap(f"{label}: {names}", width=88, subsequent_indent="  "))
    if "recent_denials" in data:
        recent = data["recent_denials"][-4:]
        if recent:
            lines.extend(("", "Recent denials (newest first, up to 4):"))
            for entry in reversed(recent):
                context = entry["context"]
                lines.append(
                    f"#{entry['sequence']} {entry['reason']} | HTTP {entry['http_status']} | "
                    f"{context['entry']}/{context['stage']} | "
                    f"{context['method']} {context['content_type']}"
                )
    elif last is None:
        lines.append("Request details unavailable (older broker).")
    lines.extend(("", "Counters reset on broker restart. Zero does not prove connectivity."))
    return "\n".join(lines)


def mcp_catalog_summary(data: dict[str, Any]) -> str:
    """Render only the validated, bounded public MCP diagnostic fields."""
    try:
        data = parse_summary(data)
    except ValueError:
        raise OperatorClientError("The broker returned invalid MCP info.") from None
    return "\n".join(
        (
            f"gui_version: {data['gui_version']}",
            f"server_version: {data['server_version']}",
            f"tools_count: {data['tools_count']}",
            f"read_capture: {str(data['read_capture']).lower()}",
            f"capture_home: {str(data['capture_home']).lower()}",
            f"broker_epoch: {data['broker_epoch']}",
        )
    )


@dataclass(frozen=True)
class SessionRow:
    session_id: str
    owner: str
    principal_id: str
    scopes: str
    state: str
    expires_in_seconds: float | None
    request_id: str


@dataclass(frozen=True)
class WaiterRow:
    request_id: str
    session_id: str
    state: str
    position: int | None
    wait_remaining_seconds: float | None
    lease_remaining_seconds: float | None
    lease_id: str
    active_job: str


@dataclass(frozen=True)
class OwnerRow:
    lease_id: str
    request_id: str
    session_id: str
    owner: str
    principal_id: str
    lease_remaining_seconds: float | None
    release_blocked: bool


_CODE = re.compile(r"[a-z][a-z0-9_]{1,80}")


def _code(value: Any) -> str:
    if type(value) is not str or _CODE.fullmatch(value) is None:
        raise ValueError("invalid_state")
    return value


@dataclass(frozen=True)
class RecoveryRequestRow:
    """An unreviewed needs_review device request; acknowledgement only."""

    request_id: str
    reason: str
    created: str


@dataclass(frozen=True)
class UncertaintyRow:
    flag: str
    reason: str


def _recovery_requests(value: Any) -> tuple[RecoveryRequestRow, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or len(value) > 1000:
        raise ValueError("invalid_state")
    rows = []
    for row in value:
        request_id = _text(row["id"])
        if re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", request_id) is None:
            raise ValueError("invalid_state")
        if row.get("state") != "needs_review" or row.get("reviewed") not in (0, False):
            raise ValueError("invalid_state")
        reason = row.get("reason")
        rows.append(
            RecoveryRequestRow(
                request_id,
                _code(reason) if reason is not None else "unknown",
                _text(row.get("created"), optional=True)[:40],
            )
        )
    if len({row.request_id for row in rows}) != len(rows):
        raise ValueError("invalid_state")
    return tuple(rows)


def _uncertainty(value: Any) -> tuple[UncertaintyRow, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or len(value) > 32:
        raise ValueError("invalid_state")
    return tuple(UncertaintyRow(_code(row["flag"]), _code(row["reason"])) for row in value)


@dataclass(frozen=True)
class QueueSnapshot:
    backend: str
    queue_revision: str
    sessions: tuple[SessionRow, ...]
    waiters: tuple[WaiterRow, ...]
    owner: OwnerRow | None
    active_job: str
    needs_review: bool
    ownership_uncertain: bool
    execution_uncertain: bool | None = None
    recovery_requests: tuple[RecoveryRequestRow, ...] = ()
    uncertainty: tuple[UncertaintyRow, ...] = ()

    @classmethod
    def parse(cls, data: dict[str, Any]) -> QueueSnapshot:
        try:
            backend = _text(data["backend"]).upper()
            if backend not in ("MOCK", "SERIAL"):
                raise ValueError("invalid_state")
            if not isinstance(data["sessions"], list) or not isinstance(data["waiters"], list):
                raise ValueError("invalid_state")
            sessions: list[SessionRow] = []
            for row in data["sessions"]:
                if not isinstance(row["scopes"], list):
                    raise ValueError("invalid_state")
                sessions.append(
                    SessionRow(
                        _text(row["session_id"]),
                        _text(row["owner"]),
                        _text(row["principal_id"]),
                        ", ".join(_text(scope) for scope in row["scopes"]),
                        _text(row["state"]).upper(),
                        _seconds(row["expires_in_seconds"]),
                        _text(row.get("request_id"), optional=True),
                    )
                )
            waiters: list[WaiterRow] = []
            for row in data["waiters"]:
                position = row["position"]
                if position is not None and (
                    isinstance(position, bool) or not isinstance(position, int) or position < 1
                ):
                    raise ValueError("invalid_state")
                state = _text(row["state"]).upper()
                if state == "WAITING" and position is None:
                    raise ValueError("invalid_state")
                waiters.append(
                    WaiterRow(
                        _text(row["request_id"]),
                        _text(row["session_id"]),
                        state,
                        position,
                        _seconds(row.get("wait_remaining_seconds")),
                        _seconds(row.get("lease_remaining_seconds")),
                        _text(row.get("lease_id"), optional=True),
                        _text(row.get("active_job"), optional=True),
                    )
                )
            owner = None
            if data["owner"] is not None:
                row = data["owner"]
                owner = OwnerRow(
                    _text(row["lease_id"]),
                    _text(row.get("request_id"), optional=True),
                    _text(row.get("session_id"), optional=True),
                    _text(row["owner"]),
                    _text(row["principal_id"]),
                    _seconds(row["lease_remaining_seconds"]),
                    _flag(row["release_blocked"]),
                )
            if len({row.session_id for row in sessions}) != len(sessions):
                raise ValueError("invalid_state")
            if len({row.request_id for row in waiters}) != len(waiters):
                raise ValueError("invalid_state")
            positions = [
                row.position
                for row in waiters
                if row.state == "WAITING" and row.position is not None
            ]
            if sorted(positions) != list(range(1, len(positions) + 1)):
                raise ValueError("invalid_state")
            return cls(
                backend,
                _text(data["queue_revision"]),
                tuple(sessions),
                tuple(sorted(waiters, key=lambda row: row.position or 0)),
                owner,
                _text(data.get("active_job"), optional=True),
                _flag(data["needs_review"]),
                _flag(data["ownership_uncertain"]),
                _flag(data["execution_uncertain"]) if "execution_uncertain" in data else None,
                _recovery_requests(data.get("recovery_requests")),
                _uncertainty(data.get("uncertainty")),
            )
        except (KeyError, TypeError, ValueError, AttributeError):
            raise OperatorClientError("The broker returned an invalid queue snapshot.") from None


@dataclass(frozen=True)
class Operation:
    sequence: int
    action: Literal["refresh", "cancel", "reorder", "release", "diagnostics", "catalog"]
    target: str = ""
    position: int = 0
    expected_revision: str = ""


@dataclass(frozen=True)
class Outcome:
    sequence: int
    snapshot: QueueSnapshot | None
    message: str
    failed: bool = False
    diagnostics: str | None = None
    catalog: str | None = None


def perform(api: OperatorAPI, operation: Operation) -> Outcome:
    """Read the queue or perform one explicit queue action; no approval polling."""
    return _perform_queue(api, operation)


def _perform_queue(api: OperatorAPI, operation: Operation) -> Outcome:
    """Runs on the worker thread. Mutations, including refusals, refresh state."""
    if operation.action == "catalog":
        # This probe has its own total deadline. Do not extend it with another
        # HTTP request; the next ordinary refresh will update the queue snapshot.
        try:
            catalog = mcp_catalog_summary(api.catalog())
        except OperatorClientError as exc:
            return Outcome(operation.sequence, None, str(exc), True)
        except Exception:
            return Outcome(
                operation.sequence, None, "MCP info failed. Check the local broker.", True
            )
        return Outcome(operation.sequence, None, "MCP info loaded", catalog=catalog)
    message = "Queue refreshed"
    failed = False
    diagnostics = None
    try:
        if operation.action == "diagnostics":
            diagnostics = http_diagnostics_summary(api.diagnostics())
            message = "HTTP diagnostics loaded"
        elif operation.action == "cancel":
            api.cancel(operation.target)
            message = "Waiting request cancelled"
        elif operation.action == "reorder":
            api.reorder(operation.target, operation.position, operation.expected_revision)
            message = "Waiting queue reordered"
        elif operation.action == "release":
            api.release(operation.target)
            message = "Idle ownership released"
    except OperatorClientError as exc:
        message, failed = str(exc), True
    except Exception:
        message, failed = "Operator request failed. Refresh before trying again.", True
    try:
        snapshot = QueueSnapshot.parse(api.state())
    except OperatorClientError as exc:
        prefix = (
            "Action outcome may be uncertain. "
            if operation.action in ("cancel", "reorder", "release")
            else ""
        )
        return Outcome(operation.sequence, None, prefix + str(exc), True, diagnostics)
    except Exception:
        return Outcome(
            operation.sequence, None, "Refresh failed. Check the local broker.", True, diagnostics
        )
    return Outcome(operation.sequence, snapshot, message, failed, diagnostics)


class QueueController:
    """Selection and stale-result guards, with no dependency on Tk or threads."""

    def __init__(self) -> None:
        self.snapshot: QueueSnapshot | None = None
        self.selected_request = ""
        self.selected_lease = ""
        self.sequence = 0
        self.busy = False
        self.fresh = False
        self.closed = False
        self.message = "Connecting to the local broker..."
        self.failed = False

    def selected_waiter(self) -> WaiterRow | None:
        if not self.snapshot:
            return None
        return next(
            (row for row in self.snapshot.waiters if row.request_id == self.selected_request), None
        )

    def can_cancel(self) -> bool:
        row = self.selected_waiter()
        return self._ready() and row is not None and row.state == "WAITING"

    def can_move(self, direction: int) -> bool:
        row = self.selected_waiter()
        if not self.can_cancel() or row is None or row.position is None or direction not in (-1, 1):
            return False
        assert self.snapshot is not None
        count = sum(item.state == "WAITING" for item in self.snapshot.waiters)
        return 1 <= row.position + direction <= count

    def can_release(self) -> bool:
        snapshot = self.snapshot
        return bool(
            self._ready()
            and snapshot
            and snapshot.owner
            and self.selected_lease == snapshot.owner.lease_id
            and not snapshot.active_job
            and not snapshot.owner.release_blocked
            and not snapshot.ownership_uncertain
        )

    def _ready(self) -> bool:
        return self.fresh and not self.busy and not self.closed

    def _begin(
        self,
        action: Literal["refresh", "cancel", "reorder", "release", "diagnostics", "catalog"],
        target: str = "",
        position: int = 0,
        expected_revision: str = "",
    ) -> Operation | None:
        if self.busy or self.closed:
            return None
        self.sequence += 1
        self.busy = True
        self.message = "Refreshing..." if action == "refresh" else "Applying change..."
        if action == "diagnostics":
            self.message = "Reading HTTP diagnostics..."
        elif action == "catalog":
            self.message = "Reading MCP info..."
        self.failed = False
        return Operation(self.sequence, action, target, position, expected_revision)

    def refresh(self) -> Operation | None:
        return self._begin("refresh")

    def diagnostics(self) -> Operation | None:
        return self._begin("diagnostics")

    def catalog(self) -> Operation | None:
        return self._begin("catalog")

    def cancel(self) -> Operation | None:
        return self._begin("cancel", self.selected_request) if self.can_cancel() else None

    def move(self, direction: int) -> Operation | None:
        if not self.can_move(direction):
            return None
        row = self.selected_waiter()
        assert row is not None and row.position is not None
        assert self.snapshot is not None
        return self._begin(
            "reorder", row.request_id, row.position + direction, self.snapshot.queue_revision
        )

    def release(self, confirmed_lease_id: str) -> Operation | None:
        if not self.can_release() or confirmed_lease_id != self.selected_lease:
            return None
        return self._begin("release", confirmed_lease_id)

    def accept(self, outcome: Outcome) -> bool:
        if self.closed or not self.busy or outcome.sequence != self.sequence:
            return False
        self.busy = False
        self.fresh = outcome.snapshot is not None
        self.message, self.failed = outcome.message, outcome.failed
        if outcome.snapshot is not None:
            self.snapshot = outcome.snapshot
            if self.selected_waiter() is None:
                self.selected_request = ""
            if not self.snapshot.owner or self.selected_lease != self.snapshot.owner.lease_id:
                self.selected_lease = ""
        return True

    def close(self) -> None:
        self.closed = True
        self.fresh = False
        self.sequence += 1


def run(
    url: str, token: str, *, broker_config: Path | None = None, project: Path | None = None
) -> None:
    """Open the native GUI; the caller obtains the token from process environment."""
    api = OperatorClient(url, token)
    from .gui_theme import prepare_display

    prepare_display()
    try:
        import tkinter as tk
    except ImportError:
        raise OperatorClientError(
            "Tkinter is required. Install Python with Tcl/Tk support."
        ) from None
    try:
        root = tk.Tk()
    except tk.TclError:
        raise OperatorClientError("The native GUI requires an available desktop display.") from None
    from .gui_tunnel import TunnelPanel

    panel = TunnelPanel(
        root, operator_base_url(url) + "/mcp", broker_config=broker_config, project=project
    )
    from .gui_live import LiveWindow

    try:
        LiveWindow(root, api, panel)
    except Exception:
        root.destroy()
        raise
    root.mainloop()
