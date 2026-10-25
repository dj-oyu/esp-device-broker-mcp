"""Optional Tunnel controls for the operator window, without Tk on worker threads.

Only non-secret metadata is persistent. Runtime credentials move directly from a
masked prompt to one worker request and are discarded after that request. Neither
this controller nor the worker automatically connects or restarts a Tunnel.
"""

from __future__ import annotations

import os
import queue
import sys
import threading
import time
import traceback
from collections import deque
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol

from .tunnel import runtime_api_key

if TYPE_CHECKING:
    import tkinter as tk

    from .tunnel import TunnelSettings


REVIEW_WARNING = "Tunnel stop was not confirmed. Keep this window open and review the owned client."
POLL_STALLED = (
    "Tunnel status check did not finish; the GUI still owns the Tunnel client. "
    "Disconnect is still available."
)
WORKER_STOPPED = (
    "The Tunnel background worker stopped unexpectedly; ownership is retained. "
    "Review the owned client and use Disconnect."
)
PANEL_FAILED = (
    "The Tunnel panel hit an internal error; status may be stale. Ownership is unchanged."
)
# A healthy poll is bounded by the 2 s health budget plus, when the client exited
# or cloud auth failed, one 5 s owned-process stop and two process-status reads.
# 15 s leaves twice that margin before declaring the status check stalled.
POLL_RESULT_SECONDS = 15.0
DIAGNOSTIC_FRAMES = 8
DIAGNOSTIC_CHARS = 600
REQUEST_FAILED = "Tunnel request failed. Review the configuration and current status."
CONFIG_FAILED = "Tunnel configuration could not be loaded or saved. Check the broker TOML."
CONFIG_MESSAGES = {
    "tunnel_config_conflict": (
        "Tunnel metadata conflicts with the legacy configuration. Reconcile both files and reload."
    ),
    "tunnel_metadata_incomplete": "Tunnel metadata is incomplete. Fill in the fields and save.",
    "unsafe_tunnel_config_path": "Choose an existing regular broker TOML file without symlinks.",
    "tunnel_config_changed": "Broker TOML changed during saving. Reload before saving again.",
}
STATUS_MESSAGES: dict[str, str] = {
    "tunnel_configuration_changed": "The broker TOML changed. Reload it before connecting.",
    "tunnel_id_placeholder": "Enter your Tunnel ID before connecting.",
    "invalid_private_tunnel_config": "Tunnel metadata is invalid. Review and save the broker TOML.",
    "tunnel_connect_failed": "The owned Tunnel client could not start. Review the client setup.",
    "tunnel_broker_backend_mismatch": "Broker backend does not match the selected TOML.",
    "tunnel_broker_ingress_mismatch": (
        "Broker ingress mode differs or is unknown. Restart the idle broker with this TOML."
    ),
    "tunnel_config_conflict": "Tunnel settings conflict with the broker TOML. Reload it.",
    "tunnel_broker_status_invalid": "The broker returned invalid status; connection was refused.",
    "tunnel_broker_discovery_failed": "Required read-only broker tools could not be discovered.",
    "tunnel_process_status_failed": "Owned client status failed; safe cleanup was attempted.",
    "tunnel_not_configured": "Tunnel not configured",
    "tunnel_configured": "Tunnel metadata loaded; click Connect to start",
    "tunnel_started": "Owned Tunnel client started; checking readiness",
    "tunnel_starting": "Owned Tunnel client is waiting for local readiness",
    "tunnel_local_ready": "Local client ready; cloud polling is not yet confirmed",
    "tunnel_cloud_poll_observed": (
        "Recent cloud polling succeeded; application discovery is unknown"
    ),
    "tunnel_client_exited": "Owned Tunnel client exited; manual connection is required",
    "tunnel_disconnected": "Owned Tunnel disconnected; broker and jobs continue",
    "tunnel_closed": "Owned Tunnel disconnected; broker and jobs continue",
    "tunnel_stop_unconfirmed": REVIEW_WARNING,
    "tunnel_cloud_auth_failed": "Cloud authentication failed. Enter a new key to connect again.",
    "tunnel_broker_origin_rejected": (
        "Remote MCP authentication required; local operator keys are refused."
    ),
    "tunnel_broker_auth_failed": "Remote MCP authentication failed. Check the remote key.",
    "tunnel_broker_scope_rejected": "The remote MCP key needs read permission.",
    "tunnel_launcher_already_running": (
        "Another Tunnel launcher owns the client lock. Stop it separately."
    ),
    "tunnel_health_listener_exists": (
        "The health port is already in use. Review other local clients."
    ),
    "client_release_verification_failed": (
        "Official client verification failed. Check the pinned ZIP and EXE."
    ),
    "tunnel_windows_required": "The official owned Tunnel client requires Windows.",
    "tunnel_broker_url_mismatch": "The selected TOML does not match this window's broker URL.",
    "serial_tunnel_requires_explicit_enable_serial": (
        "Enable the explicit SERIAL option before connecting."
    ),
    "health_port_conflicts_with_broker": "Choose a health port different from the broker port.",
    "invalid_runtime_key": "The Tunnel API key is invalid. Enter it again.",
    "invalid_mcp_bearer": "The remote MCP key format is invalid. Enter it again.",
    "tunnel_health_unavailable": (
        "Owned client health could not be read; app discovery remains unknown."
    ),
    "tunnel_readiness_unconfirmed": (
        "Local readiness was not confirmed; review the owned client setup."
    ),
    "tunnel_cloud_poll_degraded": "Cloud polling is degraded; no automatic restart will occur.",
    "tunnel_cloud_poll_stale": "Recent cloud polling is not confirmed.",
    "tunnel_health_stale": "Client health evidence is stale; app discovery remains unknown.",
    "tunnel_health_schema_unsupported": (
        "Client health schema was not recognized; evidence remains unknown."
    ),
}
SUCCESS_CODES = {
    "tunnel_not_configured",
    "tunnel_configured",
    "tunnel_started",
    "tunnel_starting",
    "tunnel_local_ready",
    "tunnel_cloud_poll_observed",
    "tunnel_disconnected",
    "tunnel_closed",
}
Action = Literal["load", "save", "connect", "poll", "disconnect", "restart_stop", "close"]


