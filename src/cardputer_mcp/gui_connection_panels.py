"""Independent read-only connection diagnostics; the caller owns all external IO."""

import sys
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any, Literal

MAX_RESULT_CHARACTERS = 65536
TITLES = {"mcp": "MCP info", "http": "HTTP diagnostics"}


def received_timestamp(value: datetime | str | None) -> str:
    """Format an actual supplied receipt time; never manufacture a fresh timestamp."""
    try:
        observed = datetime.fromisoformat(value) if isinstance(value, str) else value
        if observed is None or observed.tzinfo is None or observed.utcoffset() is None:
            return "Received time unavailable"
        return "Received: " + observed.astimezone(UTC).isoformat(timespec="seconds").replace(
            "+00:00", "Z"
        )
    except (TypeError, ValueError, OverflowError):
        return "Received time unavailable"


@dataclass(frozen=True)
class DiagnosticOutcome:
    text: str | None
    received_utc: datetime | None = None


@dataclass(frozen=True)
class PanelState:
    phase: Literal["idle", "loading", "available", "empty", "error"] = "idle"
    text: str = ""
    received: str = "Not received yet"

    @property
    def message(self) -> str:
        return {
            "idle": "Not read yet.",
            "loading": "Reading...",
            "available": "Information received.",
            "empty": "No information returned.",
            "error": "Unable to read information.",
        }[self.phase]


@dataclass
class PanelWidgets:
    frame: Any
    body: Any
    toggle: Any
    refresh: Any
    copy: Any
    text: Any
    status: Any
    timestamp: Any
    state: PanelState = field(default_factory=PanelState)
    expanded: bool = False


