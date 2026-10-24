"""Native Tk smoke where a desktop exists; never connects to a broker or device."""

import gc
import time

import pytest
from test_gui_live_state import card, snapshot

from cardputer_mcp import gui_live
from cardputer_mcp.gui_live_transport import TransportEvent
from cardputer_mcp.operator_client import OperatorClient


@pytest.fixture(autouse=True)
def collect_tk_objects_on_main_thread():
    yield
    # Tk variable finalizers must not run later on a transport worker thread.
    gc.collect()


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

    def pause(self, generation):
        self.generation = generation

    def close(self):
        self.closed = True


@pytest.mark.parametrize("job_id", ["job_image_1", "capture_" + "long_context_" * 4])
def test_native_selection_displays_validated_synthetic_pixels_and_expires(monkeypatch, job_id):
    import tkinter as tk

    from test_gui_images import detail, row
    from test_gui_timeline import card as image_card

    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("Native Tk requires a desktop display")
    monkeypatch.setattr(gui_live, "LiveRunner", Runner)
    now = [time.monotonic()]
    monkeypatch.setattr(gui_live.time, "monotonic", lambda: now[0])
    window = None
    try:
        window = gui_live.LiveWindow(root, OperatorClient("http://127.0.0.1:8765", "synthetic"))
        root.geometry("860x600")
        generation = window.state.generation
        data = detail(remaining_seconds=5, job_id=job_id)
        window.runner.events.append(
            TransportEvent(
                "message",
                generation,
                snapshot([image_card(job_id=job_id, image=row(data))]),
                now[0],
            )
        )
        window._drain()
        window.job_tree.selection_set(job_id)
        window._select()
        root.update_idletasks()
        assert window.image_marker.winfo_class() == "TLabel"
        assert window.runner.images == [(generation, job_id)]
        assert window.preview_state.pending and not window.action_pending
        window.runner.events.append(
            TransportEvent(
                "image",
                generation,
                {
                    "job_id": job_id,
                    "request_id": 1,
                    "response": {**data, "broker_epoch": "e" * 32},
                },
                now[0],
            )
        )
        window._drain()
        root.update()
        window._scroll_detail_key("End")
        root.update()
        assert (root.winfo_width(), root.winfo_height()) == (860, 600)
        assert window.preview_photo is not None
        assert (window.preview_photo.width(), window.preview_photo.height()) == (240, 135)
        assert window.preview_image.cget("image") == str(window.preview_photo)
        assert "TEST / NOT DEVICE" in window.preview_status.get()
        assert window.preview_image.winfo_width() >= 240
        assert window.preview_image.winfo_height() >= 135
        for widget in (window.preview_area, window.preview_status_label, window.log_status_label):
            parent = widget.master
            assert widget.winfo_x() + widget.winfo_width() <= parent.winfo_width()
            assert widget.winfo_y() + widget.winfo_height() <= parent.winfo_height()
        for widget in (
            window.preview_area,
            window.preview_status_label,
            window.log_status_label,
            window.image_marker,
            window.job_cancel,
        ):
            assert widget.winfo_rootx() + widget.winfo_width() <= root.winfo_rootx() + 860
            assert widget.winfo_rooty() + widget.winfo_height() <= root.winfo_rooty() + 600
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


@pytest.mark.parametrize("phase", ["empty", "running", "completed", "denied", "expired", "image"])
def test_selected_result_viewer_states_copy_cancel_and_mock_screenshots(monkeypatch, phase):
    import tkinter as tk
    from tkinter import ttk

    from gui_screenshot import save_mock_window
    from test_gui_images import detail, row

    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("Native Tk requires a desktop display")
    monkeypatch.setattr(gui_live, "LiveRunner", Runner)
    now = [time.monotonic()]
    monkeypatch.setattr(gui_live.time, "monotonic", lambda: now[0])
    window = None
    try:
        window = gui_live.LiveWindow(root, OperatorClient("http://127.0.0.1:8765", "synthetic"))
        generation = window.state.generation
        data = detail(remaining_seconds=5)
        selected = card(
            "job_image_1",
            state="succeeded" if phase == "completed" else "running",
            cancel_available=phase == "running",
            test="firmware_flash" if phase == "denied" else "firmware_build",
        )
        if phase in {"expired", "image"}:
            selected.update(
                test="capture_home",
                backend="mock",
                state="succeeded",
                cancel_available=False,
                image=row(data),
            )
        window.runner.events.append(
            TransportEvent(
                "message", generation, snapshot([] if phase == "empty" else [selected]), now[0]
            )
        )
        window._drain()
        if phase != "empty":
            window.job_tree.selection_set("job_image_1")
            window._select()
            window._show_selected()
            reply = {
                "ok": True,
                "entries": [{"sequence": 1, "text": "日本語 synthetic output"}],
                "next_cursor": {"epoch": "a" * 32, "sequence": 1},
                "gap": False,
                "expired": False,
                "has_more": False,
            }
            if phase == "denied":
                reply = {"ok": False, "error": "not_authorized"}
            window.runner.events.append(
                TransportEvent(
                    "logs",
                    generation,
                    {"job_id": "job_image_1", "request_id": 1, "response": reply},
                    now[0],
                )
            )
            if phase in {"expired", "image"}:
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
            if phase == "expired":
                now[0] += 5
            window._drain()
        root.update()
        assert window.log_marker.winfo_class() == "TLabel"
        assert window.image_marker.winfo_class() == "TLabel"
        assert not any(
            child.winfo_class() == "TButton" and child.cget("text") == "Back to list"
            for child in window.detail_heading_label.master.winfo_children()
        )
        if phase == "denied":
            assert "access denied" in window.log_text.get("1.0", "end-1c")
            assert "owning client" in window.cancel_context.get()
        if phase == "expired":
            assert window.preview_photo is None and "expired" in window.preview_status.get()
        if phase == "image":
            assert "Captured " in window.image_context.get()
            assert window.preview_photo is not None and len(window.runner.images) == 1
        if phase != "empty":
            window.copy_button.invoke()
            assert "Logs |" in root.clipboard_get()
        assert window.job_cancel.winfo_manager() == ("grid" if phase == "running" else "")
        assert ttk.Style(root).lookup("Danger.TButton", "foreground", ("!disabled",)) == "#b42318"
        save_mock_window(root, phase + "-top")
        window._scroll_detail_key("End")
        root.update()
        save_mock_window(root, phase + "-result")
        if window._job_pages is not None:
            recent = window.job_tree.master.master
            assert not any(
                w.winfo_class() == "TLabel" and w.cget("text") == "Recent jobs"
                for w in recent.winfo_children()
            )
    finally:
        if window is not None:
            window._finish_close()
        else:
            root.destroy()