def runtime_remote_key(supplied: str, environment: Mapping[str, str]) -> str:
    """Only an explicit remote key or the launcher's remote runtime credential."""
    return supplied or environment.get("CARDPUTER_REMOTE_TOKEN", "")


class TunnelAPI(Protocol):
    def configure(
        self, settings: TunnelSettings, broker_url: str, *, enable_serial: bool = False
    ) -> dict[str, Any]: ...
    def connect(self, api_key: str, bearer: str) -> dict[str, Any]: ...
    def poll(self) -> dict[str, Any]: ...
    def disconnect(self) -> dict[str, Any]: ...
    def close(self) -> bool: ...
    def snapshot(self) -> dict[str, Any]: ...


@dataclass(frozen=True)
class TunnelView:
    """Allowlisted display data. No native output, arbitrary messages, or secrets."""

    configured: bool = False
    owned: bool = False
    process_alive: bool = False
    local_ready: bool | None = None
    cloud_poll_success: bool | None = None
    application_verified: bool | None = None
    exit_code: int | None = None
    needs_review: bool = False
    state: str = "not_configured"
    status_code: str = "tunnel_not_configured"

    @classmethod
    def parse(cls, data: dict[str, Any]) -> TunnelView:
        def flag(key: str, default: bool = False) -> bool:
            value = data.get(key, default)
            if not isinstance(value, bool):
                raise ValueError("invalid_tunnel_snapshot")
            return value

        def evidence(key: str) -> bool | None:
            value = data.get(key)
            if value is not None and not isinstance(value, bool):
                raise ValueError("invalid_tunnel_snapshot")
            return value

        state = data.get("state", "not_configured")
        if state not in {
            "not_configured",
            "disconnected",
            "connecting",
            "connected",
            "stopping",
            "exited",
            "failed",
            "needs_review",
            "running",
            "disconnecting",
            "stop_uncertain",
            "closed",
        }:
            state = "failed"
        code = data.get("exit_code")
        if code is not None and (
            isinstance(code, bool) or not isinstance(code, int) or not -(2**31) <= code <= 2**32 - 1
        ):
            code = None
        status_code = data.get("status_code", "tunnel_not_configured")
        if status_code not in STATUS_MESSAGES:
            status_code = "tunnel_request_failed"
        return cls(
            configured=flag("configured"),
            owned=flag("owned"),
            process_alive=flag("process_alive"),
            local_ready=evidence("local_readiness"),
            cloud_poll_success=True if evidence("cloud_poll_observed") else None,
            application_verified=True if evidence("app_verified") else None,
            exit_code=code,
            needs_review=flag("ownership_uncertain"),
            state=state,
            status_code=status_code,
        )


@dataclass
class TunnelOperation:
    sequence: int
    action: Action
    path: Path | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    enable_serial: bool = False
    api_key: str = field(default="", repr=False)
    bearer: str = field(default="", repr=False)

    def take_credentials(self) -> tuple[str, str]:
        key, bearer = self.api_key, self.bearer
        self.api_key = self.bearer = ""
        return key, bearer


@dataclass(frozen=True)
class TunnelOutcome:
    sequence: int
    action: Action
    view: TunnelView | None
    message: str
    failed: bool = False
    path: Path | None = None
    metadata: dict[str, Any] | None = None
    close_confirmed: bool = False


