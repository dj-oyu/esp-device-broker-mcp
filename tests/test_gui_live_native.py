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
        self.images = []

    def start(self, generation):
        self.generation = generation

    def drain(self):
        events, self.events = self.events, []
        return events

    def read_logs(self, generation, job_id, cursor):
        self.reads.append((generation, job_id, cursor))
        return len(self.reads)

    def read_image(self, generation, job_id, *, expires_at=None):
        self.images.append((generation, job_id))
        return len(self.images)

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
        assert window.detail_heading.get() == "a | running"
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


def test_native_preview_button_displays_validated_synthetic_pixels_and_expires(monkeypatch):
    import tkinter as tk

    from test_gui_images import detail, row
    from test_gui_timeline import card as image_card

    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("Native Tk requires a desktop display")
    root.withdraw()
    monkeypatch.setattr(gui_live, "LiveRunner", Runner)
    now = [time.monotonic()]
    monkeypatch.setattr(gui_live.time, "monotonic", lambda: now[0])
    window = None
    try:
        window = gui_live.LiveWindow(root, OperatorClient("http://127.0.0.1:8765", "synthetic"))
        generation = window.state.generation
        data = detail(remaining_seconds=5)
        window.runner.events.append(
            TransportEvent(
                "message",
                generation,
                snapshot([image_card(image=row(data))]),
                now[0],
            )
        )
        window._drain()
        window.job_tree.selection_set("job_image_1")
        window._select()
        root.update_idletasks()
        assert window.preview_button.instate(["!disabled"])
        window.preview_button.invoke()
        assert window.runner.images == [(generation, "job_image_1")]
        assert window.preview_state.pending and not window.action_pending
        window.runner.events.append(
            TransportEvent(
                "image",
                generation,
                {
                    "job_id": "job_image_1",
                    "request_id": 1,
                    "response": {**data, "broker_epoch": "e" * 32},
                },
                now[0],
            )
        )
        window._drain()
        root.update_idletasks()
        assert window.preview_photo is not None
        assert (window.preview_photo.width(), window.preview_photo.height()) == (240, 135)
        assert window.preview_image.cget("image") == str(window.preview_photo)
        assert "TEST / NOT DEVICE" in window.preview_status.get()
        name = str(window.preview_photo)
        assert name in root.tk.call("image", "names")
        now[0] += 5
        window._drain()
        assert window.preview_photo is None and window.preview_state.preview is None
        assert window.preview_image.cget("image") == ""
        assert name not in root.tk.call("image", "names")
    finally:
        if window is not None and window.state.connection != "closed":
            window._finish_close()
        else:
            try:
                root.destroy()
            except tk.TclError:
                pass


def test_native_job_heading_ascii_controls_and_nonlog_empty_loading_states(monkeypatch):
    import tkinter as tk

    from test_gui_timeline import card as image_card

    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("Native Tk requires a desktop display")
    monkeypatch.setattr(gui_live, "LiveRunner", Runner)
    window = None
    try:
        window = gui_live.LiveWindow(root, OperatorClient("http://127.0.0.1:8765", "synthetic"))
        root.geometry("860x600")
        generation = window.state.generation
        window.runner.events.append(
            TransportEvent(
                "message",
                generation,
                snapshot([image_card(), card("build")]),
                time.monotonic(),
            )
        )
        window._drain()
        window.job_tree.selection_set("job_image_1")
        window._select()
        root.update()
        assert window.detail_heading.get() == "job_image_1 | succeeded"
        assert window.detail_heading_label.cget("style") == "Section.TLabel"
        assert window.detail_text.cget("font") == "TkTextFont"
        body = window.detail_text.get("1.0", "end-1c")
        assert "Stage  " not in body and "Result  " not in body and "Finished  " not in body
        assert "Created  " in body and "Historical review  " in body
        assert window.logs.phase == "not_applicable"
        assert window.log_button.instate(["disabled"]) and window.runner.reads == []
        assert window.preview_button.cget("text") == "Preview retained image"
        assert window.job_cancel.cget("text") == "Cancel job..."
        assert window.preview_button.winfo_width() >= window.preview_button.winfo_reqwidth()
        assert (
            window.preview_button.winfo_x() + window.preview_button.winfo_width()
            <= window.preview_button.master.winfo_width()
        )
        window.job_tree.selection_set("build")
        window._select()
        assert window.runner.reads[-1][1] == "build"
        assert window.log_status.get() == "Reading permitted output..."
        assert window.detail_heading.get() == "build | running"
        window.runner.events.append(
            TransportEvent(
                "logs",
                generation,
                {
                    "job_id": "build",
                    "request_id": 1,
                    "response": {
                        "ok": True,
                        "entries": [],
                        "next_cursor": {"epoch": "a" * 32, "sequence": 0},
                        "gap": False,
                        "expired": False,
                        "has_more": False,
                    },
                },
                time.monotonic(),
            )
        )
        window._drain()
        assert window.logs.phase == "empty" and window.log_status.get().startswith("No output")
        assert window.log_button.instate(["!disabled"])
        fixed_text = [
            window.preview_button.cget("text"),
            window.job_cancel.cget("text"),
            window.log_status.get(),
            window.preview_status.get(),
            window.connection_status.get(),
        ]
        assert all(str(value).isascii() for value in fixed_text)
    finally:
        if window is not None and window.state.connection != "closed":
            window._finish_close()
        else:
            try:
                root.destroy()
            except tk.TclError:
                pass
