"""Native operator window and headless, independently testable queue controller.

Tkinter is imported only by run(). The GUI never opens serial ports, reads the
broker database, receives capability tokens, or stops the broker when it closes.
"""

from __future__ import annotations

import math
import queue
import threading
from dataclasses import dataclass
from pathlib import Path
from textwrap import wrap
from typing import TYPE_CHECKING, Any, Literal, Protocol

from .http_diagnostics import DenialReason
from .mcp_diagnostics import parse_summary
from .operator_client import (
    OperatorClient,
    OperatorClientError,
    operator_base_url,
    parse_http_diagnostics,
)

if TYPE_CHECKING:
    import tkinter as tk
    from tkinter import ttk

    from .gui_tunnel import TunnelPanel


class OperatorAPI(Protocol):
    def state(self) -> dict[str, Any]: ...
    def timeline(self) -> dict[str, Any]: ...
    def cancel_job(self, job_id: str, plan_hash: str, broker_epoch: str) -> dict[str, Any]: ...
    def diagnostics(self) -> dict[str, Any]: ...
    def catalog(self) -> dict[str, Any]: ...
    def cancel(self, request_id: str) -> dict[str, Any]: ...
    def reorder(self, request_id: str, position: int, expected_revision: str) -> dict[str, Any]: ...
    def release(self, lease_id: str) -> dict[str, Any]: ...
    def local_home_line(
        self, job_id: str, plan_hash: str, diagnostic_id: str
    ) -> dict[str, Any]: ...
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