class TunnelController:
    """Headless repeated-click, stale-result, shutdown, and restart policy."""

    def __init__(self, *, monotonic: Callable[[], float] = time.monotonic) -> None:
        self._monotonic = monotonic
        self.started = 0.0
        # Sequence of a poll abandoned at its deadline whose worker has not returned.
        self.stalled_poll: int | None = None
        self.view = TunnelView()
        self.path: Path | None = None
        self.metadata: dict[str, Any] = {}
        self.sequence = 0
        self.busy = False
        self.action: Action | None = None
        self.closed = False
        self.closing = False
        self.dirty = False
        self.failed = False
        self.message = "Tunnel not configured"
        self.prompt_after_stop = False
        self.stop_unconfirmed = False

    def can_edit(self) -> bool:
        return not (
            self.closed
            or self.closing
            or self.busy
            or self.view.owned
            or self.view.process_alive
            or self.view.needs_review
            or self.stop_unconfirmed
        )

    def can_connect(self) -> bool:
        return self.can_edit() and self.view.configured and not self.dirty

    def can_disconnect(self) -> bool:
        return not (self.closed or self.closing or self.busy) and (
            self.view.owned
            or self.view.process_alive
            or self.view.needs_review
            or self.stop_unconfirmed
        )

    def _begin(self, action: Action, **kwargs: Any) -> TunnelOperation | None:
        if self.busy or self.closed or (self.closing and action != "close"):
            return None
        self.sequence += 1
        self.busy, self.action = True, action
        self.started = self._monotonic()
        self.failed = False
        self.message = {
            "load": "Loading Tunnel metadata...",
            "save": "Saving Tunnel metadata...",
            "connect": "Connecting Tunnel...",
            "poll": "Checking Tunnel status...",
            "disconnect": "Disconnecting owned Tunnel...",
            "restart_stop": "Stopping owned Tunnel before a manual restart...",
            "close": "Disconnecting owned Tunnel before closing...",
        }[action]
        return TunnelOperation(self.sequence, action, **kwargs)

    def load(self, path: Path, *, enable_serial: bool = False) -> TunnelOperation | None:
        if not self.can_edit():
            return None
        return self._begin("load", path=path, enable_serial=enable_serial)

    def save(
        self, metadata: dict[str, Any], *, enable_serial: bool = False
    ) -> TunnelOperation | None:
        if not self.can_edit() or self.path is None:
            return None
        return self._begin("save", path=self.path, metadata=metadata, enable_serial=enable_serial)

    def connect(self, api_key: str, remote_key: str) -> TunnelOperation | None:
        if not self.can_connect():
            return None
        if not api_key or not remote_key:
            self.failed = True
            self.message = "Enter a Tunnel API key and a remote MCP key."
            return None
        bearer = remote_key if remote_key.startswith("Bearer ") else "Bearer " + remote_key
        return self._begin("connect", api_key=api_key, bearer=bearer)

    def poll(self) -> TunnelOperation | None:
        if not (self.view.owned or self.view.process_alive) or self.closing:
            return None
        if self.stalled_poll is not None:
            return None  # Never queue another probe behind a stalled one.
        return self._begin("poll")

    def expire(self) -> bool:
        """Give up waiting for one overdue poll; other actions keep their semantics.

        Only the poll's busy state is cleared. Ownership, view, and the worker are
        untouched, so Disconnect/close stay available. A late result for the
        abandoned sequence is dropped by accept().
        """
        if not self.busy or self.action != "poll":
            return False
        if self._monotonic() - self.started < POLL_RESULT_SECONDS:
            return False
        self.stalled_poll = self.sequence
        self.busy, self.action = False, None
        self.failed, self.message = True, POLL_STALLED
        return True

    def disconnect(self) -> TunnelOperation | None:
        return self._begin("disconnect") if self.can_disconnect() else None

    def restart(self) -> TunnelOperation | None:
        # The outcome requests a NEW prompt only after a confirmed stop.
        return self._begin("restart_stop") if self.can_disconnect() else None

    def needs_close_confirmation(self) -> bool:
        return bool(
            self.view.owned
            or self.view.process_alive
            or self.view.needs_review
            or self.stop_unconfirmed
            or (self.busy and self.action == "connect")
        )

    def request_close(self) -> TunnelOperation | None:
        self.closing = True
        self.prompt_after_stop = False
        return self._begin("close")

    def pending_close(self) -> TunnelOperation | None:
        return self._begin("close") if self.closing and not self.busy else None

    def accept(self, result: TunnelOutcome) -> bool:
        if result.action == "poll" and result.sequence == self.stalled_poll:
            # The abandoned probe finally returned; its evidence is stale.
            self.stalled_poll = None
            return False
        if (
            self.closed
            or not self.busy
            or result.sequence != self.sequence
            or result.action != self.action
        ):
            return False
        self.busy, self.action = False, None
        self.failed, self.message = result.failed, result.message
        if result.failed and result.action == "connect" and result.view is None:
            self.stop_unconfirmed = True
        if result.failed and result.action in ("load", "save"):
            self.dirty = True
        self.prompt_after_stop = False
        if result.view is not None:
            self.view = result.view
        if result.path is not None:
            self.path = result.path
        if result.metadata is not None:
            self.metadata = result.metadata
            self.dirty = result.failed
        if result.action in ("disconnect", "restart_stop", "close"):
            stopped = (
                result.view is not None
                and not (self.view.owned or self.view.process_alive or self.view.needs_review)
                and not result.failed
            )
            if result.action == "close":
                stopped = stopped and result.close_confirmed
                self.closing = False
                self.closed = stopped
            self.stop_unconfirmed = not stopped
            if not stopped:
                self.failed, self.message = True, REVIEW_WARNING
            elif result.action == "restart_stop" and not self.closing:
                self.prompt_after_stop = True
        return True

    def abandon(self) -> None:
        self.closed = True
        self.sequence += 1
        self.prompt_after_stop = False


