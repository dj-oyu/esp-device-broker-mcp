"""Tk adapter for the live projection. Workers own IO; Tk owns all widgets."""

import base64
import queue
import threading
import time
from typing import Any, Literal

from .gui import Outcome, QueueController, http_diagnostics_summary, mcp_catalog_summary, perform
from .gui_live_presentation import (
    LogRequestState,
    LogView,
    logs_applicable,
    read_log_result,
    replace_text,
    sync_rows,
)
from .gui_live_preview import FAILED, PreviewState
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
        notebook.grid(row=1, column=0, sticky="nsew", padx=16, pady=(0, 12))
        ttk.Label(root, textvariable=self.action_status, wraplength=1000).grid(
            row=2, column=0, sticky="ew", padx=16, pady=(0, 8)
        )
        jobs, events, connection = (ttk.Frame(notebook, padding=12) for _ in range(3))
        for frame, name in ((jobs, "Jobs"), (events, "Events"), (connection, "Connection")):
            notebook.add(frame, text=name)
        jobs.columnconfigure(0, weight=3)
        jobs.columnconfigure(1, weight=2)
        jobs.rowconfigure(1, weight=1)
        ttk.Label(jobs, text="Recent jobs", style="Section.TLabel").grid(
            row=0, column=0, sticky="w", pady=(0, 8)
        )
        self.history_status = tk.StringVar(value="Waiting for a consistent snapshot")
        ttk.Label(jobs, textvariable=self.history_status).grid(row=2, column=0, sticky="w", pady=8)
        job_box, self.job_tree = self._table(jobs, ("request", "state", "stage"), (160, 110, 200))
        job_box.grid(row=1, column=0, sticky="nsew", padx=(0, 16))
        self.job_tree.bind("<<TreeviewSelect>>", self._select)
        detail = ttk.Frame(jobs)
        detail.grid(row=0, column=1, rowspan=3, sticky="nsew")
        detail.columnconfigure(0, weight=1)
        detail.rowconfigure(3, weight=1)
        self.detail_heading = tk.StringVar(value="Selected job")
        self.detail_heading_label = ttk.Label(
            detail, textvariable=self.detail_heading, style="Section.TLabel", wraplength=420
        )
        self.detail_heading_label.grid(row=0, sticky="ew")
        detail.bind(
            "<Configure>",
            lambda event: self.detail_heading_label.configure(wraplength=max(1, event.width)),
        )
        detail_box, self.detail_text = self._text(detail, height=11, wrap="word")
        detail_box.grid(row=1, sticky="nsew", pady=(8, 12))
        controls = ttk.Frame(detail)
        controls.grid(row=2, sticky="ew", pady=(0, 8))
        self.log_button = ttk.Button(controls, text="Read permitted logs", command=self._read_logs)
        controls.columnconfigure(0, weight=1)
        controls.columnconfigure(1, weight=1)
        self.log_button.grid(row=0, column=0, sticky="w", pady=(0, 6))
        self.preview_button = ttk.Button(
            controls, text="Preview retained image", command=self._preview
        )
        self.preview_button.grid(row=1, column=0, columnspan=2, sticky="w")
        self.job_cancel = ttk.Button(controls, text="Cancel job...", command=self._cancel_job)
        self.job_cancel.grid(row=0, column=1, sticky="e", padx=(8, 0), pady=(0, 6))
        log_box, self.log_text = self._text(detail, height=10, wrap="none")
        log_box.grid(row=3, sticky="nsew")
        self.log_status = tk.StringVar(value=self.logs.message)
        ttk.Label(detail, textvariable=self.log_status, wraplength=420).grid(
            row=4, sticky="ew", pady=(6, 0)
        )
        self.preview_area = ttk.Frame(detail, width=240, height=135)
        self.preview_area.grid_propagate(False)
        self.preview_image = tk.Label(self.preview_area, borderwidth=0)
        self.preview_image.pack(fill="both", expand=True)
        self.preview_status = tk.StringVar(value=self.preview_state.message)
        ttk.Label(detail, textvariable=self.preview_status, wraplength=420).grid(
            row=6, sticky="ew", pady=(6, 0)
        )
        events.columnconfigure(0, weight=1)
        events.rowconfigure(1, weight=1)
        ttk.Label(events, text="Observed job changes", style="Section.TLabel").grid(
            row=0, sticky="w", pady=(0, 8)
        )
        event_box, self.event_text = self._text(events, height=12, wrap="word")
        event_box.grid(row=1, sticky="nsew")
        ttk.Label(
            events, text="Safe job state and stage events. Private output stays in Jobs."
        ).grid(row=2, sticky="w", pady=8)
        self._connection_view(connection)
        root.bind("<Control-r>", lambda _event: self._reconnect())
        root.bind("<Escape>", lambda _event: self._close())
        self.runner = LiveRunner(api)
        self.runner.start(self.state.generation)
        self._render()
        self.timer = root.after(50, self._drain)

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
        tree.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
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
        parent.rowconfigure(3, weight=1)
        self.connection_status = tk.StringVar(value="Waiting for broker state")
        ttk.Label(parent, text="Broker and device ownership", style="Section.TLabel").grid(
            row=0, sticky="w"
        )
        ttk.Label(parent, textvariable=self.connection_status, wraplength=900).grid(
            row=1, sticky="ew", pady=8
        )
        controls = ttk.Frame(parent)
        controls.grid(row=2, sticky="ew", pady=(0, 12))
        self.release_button = ttk.Button(
            controls, text="Release idle owner...", command=self._release
        )
        self.release_button.pack(side="left")
        ttk.Button(controls, text="HTTP diagnostics", command=lambda: self._diagnostic(False)).pack(
            side="left", padx=8
        )
        ttk.Button(controls, text="MCP info", command=lambda: self._diagnostic(True)).pack(
            side="left"
        )
        if self.tunnel_panel is not None:
            ttk.Button(controls, text="Tunnel...", command=self.tunnel_panel.show).pack(
                side="right"
            )
        wait_box, self.wait_tree = self._table(
            parent, ("position", "state", "session"), (90, 120, 220)
        )
        wait_box.grid(row=3, sticky="nsew")
        self.wait_tree.bind("<<TreeviewSelect>>", self._select_wait)
        actions = ttk.Frame(parent)
        actions.grid(row=4, sticky="ew", pady=10)
        self.wait_cancel = ttk.Button(
            actions, text="Cancel waiting request", command=self._cancel_wait
        )
        self.wait_cancel.pack(side="left")
        self.up_button = ttk.Button(actions, text="Move up", command=lambda: self._move(-1))
        self.up_button.pack(side="left", padx=8)
        self.down_button = ttk.Button(actions, text="Move down", command=lambda: self._move(1))
        self.down_button.pack(side="left")
        connection_box, self.connection_detail = self._text(parent, height=8, wrap="word")
        connection_box.grid(row=5, sticky="ew")

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

    def _reconnect(self) -> None:
        if self.state.connection == "closed" or self.action_pending:
            return
        self.preview_state = PreviewState()
        self.state = begin(self.state)
        self.log_requests = LogRequestState()
        self.runner.reconnect(self.state.generation)
        self._render()

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
                    self._record_changes(before)
                self._render()
                if self.state.resync_required:
                    self.runner.pause(self.state.generation)
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
                if kind == "diagnostic":
                    replace_text(self.connection_detail, value)
                    self.action_status.set("Diagnostic request completed. See Connection.")
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
        self._render_preview()
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
        self.status.set(self.state.message)
        cards = self.state.timeline.cards if self.state.timeline else ()
        sync_rows(
            self.job_tree,
            [
                (card.job_id, card.job_id, (card.test or "-", card.state, card.stage or "-"))
                for card in cards
            ],
        )
        selected = self.state.selected_job_id
        if selected and self.job_tree.exists(selected):
            if self.job_tree.selection() != (selected,):
                self.job_tree.selection_set(selected)
        elif not selected and self.job_tree.selection():
            self.job_tree.selection_set(())
        self.history_status.set(
            "Older jobs are outside this bounded view."
            if self.state.timeline and self.state.timeline.has_more
            else ("Live updates" if live else "Showing the last verified snapshot")
        )
        card = self.state.selected_card
        detail = "Select a job to inspect its status and permitted logs."
        heading = "Selected job"
        if card:
            heading = f"{card.job_id} | {card.state}"
            fields = [f"Request  {card.test or 'Unknown'}"]
            if card.stage:
                fields.append(f"Stage  {card.stage}")
            if card.result:
                fields.append(f"Result  {card.result}")
            dates = []
            if card.created_utc:
                dates.append(f"Created  {card.created_utc}")
            if card.finished_utc:
                dates.append(f"Finished  {card.finished_utc}")
            dates.append(
                f"Historical review  {'recorded' if card.reviewed else 'not marked reviewed'}"
            )
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
            self.connection_status.set(
                f"{snapshot.backend} | Owner: {owner.owner if owner else 'none'} | "
                f"Active job: {snapshot.active_job or 'none'}\n"
                f"Historical review: {'present' if snapshot.needs_review else 'none'} | "
                f"Current ownership uncertainty: {'yes' if snapshot.ownership_uncertain else 'no'}"
            )
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
        self.log_status.set(message)
        replace_text(self.log_text, "\n".join(self.logs.lines), follow=True)

    def _controls(self) -> None:
        ready = self._actions_ready()
        card = self.state.selected_card
        self.log_button.configure(
            state="normal"
            if ready and card and logs_applicable(card.test) and not self.log_requests.pending
            else "disabled"
        )
        self.preview_button.configure(
            state="normal"
            if ready and self.preview_state.can_read(time.monotonic())
            else "disabled"
        )
        self.job_cancel.configure(
            state="normal" if ready and card and card.cancel_available else "disabled"
        )
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
                    self.preview_area.grid(row=5, sticky="w", pady=(8, 0))
            except Exception:
                self.preview_state = PreviewState(
                    self.preview_state.generation,
                    self.preview_state.epoch,
                    self.preview_state.row,
                    message=FAILED,
                )
        if self.preview_state.preview is None:
            self.preview_image.configure(image="")
            self.preview_area.grid_remove()
            self.preview_photo = None
            self.displayed_preview = None
        self.preview_status.set(self.preview_state.message)

    def _run_action(
        self, action: Any, *, diagnostic: bool = False, queue_operation: bool = False
    ) -> None:
        if self.action_pending or self._is_closing() or self.state.connection == "closed":
            return
        self.action_pending = True
        self.action_status.set("Request pending...")
        self._controls()

        def worker() -> None:
            try:
                value = action()
            except Exception:
                value = "Request outcome unknown. Reconnect before any explicit retry."
            kind = "diagnostic" if diagnostic else "queue" if queue_operation else "action"
            self.action_results.put((kind, value))

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
        self._run_action(
            lambda: (
                mcp_catalog_summary(self.api.catalog())
                if catalog
                else http_diagnostics_summary(self.api.diagnostics())
            ),
            diagnostic=True,
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