class DiagnosticPanels:
    def __init__(self, parent: Any, request: Callable[[str], None]) -> None:
        import tkinter as tk
        from tkinter import ttk

        self.request = request
        self.enabled = True
        self.frame = ttk.Frame(parent)
        self.frame.columnconfigure(0, weight=1)
        self.panels: dict[str, PanelWidgets] = {}
        self.widgets = self.panels
        for row, kind in enumerate(TITLES):
            frame = ttk.Frame(self.frame)
            frame.grid(row=row, column=0, sticky="ew", pady=(0, 10))
            frame.columnconfigure(0, weight=1)
            header = ttk.Frame(frame)
            header.grid(row=0, column=0, sticky="ew")
            header.columnconfigure(0, weight=1)
            toggle = ttk.Button(header, command=self._command(kind, self.toggle))
            toggle.grid(row=0, column=0, sticky="w")
            refresh = ttk.Button(header, text="Refresh", command=self._command(kind, self.refresh))
            refresh.grid(row=0, column=1, sticky="e", padx=(8, 4))
            copy = ttk.Button(header, text="Copy", command=self._command(kind, self.copy))
            copy.grid(row=0, column=2, sticky="e")
            body = ttk.Frame(frame)
            body.grid(row=1, column=0, sticky="ew", pady=(6, 0))
            body.columnconfigure(0, weight=1)
            timestamp = ttk.Label(body, anchor="w", justify="left")
            timestamp.grid(row=0, column=0, columnspan=2, sticky="ew")
            status = ttk.Label(body, anchor="w", justify="left")
            status.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(2, 4))
            text = tk.Text(
                body,
                width=1,
                height=7,
                wrap="word",
                state="disabled",
                font="TkFixedFont",
                cursor="xterm",
                takefocus=True,
                exportselection=False,
                padx=8,
                pady=6,
            )
            text.grid(row=2, column=0, sticky="nsew")
            scrollbar = ttk.Scrollbar(body, orient="vertical", command=text.yview)
            scrollbar.grid(row=2, column=1, sticky="ns")
            text.configure(yscrollcommand=scrollbar.set)
            select_events = ["<Control-a>"]
            copy_events = ["<Control-c>"]
            if sys.platform == "darwin":
                select_events.append("<Command-a>")
                copy_events.append("<Command-c>")
            for event in select_events:
                text.bind(event, self._event(kind, self.select_all))
            for event in copy_events:
                text.bind(event, self._event(kind, self.copy))
            frame.bind("<Configure>", self._resize_event(kind), add="+")
            self.panels[kind] = PanelWidgets(
                frame, body, toggle, refresh, copy, text, status, timestamp
            )
            self._render(kind)

    @staticmethod
    def _command(kind: str, action: Callable[[str], object]) -> Callable[[], object]:
        def callback() -> object:
            return action(kind)

        return callback

    @staticmethod
    def _event(kind: str, action: Callable[[str], str]) -> Callable[[Any], str]:
        def callback(_event: Any) -> str:
            return action(kind)

        return callback

    def _resize_event(self, kind: str) -> Callable[[Any], None]:
        def callback(event: Any) -> None:
            self._resize(kind, event.width)
        return callback

    def _panel(self, kind: str) -> PanelWidgets:
        if kind not in self.panels:
            raise ValueError("unknown_diagnostic_panel")
        return self.panels[kind]

    def _render(self, kind: str) -> None:
        panel = self._panel(kind)
        marker = "▾" if panel.expanded else "▸"
        panel.toggle.configure(text=f"{marker} {TITLES[kind]}")
        if panel.expanded:
            panel.body.grid()
        else:
            panel.body.grid_remove()
        panel.status.configure(text=panel.state.message)
        panel.timestamp.configure(text=panel.state.received)
        panel.refresh.configure(
            state="normal" if self.enabled and panel.state.phase != "loading" else "disabled"
        )
        panel.copy.configure(state="normal" if panel.state.text else "disabled")
        if panel.text.get("1.0", "end-1c") != panel.state.text:
            panel.text.configure(state="normal")
            panel.text.delete("1.0", "end")
            panel.text.insert("1.0", panel.state.text)
        panel.text.configure(state="disabled")

    def _resize(self, kind: str, width: int) -> None:
        panel = self._panel(kind)
        wrap = max(80, width - 24)
        panel.status.configure(wraplength=wrap)
        panel.timestamp.configure(wraplength=wrap)

    def toggle(self, kind: str) -> None:
        panel = self._panel(kind)
        panel.expanded = not panel.expanded
        self._render(kind)

    def refresh(self, kind: str) -> None:
        panel = self._panel(kind)
        if not self.enabled or panel.state.phase == "loading":
            return
        self.show_loading(kind)
        try:
            self.request(kind)
        except Exception:
            # Raw transport exceptions can contain credentials. The caller can
            # supply a sanitized diagnostic later via show_error when appropriate.
            self.show_error(kind)

    def show_loading(self, kind: str) -> None:
        panel = self._panel(kind)
        panel.state = PanelState("loading")
        panel.expanded = True
        self._render(kind)

    def show_result(self, kind: str, text: str, received_utc: datetime | str | None) -> None:
        panel = self._panel(kind)
        bounded = text[:MAX_RESULT_CHARACTERS]
        panel.state = PanelState(
            "available" if bounded else "empty", bounded, received_timestamp(received_utc)
        )
        panel.expanded = True
        self._render(kind)

    def show_error(
        self,
        kind: str,
        message: str = "Information unavailable.",
        received_utc: datetime | str | None = None,
    ) -> None:
        panel = self._panel(kind)
        panel.state = PanelState(
            "error", message[:MAX_RESULT_CHARACTERS], received_timestamp(received_utc)
        )
        panel.expanded = True
        self._render(kind)

    def set_enabled(self, enabled: bool) -> None:
        self.enabled = enabled
        for kind in self.panels:
            self._render(kind)

    def clear(self, kind: str | None = None) -> None:
        kinds = tuple(self.panels) if kind is None else (kind,)
        for name in kinds:
            panel = self._panel(name)
            panel.state = PanelState()
            self._render(name)

    def select_all(self, kind: str) -> str:
        panel = self._panel(kind)
        panel.text.tag_add("sel", "1.0", "end-1c")
        panel.text.mark_set("insert", "1.0")
        return "break"

    def copy(self, kind: str) -> str:
        panel = self._panel(kind)
        if not panel.state.text:
            return "break"
        text = panel.state.text
        try:
            selected = panel.text.tag_ranges("sel")
            if selected:
                text = panel.text.get("sel.first", "sel.last")
            panel.text.clipboard_clear()
            panel.text.clipboard_append(text)
        except Exception:
            panel.state = replace(panel.state, phase="error")
            panel.status.configure(text="Copy unavailable.")
        return "break"