def _display_metadata(settings: Any) -> dict[str, Any]:
    return {
        "tunnel_id": settings.tunnel_id or "",
        "client_exe": str(settings.client_exe or ""),
        "client_archive": str(settings.client_archive or ""),
        "health_port": settings.health_port or 8766,
        "ingress_enabled": settings.ingress_enabled,
    }


def perform_tunnel(
    manager: TunnelAPI, operation: TunnelOperation, broker_url: str, project: Path
) -> TunnelOutcome:
    """All file, network, native-process, and manager calls run off the Tk thread."""
    key = bearer = ""
    try:
        metadata = None
        close_confirmed = False
        if operation.action in ("load", "save"):
            from .config import load_settings
            from .tunnel import load_gui_config, save_gui_config

            assert operation.path is not None
            if operation.action == "save":
                save_gui_config(operation.path, project, operation.metadata)
            settings = load_gui_config(operation.path, project)
            config_path = (
                operation.path if operation.path.is_absolute() else project / operation.path
            )
            canonical = load_settings(config_path).tunnel
            metadata = _display_metadata(canonical) if canonical is not None else {}
            if settings is not None:
                metadata = _display_metadata(settings)
                if canonical is not None:
                    metadata.update(
                        {
                            key: value
                            for key, value in canonical.model_dump(
                                mode="json", exclude_unset=True, exclude_none=True
                            ).items()
                            if value != ""
                        }
                    )
                data = manager.configure(
                    settings, broker_url, enable_serial=operation.enable_serial
                )
            else:
                data = {"state": "not_configured", "status_code": "tunnel_not_configured"}
            message = (
                "Tunnel metadata saved" if operation.action == "save" else "Tunnel metadata loaded"
            )
            if settings is None:
                message = "Tunnel not configured. Complete the metadata and save."
        elif operation.action == "connect":
            key, bearer = operation.take_credentials()
            data = manager.connect(key, bearer)
            message = "Tunnel connection checked; application discovery remains separately verified"
        elif operation.action == "poll":
            data = manager.poll()
            message = "Tunnel status refreshed"
        elif operation.action in ("disconnect", "restart_stop"):
            data = manager.disconnect()
            message = "Owned Tunnel disconnected; broker and jobs continue"
        else:
            close_confirmed = manager.close()
            data = manager.snapshot()
            message = "Owned Tunnel disconnected; broker and jobs continue"
        view = TunnelView.parse(data)
        failed = view.needs_review or view.status_code not in SUCCESS_CODES
        if operation.action not in ("load", "save") or failed:
            message = STATUS_MESSAGES.get(view.status_code, REQUEST_FAILED)
        if view.needs_review:
            message = REVIEW_WARNING
        return TunnelOutcome(
            operation.sequence,
            operation.action,
            view,
            message,
            failed,
            operation.path,
            metadata,
            close_confirmed,
        )
    except Exception as error:
        # Never echo exceptions: a manager, OS, HTTP library, or parser may
        # include credentials, native output, or private file contents.
        view = None
        try:
            view = TunnelView.parse(manager.snapshot())
        except Exception:
            pass
        message = CONFIG_FAILED if operation.action in ("load", "save") else REQUEST_FAILED
        if operation.action in ("load", "save"):
            from .tunnel import TunnelError

            if isinstance(error, TunnelError):
                message = CONFIG_MESSAGES.get(str(error), CONFIG_FAILED)
        return TunnelOutcome(operation.sequence, operation.action, view, message, True)
    finally:
        operation.api_key = operation.bearer = ""
        key = bearer = ""