class _Window:
    def __init__(
        self, root: tk.Tk, api: OperatorAPI, tunnel_panel: TunnelPanel | None = None
    ) -> None:
        import tkinter as tk
        from tkinter import ttk

        from .gui_timeline import TimelinePanel

        self.root, self.api = root, api
        self.tunnel_panel = tunnel_panel
        self.controller = QueueController()
        self.commands: queue.Queue[Operation | None] = queue.Queue()
        self.results: queue.Queue[Outcome] = queue.Queue()
        self.stopped = threading.Event()
        root.title("Cardputer Device Timeline")
        root.geometry("1380x900")
        root.minsize(1100, 720)
        root.protocol("WM_DELETE_WINDOW", self._close)
        root.columnconfigure(0, weight=1)
        root.rowconfigure(1, weight=1)
        style = ttk.Style(root)
        style.configure("Title.TLabel", font=("TkDefaultFont", 18, "bold"))
        style.configure("Heading.TLabel", font=("TkDefaultFont", 11, "bold"))
        style.configure("Treeview", rowheight=25)
        header = ttk.Frame(root, padding=(18, 14, 18, 8))
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(0, weight=1)
        ttk.Label(header, text="Cardputer Device Timeline", style="Title.TLabel").grid(
            row=0, column=0, sticky="w"
        )
        self.mode = tk.Label(
            header,
            text="CONNECTING",
            padx=12,
            pady=6,
            bg="#e2e8f0",
            fg="#0f172a",
            font=("TkDefaultFont", 11, "bold"),
        )
        self.mode.grid(row=0, column=1, padx=(12, 0))
        self.diagnostics_button = ttk.Button(
            header,
            text="HTTP diagnostics",
            command=lambda: self._submit(self.controller.diagnostics()),
        )
        self.diagnostics_button.grid(row=0, column=2, padx=(12, 0))
        self.catalog_button = ttk.Button(
            header,
            text="MCP info",
            command=lambda: self._submit(self.controller.catalog()),
        )
        self.catalog_button.grid(row=0, column=3, padx=(12, 0))
        if tunnel_panel is not None:
            ttk.Button(header, text="Tunnel...", command=tunnel_panel.show).grid(
                row=0, column=6, padx=(12, 0)
            )
        ttk.Label(header, text="One broker | One physical owner | FIFO waiting queue").grid(
            row=1, column=0, columnspan=2, sticky="w", pady=(5, 0)
        )
        body = ttk.Frame(root, padding=(18, 4, 18, 8))
        body.grid(row=1, column=0, sticky="nsew")
        body.columnconfigure(0, weight=1)
        body.columnconfigure(1, weight=0, minsize=360)
        body.rowconfigure(1, weight=1)
        self.health = tk.StringVar(value="Waiting for broker state")
        self.job = tk.StringVar(value="Active job: --")
        ttk.Label(body, textvariable=self.health, style="Heading.TLabel", wraplength=1050).grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 6)
        )
        timeline_frame = ttk.Frame(body)
        timeline_frame.grid(row=1, column=0, sticky="nsew", padx=(0, 14))
        self.timeline_panel = TimelinePanel(root, timeline_frame, api)
        sidebar = ttk.Frame(body, width=360)
        sidebar.grid(row=1, column=1, sticky="nsew")
        sidebar.grid_propagate(False)
        sidebar.columnconfigure(0, weight=1)
        sidebar.rowconfigure(1, weight=1)
        sidebar.rowconfigure(2, weight=2)
        owner_box = ttk.LabelFrame(sidebar, text="Current physical owner", padding=8)
        owner_box.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        self.owner_tree = self._table(
            owner_box,
            (
                ("owner", "Owner", 140),
                ("lease", "Public lease ID", 240),
                ("session", "Session ID", 240),
                ("request", "Request ID", 240),
                ("ttl", "Lease TTL", 90),
            ),
            height=1,
        )
        owner_footer = ttk.Frame(owner_box)
        owner_footer.pack(fill="x", pady=(6, 0))
        ttk.Label(owner_footer, textvariable=self.job).pack(anchor="w")
        self.release_button = ttk.Button(
            owner_footer, text="Release selected idle owner...", command=self._release
        )
        self.release_button.pack(anchor="e", pady=(5, 0))
        sessions_box = ttk.LabelFrame(
            sidebar, text="Logical work sessions (active + recent)", padding=8
        )
        sessions_box.grid(row=1, column=0, sticky="nsew", pady=(0, 10))
        self.session_tree = self._table(
            sessions_box,
            (
                ("id", "Session ID", 250),
                ("owner", "Owner", 150),
                ("principal", "Principal ID", 210),
                ("scopes", "Scopes", 120),
                ("state", "State", 90),
                ("ttl", "Session TTL", 90),
                ("request", "Request ID", 240),
            ),
            height=4,
        )
        queue_box = ttk.LabelFrame(sidebar, text="Physical device waiting queue", padding=8)
        queue_box.grid(row=2, column=0, sticky="nsew")
        actions = ttk.Frame(queue_box)
        actions.pack(side="bottom", fill="x", pady=(8, 0))
        self.cancel_button = ttk.Button(
            actions,
            text="Cancel selected waiting request",
            command=lambda: self._submit(self.controller.cancel()),
        )
        self.cancel_button.grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 5))
        self.up_button = ttk.Button(actions, text="Move up", command=lambda: self._move(-1))
        self.up_button.grid(row=1, column=0, sticky="w")
        self.down_button = ttk.Button(actions, text="Move down", command=lambda: self._move(1))
        self.down_button.grid(row=1, column=1, sticky="w", padx=(8, 0))
        self.queue_tree = self._table(
            queue_box,
            (
                ("position", "Position", 70),
                ("request", "Request ID", 240),
                ("session", "Session ID", 240),
                ("state", "State", 110),
                ("wait", "Wait TTL", 90),
                ("lease_ttl", "Lease TTL", 90),
                ("lease", "Public lease ID", 240),
                ("job", "Active job", 180),
            ),
            height=5,
        )
        self.queue_tree.bind("<<TreeviewSelect>>", self._select)
        self.owner_tree.bind("<<TreeviewSelect>>", self._select)
        footer = ttk.Frame(root, padding=(18, 4, 18, 14))
        footer.grid(row=2, column=0, sticky="ew")
        footer.columnconfigure(0, weight=1)
        ttk.Label(
            footer,
            text=(
                "Running work must finish or safely close before ownership changes. "
                "Closing this window leaves the broker and jobs running."
            ),
            wraplength=1050,
        ).grid(row=0, column=0, columnspan=3, sticky="w")
        self.status = tk.StringVar(value=self.controller.message)
        self.status_label = ttk.Label(footer, textvariable=self.status, wraplength=750)
        self.status_label.grid(row=1, column=0, sticky="w", pady=(9, 0))
        self.refresh_button = ttk.Button(footer, text="Refresh", command=self._refresh)
        self.refresh_button.grid(row=1, column=1, padx=(12, 0), pady=(9, 0))
        self.close_button = ttk.Button(footer, text="Close window", command=self._close)
        self.close_button.grid(row=1, column=2, padx=(8, 0), pady=(9, 0))
        ttk.Label(
            footer, text="Auto-refresh every 2 seconds | TTLs reflect the last broker snapshot"
        ).grid(row=2, column=0, columnspan=3, sticky="w", pady=(4, 0))
        threading.Thread(target=self._worker, name="cardputer-operator", daemon=True).start()
        self._refresh()
        root.after(75, self._poll)
        root.after(2000, self._auto_refresh)

    @staticmethod
    def _table(parent: Any, columns: tuple[tuple[str, str, int], ...], height: int) -> ttk.Treeview:
        from tkinter import ttk

        frame = ttk.Frame(parent)
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)
        tree = ttk.Treeview(
            frame,
            columns=[item[0] for item in columns],
            show="headings",
            selectmode="browse",
            height=height,
        )
        for key, title, width in columns:
            tree.heading(key, text=title)
            tree.column(key, width=width, minwidth=60, stretch=False)
        vertical = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        horizontal = ttk.Scrollbar(frame, orient="horizontal", command=tree.xview)
        tree.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        tree.grid(row=0, column=0, sticky="nsew")
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal.grid(row=1, column=0, sticky="ew")
        return tree

    def _select(self, _event: Any = None) -> None:
        requests, leases = self.queue_tree.selection(), self.owner_tree.selection()
        self.controller.selected_request = requests[0] if requests else ""
        self.controller.selected_lease = leases[0] if leases else ""
        self._controls()

    def _controls(self) -> None:
        model = self.controller
        for button, enabled in (
            (self.cancel_button, model.can_cancel()),
            (self.up_button, model.can_move(-1)),
            (self.down_button, model.can_move(1)),
            (self.release_button, model.can_release()),
            (self.refresh_button, not model.busy and not model.closed),
            (self.diagnostics_button, not model.busy and not model.closed),
            (self.catalog_button, not model.busy and not model.closed),
        ):
            button.configure(state="normal" if enabled else "disabled")
        self.status.set(model.message)
        self.status_label.configure(foreground="#9b1c1c" if model.failed else "")

    def _submit(self, operation: Operation | None) -> None:
        if operation is not None:
            self.commands.put(operation)
        self._controls()

    def _refresh(self) -> None:
        self._submit(self.controller.refresh())
        panel = getattr(self, "timeline_panel", None)
        if panel is not None:
            panel.refresh()

    def _move(self, direction: int) -> None:
        self._submit(self.controller.move(direction))

    def _release(self) -> None:
        model = self.controller
        if not model.can_release() or not model.snapshot or not model.snapshot.owner:
            return
        owner = model.snapshot.owner
        self._submit(model.release(owner.lease_id))

    def _worker(self) -> None:
        while not self.stopped.is_set():
            operation = self.commands.get()
            if operation is None or self.stopped.is_set():
                return
            self.results.put(perform(self.api, operation))

    def _poll(self) -> None:
        if self.stopped.is_set():
            return
        try:
            while True:
                outcome = self.results.get_nowait()
                if self.controller.accept(outcome):
                    self._render()
                    if outcome.diagnostics is not None:
                        self._show_diagnostics(outcome.diagnostics)
                    if outcome.catalog is not None:
                        self._show_catalog(outcome.catalog)
                    if self.stopped.is_set():
                        return
        except queue.Empty:
            pass
        if self.stopped.is_set():
            return
        if not self.stopped.is_set():
            self.root.after(75, self._poll)

    def _show_diagnostics(self, summary: str) -> None:
        from tkinter import messagebox

        messagebox.showinfo("HTTP diagnostics", summary, parent=self.root)

    def _show_catalog(self, summary: str) -> None:
        from tkinter import messagebox

        messagebox.showinfo("MCP info", summary, parent=self.root)

    def _auto_refresh(self) -> None:
        if self.stopped.is_set():
            return
        if not self.controller.busy:
            # The timeline owns its own idle timer. Its explicit Refresh action
            # may retry a failed read-only preview and must not be auto-clicked.
            self._submit(self.controller.refresh())
        self.root.after(2000, self._auto_refresh)

    @staticmethod
    def _replace(
        tree: ttk.Treeview, rows: list[tuple[str, tuple[Any, ...]]], selected: str
    ) -> None:
        horizontal, vertical = tree.xview(), tree.yview()
        children = tree.get_children()
        if children:
            tree.delete(*children)
        for identifier, values in rows:
            tree.insert("", "end", iid=identifier, values=values)
        if selected and tree.exists(selected):
            tree.selection_set(selected)
        if horizontal:
            tree.xview_moveto(horizontal[0])
        if vertical:
            tree.yview_moveto(vertical[0])

    def _render(self) -> None:
        model, snapshot = self.controller, self.controller.snapshot
        if snapshot:
            self.mode.configure(
                text=snapshot.backend, bg="#ffedd5" if snapshot.backend == "SERIAL" else "#cffafe"
            )
            self.health.set(
                f"Historical needs review: {'YES' if snapshot.needs_review else 'no'}    |    "
                f"Ownership uncertain: {'YES' if snapshot.ownership_uncertain else 'no'}"
                + ("    |    STALE STATE: refresh required" if not model.fresh else "")
            )
            self.job.set(f"Active job: {snapshot.active_job or 'none'}")
            owner_rows = []
            if snapshot.owner:
                owner = snapshot.owner
                owner_rows.append(
                    (
                        owner.lease_id,
                        (
                            owner.owner,
                            owner.lease_id,
                            owner.session_id or "legacy",
                            owner.request_id or "--",
                            ttl(owner.lease_remaining_seconds),
                        ),
                    )
                )
            self._replace(self.owner_tree, owner_rows, model.selected_lease)
            self._replace(
                self.session_tree,
                [
                    (
                        row.session_id,
                        (
                            row.session_id,
                            row.owner,
                            row.principal_id,
                            row.scopes,
                            row.state,
                            ttl(row.expires_in_seconds),
                            row.request_id or "--",
                        ),
                    )
                    for row in snapshot.sessions
                ],
                "",
            )
            self._replace(
                self.queue_tree,
                [
                    (
                        row.request_id,
                        (
                            row.position or "--",
                            row.request_id,
                            row.session_id,
                            row.state,
                            ttl(row.wait_remaining_seconds),
                            ttl(row.lease_remaining_seconds),
                            row.lease_id or "--",
                            row.active_job or "--",
                        ),
                    )
                    for row in snapshot.waiters
                ],
                model.selected_request,
            )
        self._controls()

    def _close(self) -> None:
        for name in ("timeline_panel",):
            panel = getattr(self, name, None)
            if panel is not None and not panel.can_close_window():
                return
        panel = getattr(self, "tunnel_panel", None)
        if panel is not None:
            panel.request_close(self._finish_close)
        else:
            self._finish_close()

    def _finish_close(self) -> None:
        panels = [getattr(self, name, None) for name in ("timeline_panel",)]
        if any(panel is not None and not panel.can_close_window() for panel in panels):
            return
        for panel in panels:
            if panel is not None:
                panel.stop()
        self.controller.close()
        self.stopped.set()
        self.commands.put(None)
        self.root.destroy()


def run(
    url: str, token: str, *, broker_config: Path | None = None, project: Path | None = None
) -> None:
    """Open the native GUI; the caller obtains the token from process environment."""
    api = OperatorClient(url, token)
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
    _Window(root, api, panel)
    root.mainloop()
