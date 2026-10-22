"""Connection diagnostics remain independent, explicit and safe without IO."""

from datetime import UTC, datetime, timedelta, timezone

import pytest

from cardputer_mcp.gui_connection_panels import (
    MAX_RESULT_CHARACTERS,
    TITLES,
    DiagnosticPanels,
    PanelState,
    PanelWidgets,
    received_timestamp,
)


class Widget:
    def __init__(self):
        self.config = {}
        self.visible = False
        self.content = ""
        self.selected = ""
        self.clipboard = []
        self.rewrites = 0

    def configure(self, **values):
        self.config.update(values)

    def grid(self):
        self.visible = True

    def grid_remove(self):
        self.visible = False

    def delete(self, *_):
        assert self.config["state"] == "normal"
        self.content = ""
        self.rewrites += 1

    def insert(self, _index, text):
        assert self.config["state"] == "normal"
        self.content = text

    def get(self, start, _end):
        return self.selected if start == "sel.first" else self.content

    def tag_ranges(self, _name):
        return ("1.0", "1.4") if self.selected else ()

    def tag_add(self, *_):
        self.selected = self.content

    def mark_set(self, *_):
        pass

    def clipboard_clear(self):
        self.clipboard.clear()

    def clipboard_append(self, text):
        self.clipboard.append(text)


def panels(request=None):
    value = DiagnosticPanels.__new__(DiagnosticPanels)
    value.request = request or (lambda _: None)
    value.enabled = True
    value.panels = {kind: PanelWidgets(*(Widget() for _ in range(8))) for kind in TITLES}
    value.widgets = value.panels
    for kind in value.panels:
        value._render(kind)
    return value


def test_request_is_explicit_and_loading_one_panel_does_not_change_the_other():
    calls = []
    value = panels(calls.append)
    assert calls == [] and all(not panel.expanded for panel in value.panels.values())
    value.refresh("mcp")
    assert calls == ["mcp"]
    assert value.panels["mcp"].state.phase == "loading"
    assert value.panels["mcp"].expanded and value.panels["mcp"].body.visible
    assert value.panels["http"].state == PanelState() and not value.panels["http"].expanded
    value.refresh("mcp")
    assert calls == ["mcp"]
    value.set_enabled(False)
    value.refresh("http")
    assert calls == ["mcp"]
    value.toggle("http")
    assert value.panels["http"].body.visible


def test_results_have_actual_supplied_utc_time_and_copy_requires_explicit_action():
    value = panels()
    observed = datetime(2026, 10, 22, 10, 30, tzinfo=timezone(timedelta(hours=9)))
    value.show_result("mcp", "MCP response", observed)
    panel = value.panels["mcp"]
    assert panel.state.received == "Received: 2026-10-22T01:30:00Z"
    assert panel.text.content == "MCP response" and panel.text.config["state"] == "disabled"
    assert panel.text.clipboard == [] and panel.copy.config["state"] == "normal"
    value.copy("mcp")
    assert panel.text.clipboard == ["MCP response"]
    panel.text.selected = "response"
    value.copy("mcp")
    assert panel.text.clipboard == ["response"]
    assert value.panels["http"].state.phase == "idle"


def test_collapse_enable_and_resize_preserve_current_text_and_selection():
    value = panels()
    value.show_result("http", "HTTP response", "2026-10-22T10:45:00Z")
    panel = value.panels["http"]
    panel.text.selected = "HTTP"
    rewrites = panel.text.rewrites
    value.toggle("http")
    value.set_enabled(False)
    value._resize("http", 280)
    assert not panel.body.visible and panel.text.selected == "HTTP"
    assert panel.text.rewrites == rewrites
    assert panel.status.config["wraplength"] == panel.timestamp.config["wraplength"] == 256
    assert panel.refresh.config["state"] == "disabled" and panel.copy.config["state"] == "normal"


def test_loading_empty_error_and_clear_states_are_distinct_and_bounded():
    value = panels()
    value.show_result("http", "", "2026-10-22T11:00:00Z")
    panel = value.panels["http"]
    assert panel.state.phase == "empty" and panel.copy.config["state"] == "disabled"
    assert panel.state.received.endswith("11:00:00Z")
    value.show_error("http", "Read unavailable.")
    assert panel.state.phase == "error" and panel.state.received == "Received time unavailable"
    assert panel.text.content == "Read unavailable."
    value.show_result("http", "x" * (MAX_RESULT_CHARACTERS + 20), None)
    assert len(panel.text.content) == MAX_RESULT_CHARACTERS
    value.show_loading("http")
    assert panel.state.phase == "loading" and not panel.text.content
    assert panel.state.received == "Not received yet"
    value.clear("http")
    assert panel.state == PanelState()
    messages = {PanelState().message, PanelState("empty").message, PanelState("error").message}
    assert len(messages) == 3


def test_callback_exception_is_not_reflected_and_unknown_kind_is_rejected():
    def fail(_kind):
        raise RuntimeError("SYNTHETIC_CREDENTIAL_MUST_NOT_BE_RENDERED")

    value = panels(fail)
    value.refresh("mcp")
    panel = value.panels["mcp"]
    assert panel.state.phase == "error" and panel.state.text == "Information unavailable."
    assert "SYNTHETIC" not in panel.state.text
    with pytest.raises(ValueError, match="unknown_diagnostic_panel"):
        value.show_loading("other")


@pytest.mark.parametrize("value", [None, "invalid", "2026-10-22T10:00:00", datetime(2026, 10, 22)])
def test_missing_or_naive_receipt_times_are_not_replaced_by_current_time(value):
    assert received_timestamp(value) == "Received time unavailable"


def test_utc_receipt_string_is_preserved_without_a_clock_read():
    assert received_timestamp("2026-10-22T10:45:00+00:00") == "Received: 2026-10-22T10:45:00Z"
    assert received_timestamp(datetime(2026, 10, 22, 10, 45, tzinfo=UTC)).endswith("10:45:00Z")


def test_native_panels_are_read_only_selectable_collapsible_and_wrap_without_horizontal_bar():
    try:
        import tkinter as tk
    except ImportError:
        pytest.skip("Native Tk is unavailable")
    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("Native Tk requires a desktop display")
    root.withdraw()
    try:
        calls = []
        value = DiagnosticPanels(root, calls.append)
        value.frame.pack(fill="both", expand=True)
        value.show_result("mcp", "Diagnostic result 日本語 " * 30, "2026-10-22T10:50:00Z")
        panel = value.panels["mcp"]
        original = panel.text.get("1.0", "end-1c")
        panel.text.insert("1.0", "must not edit")
        assert panel.text.get("1.0", "end-1c") == original
        assert panel.text.cget("state") == "disabled" and panel.text.cget("wrap") == "word"
        assert panel.text.cget("xscrollcommand") == "" and int(panel.text.cget("width")) == 1
        value.select_all("mcp")
        assert panel.text.tag_ranges("sel")
        value.copy("mcp")
        assert root.clipboard_get() == original
        value.toggle("mcp")
        assert not panel.expanded
        value.panels["http"].refresh.invoke()
        assert calls == ["http"] and value.panels["http"].state.phase == "loading"
        value.set_enabled(False)
        assert str(value.panels["mcp"].refresh.cget("state")) == "disabled"
    finally:
        root.destroy()