def _lost_outcome(manager: TunnelAPI, operation: TunnelOperation) -> TunnelOutcome:
    """A worker-side failure that escaped perform_tunnel; never echo it."""
    operation.take_credentials()
    view = None
    if operation.action not in ("connect", "disconnect", "restart_stop", "close"):
        try:
            view = TunnelView.parse(manager.snapshot())
        except Exception:
            pass
    return TunnelOutcome(operation.sequence, operation.action, view, REQUEST_FAILED, True)


def _frames(frames: Iterable[traceback.FrameSummary]) -> str:
    """File basenames, function names, and line numbers only: no paths or values."""
    return " > ".join(
        f"{Path(frame.filename).name}:{frame.name}:{frame.lineno}"
        for frame in list(frames)[-DIAGNOSTIC_FRAMES:]
    )


def thread_stack(thread: threading.Thread) -> str:
    frame = sys._current_frames().get(thread.ident) if thread.ident is not None else None
    return _frames(traceback.extract_stack(frame)) if frame is not None else "no stack"


class TunnelWorker:
    """Control actions and status polls on separate threads.

    A stalled status probe therefore cannot delay Disconnect/close, whose
    manager calls only contend for the manager's short critical sections.
    """

    def __init__(self, manager: TunnelAPI, broker_url: str, project: Path) -> None:
        self.manager, self.broker_url, self.project = manager, broker_url, project
        self.commands: queue.Queue[TunnelOperation | None] = queue.Queue()
        self.poll_commands: queue.Queue[TunnelOperation | None] = queue.Queue()
        self.results: queue.Queue[TunnelOutcome] = queue.Queue()
        self.stopped = threading.Event()
        self.thread = self._start(self.commands, "cardputer-tunnel")
        self.poll_thread = self._start(self.poll_commands, "cardputer-tunnel-poll")

    def _start(self, commands: queue.Queue[TunnelOperation | None], name: str) -> threading.Thread:
        thread = threading.Thread(target=self.run, args=(commands,), name=name, daemon=True)
        thread.start()
        return thread

    def thread_for(self, action: Action) -> threading.Thread:
        return self.poll_thread if action == "poll" else self.thread

    def submit(self, operation: TunnelOperation) -> None:
        if self.stopped.is_set():
            operation.take_credentials()
            return
        if operation.action == "poll":
            if not self.poll_thread.is_alive():
                self.poll_commands = self._discard(self.poll_commands)
                self.poll_thread = self._start(self.poll_commands, "cardputer-tunnel-poll")
            self.poll_commands.put(operation)
        else:
            if not self.thread.is_alive():
                self.commands = self._discard(self.commands)
                self.thread = self._start(self.commands, "cardputer-tunnel")
            self.commands.put(operation)

    @staticmethod
    def _discard(
        commands: queue.Queue[TunnelOperation | None],
    ) -> queue.Queue[TunnelOperation | None]:
        # A dead thread's leftovers are stale; never replay them (or their keys).
        while True:
            try:
                stale = commands.get_nowait()
            except queue.Empty:
                return queue.Queue()
            if stale is not None:
                stale.take_credentials()

    def run(self, commands: queue.Queue[TunnelOperation | None] | None = None) -> None:
        commands = self.commands if commands is None else commands
        while not self.stopped.is_set():
            operation = commands.get()
            if operation is None:
                return
            if self.stopped.is_set():
                operation.take_credentials()
                return
            try:
                result = perform_tunnel(self.manager, operation, self.broker_url, self.project)
            except BaseException:
                # perform_tunnel handles Exception; anything else must still
                # produce a result instead of silently ending this thread.
                result = _lost_outcome(self.manager, operation)
            self.results.put(result)
            del operation

    def stop(self) -> None:
        self.stopped.set()
        self.commands.put(None)
        self.poll_commands.put(None)


