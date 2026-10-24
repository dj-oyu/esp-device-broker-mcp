"""Connection diagnostics remain independent, explicit and safe without IO."""

from datetime import datetime, timedelta, timezone

import pytest

from cardputer_mcp.gui_connection_panels import (
    TITLES,
    DiagnosticPanels,
    PanelWidgets,
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
