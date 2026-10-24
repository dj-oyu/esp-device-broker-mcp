"""Tk adapter for the live projection. Workers own IO; Tk owns all widgets."""

import base64
import queue
import threading
import time
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any, Literal

from .gui import Outcome, QueueController, http_diagnostics_summary, mcp_catalog_summary, perform
from .gui_connection_panels import DiagnosticOutcome, DiagnosticPanels
from .gui_images import SOURCES
from .gui_job_status import LABELS, configure_job_status_tags
from .gui_live_presentation import (
    LogRequestState,
    LogView,
    logs_applicable,
    read_log_result,
    replace_text,
    sync_rows,
)
from .gui_live_preview import FAILED, PreviewState
from .gui_live_retry import LiveRetry, diagnostic
from .gui_live_state import LiveState, apply, begin, close, disconnect, select
from .gui_live_transport import (
    AUTH_FAILED,
    DELIVERY_GAP,
    INVALID_RESPONSE,
    REJECTED,
    UNREACHABLE,
    LiveRunner,
)
from .gui_theme import apply_theme
from .gui_timeline import TimelineOperation, perform_timeline
from .job_failure_reason import REASON_LABELS
from .models import FLASH_TESTS, TestName
from .operator_client import OperatorClient


class LiveWindow:
    def __init__(self, root: Any, api: OperatorClient, tunnel_panel: Any = None) -> None:
        import tkinter as tk
        from tkinter import ttk

        self.root, self.api, self.tunnel_panel = root, api, tunnel_panel
        self.state = begin(LiveState())
        self.logs = LogView()
        self.preview_state = PreviewState()
        self.preview_photo: Any = None
        self.displayed_preview: Any = None
        self.live_retry = LiveRetry()
        self.live_diagnostics: list[dict[str, Any]] = []
        self.log_requests = LogRequestState()
        self.close_prompt = False
        self.queue_controller = QueueController()
        self.action_pending = False
        self.action_results: queue.Queue[tuple[str, Any]] = queue.Queue(maxsize=2)
        self.events: list[str] = []
        self.timer: Any = None
        root.title("Cardputer")
        root.geometry("1280x820")
        root.minsize(860, 600)
        root.protocol("WM_DELETE_WINDOW", self._close)
        root.columnconfigure(0, weight=1)
        root.rowconfigure(1, weight=1)
        apply_theme(root)
        header = ttk.Frame(root, padding=(16, 12, 16, 8))
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(1, weight=1)
        ttk.Label(header, text="Cardputer", style="App.TLabel").grid(row=0, column=0)
        self.status = tk.StringVar(value="Connecting...")
        self.action_status = tk.StringVar(value="")
        ttk.Label(header, textvariable=self.status).grid(row=0, column=1, sticky="e", padx=12)
        ttk.Button(header, text="Reconnect", command=self._reconnect).grid(row=0, column=2)
        notebook = ttk.Notebook(root)
        self.tabs = notebook
        notebook.grid(row=1, column=0, sticky="nsew", padx=16, pady=(0, 12))
        ttk.Label(root, textvariable=self.action_status, wraplength=1000).grid(
            row=2, column=0, sticky="ew", padx=16, pady=(0, 8)
        )
        jobs, events, connection = (ttk.Frame(notebook, padding=12) for _ in range(3))
        for frame, name in ((jobs, "Jobs"), (events, "Events"), (connection, "Connection")):
            notebook.add(frame, text=name)
        self._job_pages: Any = None
        self._detail_canvas: Any = None
        self._detail_page: Any = None
        self.open_selected_button: Any = None
        self._suppress_next_detail_focus = False
        jobs.columnconfigure(0, weight=1)
        jobs.rowconfigure(0, weight=1)
        self.job_panes = ttk.Panedwindow(jobs, orient="horizontal")
        self.job_panes.grid(row=0, column=0, sticky="nsew")
        list_parent = ttk.Frame(self.job_panes)
        detail_host = ttk.Frame(self.job_panes, padding=(12, 0, 0, 0))
        self.job_panes.add(list_parent, weight=3)
        self.job_panes.add(detail_host, weight=2)
        self._panes_initialized = False
        self.job_panes.bind("<Configure>", self._initialize_panes)
        list_parent.columnconfigure(0, weight=1)
        list_parent.rowconfigure(1, weight=1)
        detail = self._scrollable_detail(detail_host)
        ttk.Label(list_parent, text="Recent jobs", style="Section.TLabel").grid(
            row=0, column=0, sticky="w", pady=(0, 8)
        )
        self.history_status = tk.StringVar(value="Waiting for a consistent snapshot")
        ttk.Label(list_parent, textvariable=self.history_status).grid(
            row=2, column=0, sticky="w", pady=8
        )
        job_box, self.job_tree = self._table(
            list_parent, ("request", "state", "confirmation", "stage"), (160, 140, 100, 200)
        )
        job_box.grid(
            row=1,
            column=0,
            columnspan=2 if self._job_pages is not None else 1,
            sticky="nsew",
            padx=(0, 16),
        )
        self.job_tree.heading("state", text="Status / result")
        self.job_tree.heading("confirmation", text="Review")
        from tkinter import font

        status_font = font.nametofont("TkDefaultFont", root=root)
        for column, labels in (
            ("state", LABELS.values()),
            ("confirmation", ("Not reviewed", "Reviewed")),
        ):
            width = max(status_font.measure(label) for label in labels) + 24
            self.job_tree.column(column, minwidth=width, width=width)
        configure_job_status_tags(self.job_tree)
        self.job_tree.bind(
            "<<ThemeChanged>>", lambda _event: configure_job_status_tags(self.job_tree), add="+"
        )
        self.job_tree.bind("<<TreeviewSelect>>", self._select)
        self.job_tree.bind("<Configure>", self._resize_job_list, add="+")
        self.job_tree.bind("<Return>", lambda _event: self._show_selected())
        self.job_tree.bind("<Double-1>", lambda _event: self._show_selected())
        detail.columnconfigure(0, weight=1)
        if self._detail_canvas is None:
            detail.rowconfigure(3, weight=1)
        self.detail_heading = tk.StringVar(value="Selected job")
        detail_header = ttk.Frame(detail)
        detail_header.grid(row=0, sticky="ew")
        detail_header.columnconfigure(0, weight=1)
        self.detail_heading_label = ttk.Label(
            detail_header, textvariable=self.detail_heading, style="Section.TLabel", wraplength=300
        )
        self.detail_heading_label.grid(row=0, column=0, sticky="ew")
        detail_box, self.detail_text = self._text(detail, height=6, wrap="word")
        detail_box.grid(row=1, sticky="nsew", pady=(8, 12))
        controls = ttk.Frame(detail)
        controls.grid(row=2, sticky="ew", pady=(0, 8))
        controls.columnconfigure(0, weight=1)
        self.log_context = tk.StringVar(value="Logs | No job selected")
        self.log_marker = ttk.Label(controls, textvariable=self.log_context, wraplength=340)
        self.log_marker.grid(row=0, column=0, sticky="w")
        self.copy_button = ttk.Button(controls, text="Copy text", command=self._copy_result)
        self.copy_button.grid(row=0, column=1, sticky="e", padx=(8, 0))
        log_box, self.log_text = self._text(detail, height=10, wrap="word")
        self.log_text.configure(font="TkFixedFont")
        log_box.grid(row=3, sticky="nsew")
        self.log_status = tk.StringVar(value=self.logs.message)
        self.log_status_label = ttk.Label(detail, textvariable=self.log_status, wraplength=420)
        self.log_status_label.grid(row=4, sticky="ew", pady=(6, 0))
        self.image_context = tk.StringVar(value="Captured image | No job selected")
        self.image_marker = ttk.Label(detail, textvariable=self.image_context, wraplength=420)
        self.image_marker.grid(row=5, sticky="ew", pady=(12, 0))
        self.preview_area = ttk.Frame(detail, width=240, height=135)
        self.preview_area.grid_propagate(False)
        self.preview_image = tk.Label(self.preview_area, borderwidth=0)
        self.preview_image.pack(fill="both", expand=True)
        self.preview_status = tk.StringVar(value=self.preview_state.message)
        self.preview_status_label = ttk.Label(
            detail, textvariable=self.preview_status, wraplength=420
        )
        self.preview_status_label.grid(row=7, sticky="ew", pady=(6, 0))
        self.cancel_context = tk.StringVar(value="")
        self.cancel_context_label = ttk.Label(
            detail, textvariable=self.cancel_context, wraplength=420
        )
        self.cancel_context_label.grid(row=8, sticky="ew", pady=(8, 0))
        self.job_cancel = ttk.Button(
            detail, text="Cancel job...", command=self._cancel_job, style="Danger.TButton"
        )
        self.job_cancel.grid(row=9, sticky="e", pady=(16, 0))
        detail.bind("<Configure>", self._resize_details, add="+")
        if self._detail_canvas is not None:
            for widget in (
                self.detail_text,
                self.log_text,
                self.copy_button,
                self.job_cancel,
            ):
                widget.bind("<FocusIn>", self._reveal_detail_focus, add="+")
                for key in ("Prior", "Next", "Home", "End"):
                    widget.bind(
                        f"<{key}>",
                        self._detail_key_callback(key),
                        add="+",
                    )
        events.columnconfigure(0, weight=1)
        events.rowconfigure(1, weight=1)
        ttk.Label(events, text="Observed job changes", style="Section.TLabel").grid(
            row=0, sticky="w", pady=(0, 8)
        )
        event_box, self.event_text = self._text(events, height=12, wrap="word")
        event_box.grid(row=1, sticky="nsew")
        connection_body, self.connection_canvas = self._scrollable_body(connection)
        self._connection_view(connection_body)
        root.bind("<Control-r>", lambda _event: self._reconnect())
        root.bind("<Alt-Left>", lambda _event: self._back_to_list())
        root.bind("<Escape>", lambda _event: self._close())
        self.runner = LiveRunner(api)
        self.runner.start(self.state.generation)
        self._render()
        self.timer = root.after(50, self._drain)

    def _initialize_panes(self, event: Any) -> None:
        if not self._panes_initialized and event.width > 1:
            self._panes_initialized = True
            self.root.tk.call(str(self.job_panes), "sashpos", 0, int(event.width * 0.52))

    def _resize_job_list(self, event: Any) -> None:
        columns = (
            ("request", "state", "confirmation", "stage")
            if event.width >= 850
            else ("request", "state", "stage")
            if event.width >= 650
            else ("state",)
        )
        if tuple(self.job_tree.cget("displaycolumns")) != columns:
            self.job_tree.configure(displaycolumns=columns)

    def _scrollable_detail(self, parent: Any) -> Any:
        detail, self._detail_canvas = self._scrollable_body(parent)
        return detail

    def _scrollable_body(self, parent: Any) -> tuple[Any, Any]:
        import tkinter as tk
        from tkinter import ttk

        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(0, weight=1)
        canvas = tk.Canvas(
            parent, borderwidth=0, highlightthickness=0, background=self.root.cget("background")
        )
        canvas.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(parent, orient="vertical", command=canvas.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        canvas.configure(yscrollcommand=scrollbar.set)
        detail = ttk.Frame(canvas)
        item = canvas.create_window((0, 0), window=detail, anchor="nw")
        detail.bind("<Configure>", lambda _event: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda event: canvas.itemconfigure(item, width=event.width))
        return detail, canvas

    def _resize_details(self, event: Any) -> None:
        for label in (
            self.log_status_label,
            self.preview_status_label,
            self.image_marker,
            self.cancel_context_label,
        ):
            label.configure(wraplength=max(1, event.width))
        self.log_marker.configure(
            wraplength=max(1, event.width - self.copy_button.winfo_reqwidth() - 24)
        )
        self.detail_heading_label.configure(wraplength=max(1, event.width))

    def _scroll_detail_key(self, key: str) -> str:
        canvas = self._detail_canvas
        if key == "Home":
            canvas.yview_moveto(0.0)
        elif key == "End":
            canvas.yview_moveto(1.0)
        else:
            canvas.yview_scroll(-1 if key == "Prior" else 1, "pages")
        return "break"

    def _detail_key_callback(self, key: str) -> Callable[[Any], str]:
        def callback(_event: Any) -> str:
            return self._scroll_detail_key(key)

        return callback

    def _reveal_detail_focus(self, event: Any) -> None:
        # The tab switch already reveals this target. Its delayed FocusIn must
        # not pull the canvas back up after an image preview scrolls to the end.
        if event.widget is self.detail_text and self._suppress_next_detail_focus:
            self._suppress_next_detail_focus = False
            return
        self._suppress_next_detail_focus = False
        canvas = self._detail_canvas
        if canvas is None or not canvas.winfo_ismapped():
            return
        self.root.update_idletasks()
        top = canvas.winfo_rooty()
        bottom = top + canvas.winfo_height()
        widget_top = event.widget.winfo_rooty()
        widget_bottom = widget_top + event.widget.winfo_height()
        if widget_top < top:
            offset = widget_top - top
        elif widget_bottom > bottom:
            offset = widget_bottom - bottom
        else:
            return
        region = canvas.bbox("all")
        if region is not None and region[3] > region[1]:
            canvas.yview_moveto(canvas.yview()[0] + offset / (region[3] - region[1]))

    @staticmethod
    def _table(parent: Any, columns: tuple[str, ...], widths: tuple[int, ...]) -> Any:
        from tkinter import ttk

        box = ttk.Frame(parent)
        box.columnconfigure(0, weight=1)
        box.rowconfigure(0, weight=1)
        tree = ttk.Treeview(box, columns=columns, show="tree headings", selectmode="browse")
        tree.heading("#0", text="Job" if columns[0] == "request" else "ID")
        tree.column("#0", width=170, minwidth=90, stretch=True)
        for name, width in zip(columns, widths, strict=True):
            tree.heading(name, text=name.replace("_", " ").title())
            tree.column(name, width=width, minwidth=75, stretch=True)
        vertical = ttk.Scrollbar(box, orient="vertical", command=tree.yview)
        horizontal = ttk.Scrollbar(box, orient="horizontal", command=tree.xview)

        def scroll_x(first: float, last: float) -> None:
            horizontal.set(first, last)
            if float(first) <= 0 and float(last) >= 1:
                horizontal.grid_remove()
            else:
                horizontal.grid(row=1, column=0, sticky="ew")

        tree.configure(yscrollcommand=vertical.set, xscrollcommand=scroll_x)
        tree.grid(row=0, column=0, sticky="nsew")
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal.grid(row=1, column=0, sticky="ew")
        return box, tree

    @staticmethod
    def _text(parent: Any, *, height: int, wrap: Literal["none", "char", "word"]) -> Any:
        import tkinter as tk
        from tkinter import ttk

        box = ttk.Frame(parent)
        box.columnconfigure(0, weight=1)
        box.rowconfigure(0, weight=1)
        text = tk.Text(
            box,
            height=height,
            width=1,
            wrap=wrap,
            state="disabled",
            font="TkFixedFont" if wrap == "none" else "TkTextFont",
            relief="flat",
            borderwidth=0,
            padx=8,
            pady=8,
            takefocus=True,
            highlightthickness=1,
            highlightcolor="#2563eb",
            highlightbackground="#cbd5e1",
        )
        vertical = ttk.Scrollbar(box, orient="vertical", command=text.yview)
        text.configure(yscrollcommand=vertical.set)
        text.grid(row=0, column=0, sticky="nsew")
        vertical.grid(row=0, column=1, sticky="ns")
        if wrap == "none":
            horizontal = ttk.Scrollbar(box, orient="horizontal", command=text.xview)
            text.configure(xscrollcommand=horizontal.set)
            horizontal.grid(row=1, column=0, sticky="ew")
        return box, text

    def _connection_view(self, parent: Any) -> None:
        import tkinter as tk
        from tkinter import ttk

        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(3, weight=0)
        self.connection_status = tk.StringVar(value="Waiting for broker state")
        ttk.Label(parent, text="Broker and device ownership", style="Section.TLabel").grid(
            row=0, sticky="w"
        )
        self.connection_status_label = ttk.Label(
            parent, textvariable=self.connection_status, wraplength=900
        )
        self.connection_status_label.grid(row=1, sticky="ew", pady=8)
        self.live_diagnostic_text = tk.StringVar(value="No live subscription failure recorded")
        self.live_diagnostic_label = ttk.Label(
            parent, textvariable=self.live_diagnostic_text, wraplength=900
        )
        controls = ttk.Frame(parent)
        controls.grid(row=2, sticky="ew", pady=(0, 12))
        self.release_button = ttk.Button(
            controls, text="Release idle owner...", command=self._release
        )
        self.release_button.pack(side="left")
        if self.tunnel_panel is not None:
            ttk.Button(controls, text="Tunnel...", command=self.tunnel_panel.show).pack(
                side="right"
            )
        wait_box, self.wait_tree = self._table(
            parent, ("position", "state", "session"), (90, 120, 220)
        )
        self.wait_box = wait_box
        wait_box.grid(row=3, sticky="ew")
        self.wait_empty = ttk.Label(parent, text="Waiting for queue state")
        self.wait_empty.grid(row=3, sticky="w", pady=8)
        self.wait_tree.bind("<<TreeviewSelect>>", self._select_wait)
        actions = ttk.Frame(parent)
        self.wait_actions = actions
        actions.grid(row=4, sticky="ew", pady=10)
        self.wait_cancel = ttk.Button(
            actions, text="Cancel waiting request", command=self._cancel_wait
        )
        self.wait_cancel.pack(side="left")
        self.up_button = ttk.Button(actions, text="Move up", command=lambda: self._move(-1))
        self.up_button.pack(side="left", padx=8)
        self.down_button = ttk.Button(actions, text="Move down", command=lambda: self._move(1))
        self.down_button.pack(side="left")
        self.connection_panels = DiagnosticPanels(
            parent, lambda kind: self._diagnostic(kind == "mcp")
        )
        self.connection_panels.frame.grid(row=5, sticky="nsew", pady=(8, 0))
        parent.bind("<Configure>", self._resize_connection, add="+")
        for panel in self.connection_panels.panels.values():
            for widget in (panel.toggle, panel.refresh, panel.copy, panel.text):
                widget.bind("<FocusIn>", self._reveal_connection_focus, add="+")

    def _resize_connection(self, event: Any) -> None:
        for label in (self.connection_status_label, self.live_diagnostic_label):
            label.configure(wraplength=max(1, event.width))

    def _reveal_connection_focus(self, event: Any) -> None:
        canvas = self.connection_canvas
        self.root.update_idletasks()
        top = canvas.winfo_rooty()
        widget_top = event.widget.winfo_rooty()
        widget_bottom = widget_top + event.widget.winfo_height()
        region = canvas.bbox("all")
        if not region or region[3] <= region[1]:
            return
        offset = (
            widget_top - top
            if widget_top < top
            else widget_bottom - top - canvas.winfo_height()
            if widget_bottom > top + canvas.winfo_height()
            else 0
        )
        if offset:
            canvas.yview_moveto(canvas.yview()[0] + offset / (region[3] - region[1]))

    def _select(self, _event: Any = None) -> None:
        chosen = self.job_tree.selection()
        selected = chosen[0] if chosen else ""
        if not selected and self.state.selection_missing:
            return
        if selected == self.state.selected_job_id:
            return
        self.preview_state = PreviewState()
        self.state = select(self.state, selected)
        self.logs, self.log_requests = LogView(), LogRequestState()
        self._render()
        if selected:
            self._read_logs()
            self._auto_preview()

    def _show_selected(self) -> None:
        if not self.state.selected_job_id:
            return
        self.root.update_idletasks()
        self._detail_canvas.configure(scrollregion=self._detail_canvas.bbox("all"))
        self._detail_canvas.yview_moveto(0.0)
        self._suppress_next_detail_focus = True
        self.detail_text.focus_set()

    def _back_to_list(self) -> None:
        self.job_tree.focus_set()

    def _select_wait(self, _event: Any = None) -> None:
        selected = self.wait_tree.selection()
        self.queue_controller.selected_request = selected[0] if selected else ""
        self._controls()

    def _read_logs(self) -> None:
        card = self.state.selected_card
        if card is not None and not logs_applicable(card.test):
            self.logs = LogView("not_applicable")
            self.log_requests = self.log_requests.settled()
            self._render_logs()
            self._controls()
            return
        if (
            self.state.connection != "live"
            or not self.state.selected_card
            or self.log_requests.pending
        ):
            return
        request_id = self.runner.read_logs(
            self.state.generation, self.state.selected_job_id, self.logs.cursor
        )
        self.log_requests = self.log_requests.started(request_id)
        self._render_logs()
        self._controls()

    def _reconnect(self, *, automatic: bool = False) -> None:
        if self.state.connection == "closed" or self.action_pending:
            return
        if not automatic:
            self._retry_state().manual()
        self._retry_state().starting()
        self.preview_state = PreviewState()
        self.state = begin(self.state)
        self.log_requests = LogRequestState()
        self.runner.reconnect(self.state.generation)
        self._render()

    def _retry_state(self) -> LiveRetry:
        # Older test adapters can create the view without its Tk constructor.
        if not hasattr(self, "live_retry"):
            self.live_retry = LiveRetry()
            self.live_diagnostics = []
        return self.live_retry

    def _live_failed(self, details: dict[str, Any]) -> None:
        self._retry_state().failed(details, time.monotonic())
        self.live_diagnostics = [*self.live_diagnostics[-31:], details]
        self._update_live_diagnostics()

    def _update_live_diagnostics(self) -> None:
        if hasattr(self, "live_diagnostic_text"):
            last = self._retry_state().last_received_utc or "none"
            lines = [f"Last validated live response UTC: {last}"]
            lines.extend(str(item) for item in self.live_diagnostics[-4:])
            self.live_diagnostic_text.set("\n".join(lines))
            if hasattr(self, "live_diagnostic_label"):
                if self.live_diagnostics:
                    self.live_diagnostic_label.grid(row=6, sticky="ew", pady=8)
                else:
                    self.live_diagnostic_label.grid_remove()

    def _drain(self) -> None:
        self.timer = None
        if self.state.connection == "closed":
            return
        for event in self.runner.drain():
            if event.generation != self.state.generation:
                continue
            if event.kind == "message":
                before = self.state
                self.state = apply(self.state, event.payload, event.generation, event.started_at)
                if before.selected_job_id != self.state.selected_job_id:
                    self.logs, self.log_requests = LogView(), LogRequestState()
                if self.state.connection == "live":
                    self._retry_state().received(time.monotonic())
                    self._update_live_diagnostics()
                    self._record_changes(before)
                self._render()
                if self.state.resync_required:
                    self.runner.pause(self.state.generation)
                    self._live_failed(diagnostic("invalid_response", self.state.generation, 0))
                    self.log_requests = LogRequestState()
                elif self.state.connection == "live" and self.state.selected_card:
                    if (
                        before.selected_card is None
                        or event.payload.get("kind") in ("snapshot", "reset")
                        or event.payload.get("logs_changed") is True
                        and (self.logs.phase != "denied" or self.log_requests.pending)
                    ):
                        self.log_requests = self.log_requests.changed()
                    if self.log_requests.dirty:
                        self._read_logs()
            elif event.kind == "disconnected":
                self.preview_state = PreviewState()
                reasons = {
                    AUTH_FAILED: "authentication_failed",
                    DELIVERY_GAP: "delivery_gap",
                    INVALID_RESPONSE: "invalid_response",
                    UNREACHABLE: "unreachable",
                    REJECTED: "rejected",
                }
                error = event.payload.get("error")
                self.state = disconnect(
                    self.state,
                    event.generation,
                    reason=reasons.get(error) if type(error) is str else None,
                )
                details = event.diagnostic or diagnostic(
                    "invalid_response"
                    if error == INVALID_RESPONSE
                    else "delivery_gap"
                    if error == DELIVERY_GAP
                    else "unclassified_disconnect",
                    self.state.generation,
                    0,
                )
                self._live_failed(details)
                self.log_requests = LogRequestState()
                self._render()
            elif event.kind == "image":
                self._reconcile_preview()
                self.preview_state = self.preview_state.accept(
                    event.generation,
                    event.payload.get("job_id", ""),
                    event.payload.get("request_id", 0),
                    event.payload.get("response"),
                    event.started_at,
                    time.monotonic(),
                )
                self._render_preview()
            elif (
                event.kind == "logs"
                and event.payload.get("job_id") == self.state.selected_job_id
                and self.log_requests.pending
                and event.payload.get("request_id") == self.log_requests.request_id
            ):
                self.log_requests = self.log_requests.completed(event.payload["request_id"])
                self.logs = read_log_result(self.logs, event.payload.get("response"))
                if self.logs.phase in {"available", "empty", "expired"}:
                    self.logs = replace(self.logs, received_utc=event.received_utc or "")
                if self.logs.phase == "denied":
                    self.log_requests = self.log_requests.settled()
                self._render_logs()
                if self.logs.has_more:
                    self.log_requests = self.log_requests.changed()
                if self.log_requests.dirty and self.logs.phase != "denied":
                    self._read_logs()
        try:
            while True:
                kind, value = self.action_results.get_nowait()
                self.action_pending = False
                if kind.startswith("diagnostic_"):
                    panel = kind.removeprefix("diagnostic_")
                    if not isinstance(value, DiagnosticOutcome) or value.text is None:
                        self.connection_panels.show_error(panel)
                    else:
                        self.connection_panels.show_result(panel, value.text, value.received_utc)
                    self.action_status.set("")
                else:
                    if kind == "queue":
                        if isinstance(value, Outcome):
                            self.queue_controller.accept(value)
                            value = value.message
                        else:
                            self.queue_controller.busy = False
                            self.queue_controller.fresh = False
                    self.action_status.set(value)
                    self._reconnect()
        except queue.Empty:
            pass
        retry = self._retry_state()
        now = time.monotonic()
        if self.state.connection == "disconnected":
            if retry.due(now) and not self.action_pending and not self._is_closing():
                self._reconnect(automatic=True)
            elif retry.deadline is not None:
                seconds = max(0, int(retry.deadline - now + 0.999))
                self.status.set(
                    f"Updates paused; reconnecting in {seconds}s (attempt {retry.attempts})"
                )
            elif retry.stopped:
                self.status.set(self.state.message + " Automatic reconnect paused; see Connection")
        self._render_preview()
        self._auto_preview()
        self._controls()
        self.timer = self.root.after(50, self._drain)

    def _record_changes(self, previous: LiveState) -> None:
        old = {card.job_id: card for card in previous.timeline.cards} if previous.timeline else {}
        if self.state.timeline:
            for card in self.state.timeline.cards:
                earlier = old.get(card.job_id)
                if earlier is None or (earlier.state, earlier.stage, earlier.result) != (
                    card.state,
                    card.stage,
                    card.result,
                ):
                    self.events.append(
                        f"{card.job_id} | {card.state} | {card.stage or '-'} | {card.result or '-'}"
                    )
        if (
            previous.cursor
            and self.state.cursor
            and previous.cursor.get("epoch") != self.state.cursor.get("epoch")
        ):
            self.events.append("Broker epoch changed. Previous selection was cleared.")
        if len(self.events) > 256:
            self.events = ["Earlier displayed events were omitted.", *self.events[-255:]]
        replace_text(self.event_text, "\n".join(self.events), follow=True)

    def _render(self) -> None:
        live = self.state.connection == "live"
        received = self._retry_state().last_received_utc
        self.status.set(
            self.state.message + (f" {received[11:19]} UTC" if live and received else "")
        )
        cards = self.state.timeline.cards if self.state.timeline else ()
        sync_rows(
            self.job_tree,
            [
                (
                    card.job_id,
                    card.job_id,
                    (
                        card.test or "-",
                        card.presentation.label,
                        card.presentation.review_label,
                        card.stage or "-",
                    ),
                )
                for card in cards
            ],
        )
        for row in cards:
            tag = row.presentation.tag
            self.job_tree.item(row.job_id, tags=(tag,) if tag else ())
        selected = self.state.selected_job_id
        if selected and self.job_tree.exists(selected):
            if self.job_tree.selection() != (selected,):
                self.job_tree.selection_set(selected)
        elif not selected and self.job_tree.selection():
            self.job_tree.selection_set(())
        self.history_status.set("" if live else "Updates paused")
        card = self.state.selected_card
        detail = "Select a job to inspect its status and permitted logs."
        heading = "Selected job"
        if card:
            heading = f"{card.job_id} | {card.presentation.label}"
            if card.failure_reason:
                heading += "\nReason: " + REASON_LABELS[card.failure_reason]
            display = card.presentation
            fields = [
                f"Request  {card.test or 'Unknown'}",
                f"Lifecycle  {display.lifecycle_label}",
                f"Outcome  {display.outcome_label}",
                f"Backend record  {card.state}",
            ]
            if card.stage:
                fields.append(f"Stage  {card.stage}")
            if card.result:
                fields.append(f"Diagnostic code  {card.result}")
            dates = []
            if card.created_utc:
                dates.append(f"Registered  {card.created_utc}")
            else:
                dates.append("Registered  time unknown (legacy history)")
            if card.finished_utc:
                dates.append(f"Finished  {card.finished_utc}")
            dates.append(f"Historical acknowledgement  {display.review_label}")
            detail = "\n".join(fields) + "\n\n" + "\n".join(dates)
        elif self.state.selected_job_id:
            heading = f"{self.state.selected_job_id} | Outside retained view"
            detail = "This selected job is no longer in the retained view."
        self.detail_heading.set(heading)
        if not live:
            detail += "\n\nConnection is not live. Job execution may continue at the broker."
        replace_text(self.detail_text, detail)
        self.queue_controller.snapshot = self.state.snapshot
        self.queue_controller.fresh = self._actions_ready()
        snapshot = self.state.snapshot
        if snapshot:
            owner = snapshot.owner
            self.queue_controller.selected_lease = owner.lease_id if owner else ""
            execution_uncertainty = "not reported"
            if snapshot.execution_uncertain is not None:
                execution_uncertainty = "yes" if snapshot.execution_uncertain else "no"
            active = next((row for row in cards if row.job_id == snapshot.active_job), None)
            operation = snapshot.active_job or "none"
            if active and active.stage:
                operation += f" / {active.stage}"
            self.connection_status.set(
                f"Broker updates: {'Live' if live else 'Paused'} | Backend: {snapshot.backend}\n"
                f"Owner: {owner.owner if owner else 'none'} | Active job: {operation}\n"
                f"Current execution uncertainty: {execution_uncertainty} | "
                f"Current ownership uncertainty: "
                f"{'yes' if snapshot.ownership_uncertain else 'no'}\n"
                f"Historical review: {'pending' if snapshot.needs_review else 'none'}"
            )
            if hasattr(self, "wait_box"):
                if snapshot.waiters:
                    self.wait_empty.grid_remove()
                    self.wait_box.grid()
                    self.wait_actions.grid()
                    self.wait_tree.configure(height=min(8, len(snapshot.waiters)))
                else:
                    self.wait_box.grid_remove()
                    self.wait_actions.grid_remove()
                    self.wait_empty.configure(text="No waiting requests")
                    self.wait_empty.grid()
            sync_rows(
                self.wait_tree,
                [
                    (
                        row.request_id,
                        row.request_id,
                        (str(row.position or "-"), row.state, row.session_id),
                    )
                    for row in snapshot.waiters
                ],
            )
        self._render_logs()
        self._render_preview()
        self._controls()

    def _render_logs(self) -> None:
        card = self.state.selected_card
        if card is not None and not logs_applicable(card.test):
            self.logs = LogView("not_applicable")
            self.log_requests = self.log_requests.settled()
        message = self.logs.message
        if self.log_requests.pending:
            message = "Reading permitted output..."
        if self.logs.gap:
            message += " Some output is missing or was omitted."
        self.log_status.set(
            "Some output is missing or was omitted."
            if self.logs.gap
            else "Reading permitted output..."
            if self.log_requests.pending and self.logs.lines
            else ""
        )
        if hasattr(self, "log_context"):
            source = f"{card.test or 'Unknown'} / {card.backend}" if card else "No job selected"
            mode = (
                "Live updates"
                if card and card.state in {"queued", "running"} and self.state.connection == "live"
                else "Retained output"
            )
            received = f" | Read {self.logs.received_utc}" if self.logs.received_utc else ""
            self.log_context.set(f"Logs | {source} | {mode}{received}")
        replace_text(self.log_text, "\n".join(self.logs.lines) or message, follow=True)

    def _controls(self) -> None:
        ready = self._actions_ready()
        card = self.state.selected_card
        open_selected = getattr(self, "open_selected_button", None)
        if open_selected is not None:
            open_selected.configure(state="normal" if self.state.selected_job_id else "disabled")
        if hasattr(self, "copy_button"):
            self.copy_button.configure(state="normal" if card else "disabled")
        can_cancel = bool(ready and card and card.cancel_available)
        self.job_cancel.configure(state="normal" if can_cancel else "disabled")
        if can_cancel:
            self.job_cancel.grid()
        else:
            self.job_cancel.grid_remove()
        if hasattr(self, "cancel_context"):
            reason = ""
            if card and card.state in {"queued", "running"} and not card.cancel_available:
                reason = (
                    "Cancel via the owning client session"
                    if card.test in {*FLASH_TESTS, TestName.APP_SESSION}
                    else "Cancellation unavailable for this operator"
                )
            elif card and card.cancel_available and not ready:
                reason = "Cancellation currently unavailable"
            self.cancel_context.set(reason)
        if hasattr(self, "connection_panels"):
            self.connection_panels.set_enabled(not self.action_pending and not self._is_closing())
        self.release_button.configure(
            state="normal" if ready and self.queue_controller.can_release() else "disabled"
        )
        self.wait_cancel.configure(
            state="normal" if ready and self.queue_controller.can_cancel() else "disabled"
        )
        self.up_button.configure(
            state="normal" if ready and self.queue_controller.can_move(-1) else "disabled"
        )
        self.down_button.configure(
            state="normal" if ready and self.queue_controller.can_move(1) else "disabled"
        )

    def _reconcile_preview(self) -> None:
        card = self.state.selected_card
        self.preview_state = self.preview_state.reconcile(
            self.state.generation,
            self.state.timeline.broker_epoch if self.state.timeline else "",
            card.image if card else None,
            self.state.connection == "live",
            time.monotonic(),
        )

    def _preview(self) -> None:
        self._reconcile_preview()
        if not self._actions_ready() or not self.preview_state.can_read(time.monotonic()):
            self._render_preview()
            return
        assert self.preview_state.row is not None
        request_id = self.runner.read_image(
            self.state.generation,
            self.preview_state.row.job_id,
            expires_at=self.preview_state.row.expires_at,
        )
        self.preview_state = self.preview_state.started(request_id)
        self._render_preview()
        self._controls()

    def _render_preview(self) -> None:
        self._reconcile_preview()
        preview = self.preview_state.preview
        if preview is not None and (
            self.displayed_preview is None or self.displayed_preview.png is not preview.png
        ):
            import tkinter as tk

            try:
                photo = tk.PhotoImage(
                    master=self.root,
                    data=base64.b64encode(preview.png).decode("ascii"),
                    format="png",
                )
                self._reconcile_preview()
                if self.preview_state.preview is not None:
                    self.preview_photo = photo
                    self.displayed_preview = preview
                    self.preview_image.configure(image=photo)
                    self.preview_area.grid(row=6, sticky="w", pady=(8, 0))
            except Exception:
                self.preview_state = PreviewState(
                    self.preview_state.generation,
                    self.preview_state.epoch,
                    self.preview_state.row,
                    message=FAILED,
                    attempted=True,
                )
        if self.preview_state.preview is None:
            self.preview_image.configure(image="")
            self.preview_area.grid_remove()
            self.preview_photo = None
            self.displayed_preview = None
        self.preview_status.set(self.preview_state.message)
        if hasattr(self, "image_context"):
            row = self.preview_state.row
            self.image_context.set(
                f"Captured image | {SOURCES[row.source]} | Captured {row.born_utc} | Retained"
                if row
                else "Captured image | No retained image metadata"
            )

    def _auto_preview(self) -> None:
        self._reconcile_preview()
        if not self.preview_state.attempted and self.preview_state.can_read(time.monotonic()):
            self._preview()

    def _copy_result(self) -> None:
        if self.state.selected_card is None:
            return
        text = "\n\n".join(
            (
                self.detail_heading.get(),
                self.detail_text.get("1.0", "end-1c"),
                self.log_context.get(),
                self.log_text.get("1.0", "end-1c"),
                self.log_status.get(),
                self.image_context.get(),
                self.preview_status.get(),
            )
        )
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(text)
            self.action_status.set("Selected job text copied.")
        except Exception:
            self.action_status.set("Could not copy selected job text.")

    def _run_action(
        self,
        action: Any,
        *,
        queue_operation: bool = False,
        diagnostic_kind: str | None = None,
    ) -> None:
        if self.action_pending or self._is_closing() or self.state.connection == "closed":
            return
        self.action_pending = True
        self.action_status.set("Request pending...")
        self._controls()

        def worker() -> None:
            received = None
            try:
                value = action()
                if diagnostic_kind:
                    received = datetime.now(UTC)
            except Exception:
                value = (
                    None
                    if diagnostic_kind
                    else "Request outcome unknown. Reconnect before any explicit retry."
                )
            kind = (
                "diagnostic_" + diagnostic_kind
                if diagnostic_kind
                else "queue"
                if queue_operation
                else "action"
            )
            self.action_results.put(
                (kind, DiagnosticOutcome(value, received) if diagnostic_kind else value)
            )

        threading.Thread(target=worker, name="gui-explicit-action", daemon=True).start()

    def _cancel_job(self) -> None:
        from tkinter import messagebox

        card = self.state.selected_card
        if not self._actions_ready() or not card or not card.cancel_available:
            return
        epoch = self.state.timeline.broker_epoch if self.state.timeline else ""
        generation = self.state.generation
        if not messagebox.askyesno(
            "Cancel job", "Request cancellation of this selected job?", parent=self.root
        ):
            return
        if self.state.connection == "closed":
            return
        if (
            not self._actions_ready()
            or self.state.generation != generation
            or self.state.selected_card != card
            or self.state.timeline is None
            or self.state.timeline.broker_epoch != epoch
        ):
            self.action_status.set("Selection changed. Review the current job before cancelling.")
            return

        operation = TimelineOperation(
            0, generation, "cancel_job", time.monotonic(), card.job_id, card.plan_hash or "", epoch
        )

        def cancel() -> str:
            result = perform_timeline(self.api, operation)
            return result.error or result.message

        self._run_action(cancel)

    def _queue_action(self, operation: Any) -> None:
        if operation is not None:
            self._run_action(lambda: perform(self.api, operation), queue_operation=True)

    def _cancel_wait(self) -> None:
        if not self._actions_ready():
            return
        self._queue_action(self.queue_controller.cancel())

    def _move(self, direction: int) -> None:
        if not self._actions_ready():
            return
        self._queue_action(self.queue_controller.move(direction))

    def _release(self) -> None:
        from tkinter import messagebox

        confirmed = self.queue_controller.selected_lease
        generation = self.state.generation
        epoch = self.state.timeline.broker_epoch if self.state.timeline else ""
        if (
            self._actions_ready()
            and self.queue_controller.can_release()
            and messagebox.askyesno(
                "Release idle owner",
                "Release this idle owner? Active work cannot be released.",
                parent=self.root,
            )
        ):
            if (
                self._actions_ready()
                and self.state.generation == generation
                and self.state.timeline is not None
                and self.state.timeline.broker_epoch == epoch
            ):
                self._queue_action(self.queue_controller.release(confirmed))

    def _diagnostic(self, catalog: bool) -> None:
        if self.action_pending or self._is_closing() or self.state.connection == "closed":
            return
        kind = "mcp" if catalog else "http"
        self.connection_panels.show_loading(kind)
        self._run_action(
            lambda: (
                mcp_catalog_summary(self.api.catalog())
                if catalog
                else http_diagnostics_summary(self.api.diagnostics())
            ),
            diagnostic_kind=kind,
        )

    def _is_closing(self) -> bool:
        return bool(
            getattr(self, "close_prompt", False)
            or self.tunnel_panel is not None
            and self.tunnel_panel.controller.closing
        )

    def _actions_ready(self) -> bool:
        return (
            self.state.connection == "live" and not self.action_pending and not self._is_closing()
        )

    def _close(self) -> None:
        if self.action_pending:
            self.status.set("Waiting for the explicit request result before closing.")
            return
        if self._is_closing() or self.state.connection == "closed":
            return
        self.close_prompt = True
        try:
            if self.tunnel_panel is not None:
                self.tunnel_panel.request_close(self._finish_close)
            else:
                self._finish_close()
        finally:
            self.close_prompt = False

    def _finish_close(self) -> None:
        if self.action_pending or self.state.connection == "closed":
            return
        self.state = close(self.state)
        self.preview_state = PreviewState()
        self.preview_photo = None
        self.displayed_preview = None
        self.preview_image.configure(image="")
        if self.timer is not None:
            self.root.after_cancel(self.timer)
            self.timer = None
        self.runner.close()
        self.root.destroy()