class TunnelPanel:
    """An integrated dialog, owned by the main window; no extra native console."""

    def __init__(
        self,
        root: tk.Tk,
        broker_url: str,
        *,
        broker_config: Path | None = None,
        project: Path | None = None,
        manager: TunnelAPI | None = None,
    ) -> None:
        if manager is None:
            from .tunnel_manager import TunnelManager

            manager = TunnelManager()
        self.root = root
        self.controller = TunnelController()
        self.worker = TunnelWorker(manager, broker_url, project or Path.cwd())
        self.dialog: tk.Toplevel | None = None
        self.prompt: tk.Toplevel | None = None
        self.on_closed: Callable[[], None] | None = None
        self.rendered_metadata: dict[str, Any] | None = None
        self.updating = False
        # Bounded, path-free records for support; never exception text or values.
        self.diagnostics: deque[str] = deque(maxlen=8)
        self.diagnostic_var: Any = None
        if broker_config is not None:
            self._submit(self.controller.load(broker_config))
        root.after(75, self._drain)
        root.after(2000, self._poll)

    def _submit(self, operation: TunnelOperation | None) -> None:
        if operation is not None:
            self.worker.submit(operation)
        self._render()

    def _record(self, kind: str, detail: str) -> None:
        text = f"{kind}: {detail}"[:DIAGNOSTIC_CHARS]
        self.diagnostics.append(text)
        if self.diagnostic_var is not None:
            self.diagnostic_var.set("Diagnostic (no secrets): " + text)

    def _callback_failed(self, error: BaseException) -> None:
        try:
            self._record(type(error).__name__, _frames(traceback.extract_tb(error.__traceback__)))
            self.controller.failed, self.controller.message = True, PANEL_FAILED
            self._render()
        except Exception:
            pass

    def show(self) -> None:
        import tkinter as tk
        from tkinter import ttk

        if self.dialog is not None:
            self.dialog.deiconify()
            self.dialog.lift()
            return
        self.dialog = dialog = tk.Toplevel(self.root)
        dialog.title("Cardputer Tunnel")
        dialog.transient(self.root)
        dialog.geometry("760x630")
        dialog.minsize(650, 610)
        dialog.protocol("WM_DELETE_WINDOW", dialog.withdraw)
        outer = ttk.Frame(dialog, padding=16)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(1, weight=1)
        ttk.Label(outer, text="Tunnel", font=("TkDefaultFont", 16, "bold")).grid(
            row=0, column=0, columnspan=3, sticky="w"
        )
        self.path_var = tk.StringVar()
        ttk.Label(outer, textvariable=self.path_var, wraplength=575).grid(
            row=1, column=0, columnspan=2, sticky="w", pady=8
        )
        self.load_button = ttk.Button(outer, text="Load broker TOML...", command=self._load)
        self.load_button.grid(row=1, column=2, padx=(8, 0))
        self.fields: dict[str, tk.StringVar] = {}
        self.entries: list[Any] = []
        self.file_buttons: list[Any] = []
        for row, (key, label) in enumerate(
            (
                ("tunnel_id", "Tunnel ID"),
                ("client_exe", "Official client EXE"),
                ("client_archive", "Pinned client ZIP"),
                ("health_port", "Local health port"),
            ),
            start=2,
        ):
            ttk.Label(outer, text=label).grid(row=row, column=0, sticky="w", pady=4)
            var = self.fields[key] = tk.StringVar()
            var.trace_add("write", self._changed)
            entry = ttk.Entry(outer, textvariable=var)
            entry.grid(row=row, column=1, columnspan=1, sticky="ew", pady=4)
            self.entries.append(entry)
            if key in ("client_exe", "client_archive"):
                button = ttk.Button(outer, text="Choose...", command=partial(self._choose, key))
                button.grid(row=row, column=2, padx=(8, 0))
                self.file_buttons.append(button)
        self.serial = tk.BooleanVar(value=False)
        self.serial.trace_add("write", self._changed)
        self.serial_button = ttk.Checkbutton(
            outer,
            text="Allow Tunnel for a configured SERIAL broker (this session)",
            variable=self.serial,
        )
        self.serial_button.grid(row=6, column=0, columnspan=3, sticky="w", pady=(8, 2))
        self.save_button = ttk.Button(outer, text="Save metadata", command=self._save)
        self.save_button.grid(row=7, column=2, sticky="e", pady=5)
        ttk.Label(
            outer,
            text="Save updates [tunnel] only; TOML formatting/comments may change.\n"
            "Keys are runtime-only and are never saved. Saving does not connect.",
            wraplength=520,
        ).grid(row=7, column=0, columnspan=2, sticky="w")
        bar = ttk.Frame(outer)
        bar.grid(row=8, column=0, columnspan=3, sticky="ew", pady=(14, 8))
        self.connect_button = ttk.Button(bar, text="Connect...", command=self._credentials)
        self.connect_button.pack(side="left")
        self.disconnect_button = ttk.Button(
            bar,
            text="Disconnect (jobs continue)",
            command=lambda: self._submit(self.controller.disconnect()),
        )
        self.disconnect_button.pack(side="left", padx=8)
        self.restart_button = ttk.Button(
            bar,
            text="Manual restart (jobs continue)...",
            command=lambda: self._submit(self.controller.restart()),
        )
        self.restart_button.pack(side="left")
        self.status_var = tk.StringVar()
        self.evidence_var = tk.StringVar()
        self.status_label = ttk.Label(outer, textvariable=self.status_var, wraplength=700)
        self.status_label.grid(row=9, column=0, columnspan=3, sticky="w", pady=6)
        ttk.Label(outer, textvariable=self.evidence_var, justify="left").grid(
            row=10, column=0, columnspan=3, sticky="w", pady=6
        )
        ttk.Label(
            outer,
            text="Only this window's Tunnel client is stopped. The broker and jobs continue.\n"
            "Other clients are not monitored. Closing this panel hides it; monitoring continues.\n"
            "No automatic connection or restart.",
            wraplength=700,
        ).grid(row=11, column=0, columnspan=3, sticky="w", pady=(8, 0))
        self.ingress_var = tk.StringVar()
        ttk.Label(outer, textvariable=self.ingress_var, wraplength=700).grid(
            row=12, column=0, columnspan=3, sticky="w", pady=(8, 0)
        )
        self.diagnostic_var = tk.StringVar(
            value="Diagnostic (no secrets): " + self.diagnostics[-1] if self.diagnostics else ""
        )
        ttk.Label(outer, textvariable=self.diagnostic_var, wraplength=700).grid(
            row=13, column=0, columnspan=3, sticky="w", pady=(8, 0)
        )
        self.rendered_metadata = None
        self._render()

    def _changed(self, *_: str) -> None:
        if not self.updating:
            self.controller.dirty = True
            self._render()

    def _choose(self, key: str) -> None:
        from tkinter import filedialog

        selected = filedialog.askopenfilename(parent=self.dialog)
        if selected and self.controller.can_edit():
            self.fields[key].set(selected)

    def _load(self) -> None:
        from tkinter import filedialog

        if not self.controller.can_edit():
            return
        selected = filedialog.askopenfilename(
            parent=self.dialog, title="Choose broker configuration", filetypes=[("TOML", "*.toml")]
        )
        if selected:
            self._submit(self.controller.load(Path(selected), enable_serial=self.serial.get()))

    def _save(self) -> None:
        metadata: dict[str, Any] = {key: value.get() for key, value in self.fields.items()}
        try:
            metadata["health_port"] = int(metadata["health_port"])
        except ValueError:
            self.controller.failed, self.controller.message = True, CONFIG_FAILED
            self._render()
            return
        metadata = {key: value for key, value in metadata.items() if value != ""}
        self._submit(self.controller.save(metadata, enable_serial=self.serial.get()))

    def _credentials(self) -> None:
        import tkinter as tk
        from tkinter import ttk

        if not self.controller.can_connect() or self.prompt is not None:
            return
        self.show()
        self.prompt = prompt = tk.Toplevel(self.root)
        prompt.title("Connect Tunnel")
        prompt.transient(self.dialog)
        prompt.resizable(False, False)
        box = ttk.Frame(prompt, padding=16)
        box.pack(fill="both", expand=True)
        api = tk.StringVar()
        remote = tk.StringVar()
        ttk.Label(box, text="Tunnel API key (optional if environment is set)").grid(
            row=0, column=0, sticky="w"
        )
        api_entry = ttk.Entry(box, textvariable=api, show="*", width=58)
        api_entry.grid(row=1, column=0, pady=(4, 10))
        ttk.Label(box, text="Issued remote MCP key (optional if runtime remote key is set)").grid(
            row=2, column=0, sticky="w"
        )
        ttk.Entry(box, textvariable=remote, show="*", width=58).grid(row=3, column=0, pady=4)
        ttk.Label(
            box,
            text="Blank uses CARDPUTER_REMOTE_TOKEN. Local operator keys are never used.\n"
            "Blank API key uses CARDPUTER_TUNNEL_API_KEY, then CONTROL_PLANE_API_KEY.",
        ).grid(row=4, column=0, sticky="w", pady=8)

        def clear_and_close() -> None:
            api.set("")
            remote.set("")
            self.prompt = None
            prompt.destroy()

        def connect_once() -> None:
            key = runtime_api_key(api.get(), os.environ)
            remote_key = runtime_remote_key(remote.get(), os.environ)
            operation = self.controller.connect(key, remote_key)
            if operation is not None:
                self.worker.submit(operation)
            # Clear even invalid input immediately; never retain keys for a retry.
            key = remote_key = ""
            clear_and_close()
            self._render()

        actions = ttk.Frame(box)
        actions.grid(row=5, column=0, sticky="e")
        ttk.Button(actions, text="Cancel", command=clear_and_close).pack(side="left", padx=8)
        ttk.Button(actions, text="Connect", command=connect_once).pack(side="left")
        prompt.protocol("WM_DELETE_WINDOW", clear_and_close)
        prompt.grab_set()
        api_entry.focus_set()

    @staticmethod
    def _evidence(value: bool | None) -> str:
        return "yes" if value is True else "no" if value is False else "unknown"

    def _render(self) -> None:
        if self.dialog is None:
            return
        model, view = self.controller, self.controller.view
        if model.metadata != self.rendered_metadata:
            self.updating = True
            for key, var in self.fields.items():
                var.set(str(model.metadata.get(key, 8766 if key == "health_port" else "")))
            self.updating = False
            self.rendered_metadata = dict(model.metadata)
        self.path_var.set(str(model.path) if model.path else "Broker TOML: not configured")
        ingress = "enabled" if model.metadata.get("ingress_enabled") is True else "disabled"
        self.ingress_var.set(
            f"Tunnel ingress: {ingress} (read-only). Change [tunnel] ingress_enabled manually, "
            "then restart the idle broker and reload this TOML."
        )
        enabled = "normal" if model.can_edit() else "disabled"
        for widget in [self.load_button, self.serial_button, *self.entries, *self.file_buttons]:
            widget.configure(state=enabled)
        self.save_button.configure(state=enabled if model.path is not None else "disabled")
        self.connect_button.configure(state="normal" if model.can_connect() else "disabled")
        for button in (self.disconnect_button, self.restart_button):
            button.configure(state="normal" if model.can_disconnect() else "disabled")
        self.status_var.set(model.message + (" | Unsaved metadata" if model.dirty else ""))
        self.status_label.configure(foreground="#9b1c1c" if model.failed else "")
        self.evidence_var.set(
            f"Owned Tunnel client alive: {self._evidence(view.process_alive)}\n"
            f"Local readiness: {self._evidence(view.local_ready)}\n"
            f"Cloud polling success: {self._evidence(view.cloud_poll_success)}\n"
            f"Application discovery verified: {self._evidence(view.application_verified)}\n"
            f"Last safe exit code: {view.exit_code if view.exit_code is not None else '--'}"
        )

    def _drain(self) -> None:
        if self.worker.stopped.is_set():
            return
        try:
            self._drain_results()
            if not self.worker.stopped.is_set():
                self._check_progress()
        except Exception as error:
            self._callback_failed(error)
        finally:
            # Reschedule unconditionally: one failed callback must never freeze the panel.
            if not self.worker.stopped.is_set():
                self._after(75, self._drain)

    def _drain_results(self) -> None:
        while not self.worker.stopped.is_set():
            try:
                result = self.worker.results.get_nowait()
            except queue.Empty:
                return
            if self.controller.accept(result):
                self._accepted()

    def _accepted(self) -> None:
        self._render()
        if self.controller.closed:
            self.worker.stop()
            callback, self.on_closed = self.on_closed, None
            if callback is not None:
                callback()
            return
        if self.controller.closing:
            self._submit(self.controller.pending_close())
        elif self.controller.prompt_after_stop:
            self.controller.prompt_after_stop = False
            self._credentials()
        elif self.controller.stop_unconfirmed:
            self.on_closed = None
            self.show()

    def _check_progress(self) -> None:
        model = self.controller
        if model.busy and model.action is not None:
            thread = self.worker.thread_for(model.action)
            if not thread.is_alive():
                # The result can never arrive. Fail closed: a lost connect/stop
                # keeps ownership uncertain exactly like a failed result.
                self._record("tunnel_worker_stopped", model.action)
                if model.accept(
                    TunnelOutcome(model.sequence, model.action, None, WORKER_STOPPED, True)
                ):
                    self._accepted()
                return
            if model.expire():
                self._record("tunnel_poll_deadline", thread_stack(thread))
                self._render()
        if model.stalled_poll is not None and not self.worker.poll_thread.is_alive():
            model.stalled_poll = None  # The next poll starts a fresh poll thread.

    def _after(self, delay: int, callback: Callable[[], None]) -> None:
        try:
            self.root.after(delay, callback)
        except Exception as error:
            self._callback_failed(error)

    def _poll(self) -> None:
        if self.worker.stopped.is_set():
            return
        try:
            self._submit(self.controller.poll())
        except Exception as error:
            self._callback_failed(error)
        finally:
            if not self.worker.stopped.is_set():
                self._after(2000, self._poll)

    def request_close(self, callback: Callable[[], None]) -> None:
        from tkinter import messagebox

        if self.controller.closing:
            return
        if not self.controller.needs_close_confirmation():
            self.controller.abandon()
            self.worker.stop()
            callback()
            return
        if not messagebox.askyesno(
            "Disconnect Tunnel and close?",
            "Disconnect this window's owned Tunnel client and close the window?\n\n"
            "The broker and its jobs will continue running.",
            parent=self.root,
            default="no",
        ):
            return
        self.on_closed = callback
        self._submit(self.controller.request_close())
