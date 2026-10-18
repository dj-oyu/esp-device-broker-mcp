"""Native Tk smoke where a desktop exists; never connects to a broker or device."""

import time

import pytest
from test_gui_live_state import card, delta, snapshot

from cardputer_mcp import gui_live
from cardputer_mcp.gui_live_transport import TransportEvent
from cardputer_mcp.operator_client import OperatorClient


class Runner:
    def __init__(self, api):
        self.events, self.reads = [], []
        self.closed = False

    def start(self, generation):
        self.generation = generation

    def drain(self):
        events, self.events = self.events, []
        return events

    def read_logs(self, generation, job_id, cursor):
        self.reads.append((generation, job_id, cursor))
        return len(self.reads)

    def close(self):
        self.closed = True


def test_native_job_selection_delta_log_navigation_and_close(monkeypatch):
    import tkinter as tk

    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("Native Tk requires a desktop display")
    root.withdraw()
    monkeypatch.setattr(gui_live, "LiveRunner", Runner)
    window = None
    try:
        window = gui_live.LiveWindow(root, OperatorClient("http://127.0.0.1:8765", "synthetic"))
        generation = window.state.generation
        window.runner.events.append(
            TransportEvent(
                "message", generation, snapshot([card("a"), card("b")]), time.monotonic()
            )
        )
        window._drain()
        window.job_tree.selection_set("a")
        window.job_tree.focus("a")
        window._select()
        assert window.runner.reads[-1][1] == "a"
        assert "a\n" in window.detail_text.get("1.0", "end-1c")
        window.runner.events.extend(
            [
                TransportEvent(
                    "message",
                    generation,
                    delta(order=["b", "a"], upsert=[card("a", stage="idf_diagnostics")]),
                    time.monotonic(),
                ),
                TransportEvent(
                    "logs",
                    generation,
                    {
                        "job_id": "a",
                        "request_id": 1,
                        "response": {
                            "ok": True,
                            "entries": [{"sequence": 1, "text": "日本語と Latin output"}],
                            "next_cursor": {"epoch": "a" * 32, "sequence": 1},
                            "gap": False,
                            "expired": False,
                            "has_more": False,
                        },
                    },
                    time.monotonic(),
                ),
            ]
        )
        window._drain()
        root.update_idletasks()
        assert window.job_tree.get_children() == ("b", "a")
        assert window.job_tree.selection() == ("a",) and window.job_tree.focus() == "a"
        assert "日本語と Latin output" in window.log_text.get("1.0", "end-1c")
        assert window.job_tree.item("a", "values")[2] == "idf_diagnostics"
        window._finish_close()
        assert window.runner.closed
    finally:
        if window is not None and window.state.connection != "closed":
            window._finish_close()
        else:
            try:
                root.destroy()
            except tk.TclError:
                pass
