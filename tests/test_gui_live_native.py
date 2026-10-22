"""Native Tk smoke where a desktop exists; never connects to a broker or device."""

import gc
import time
from types import SimpleNamespace

import pytest
from test_gui_live_state import card, delta, snapshot

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
        assert window.detail_heading.get() == "a | ⏳ Running"
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
        assert window.job_tree.item("a", "values")[3] == "idf_diagnostics"
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


def test_native_preview_and_controls_reachable_at_168_dpi_minimum(monkeypatch):
    import tkinter as tk

    from test_gui_images import detail, row
    from test_gui_timeline import card as image_card

    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("Native Tk requires a desktop display")
    original_scaling = root.tk.call("tk", "scaling")
    root.tk.call("tk", "scaling", 168 / 72)
    monkeypatch.setattr(gui_live, "LiveRunner", Runner)
    window = None
    try:
        window = gui_live.LiveWindow(root, OperatorClient("http://127.0.0.1:8765", "synthetic"))
        root.geometry("860x600")
        job_id = "capture_" + "long_context_" * 4
        data = detail(remaining_seconds=5, job_id=job_id)
        generation = window.state.generation
        window.runner.events.append(
            TransportEvent(
                "message",
                generation,
                snapshot([image_card(job_id=job_id, image=row(data))]),
                time.monotonic(),
            )
        )
        window._drain()
        window.job_tree.selection_set(job_id)
        window._select()
        window._show_selected()
        window.runner.events.append(
            TransportEvent(
                "image",
                generation,
                {
                    "job_id": job_id,
                    "request_id": 1,
                    "response": {**data, "broker_epoch": "e" * 32},
                },
                time.monotonic(),
            )
        )
        window._drain()
        root.update()
        assert (root.winfo_width(), root.winfo_height()) == (860, 600)
        assert window._job_pages is None and window._detail_canvas is not None
        assert str(window.job_panes.cget("orient")) == "horizontal"
        assert window.job_cancel.winfo_manager() == ""
        assert window.preview_photo is not None
        assert window._detail_canvas.yview()[0] == 0.0
        window._scroll_detail_key("End")
        root.update()
        assert window._detail_canvas.yview()[1] == 1.0
        for widget in (window.preview_area, window.preview_status_label):
            assert widget.winfo_rooty() >= root.winfo_rooty()
            assert widget.winfo_rooty() + widget.winfo_height() <= root.winfo_rooty() + 600
        window.back_button.invoke()
        root.update()
        assert window.job_tree.winfo_width() > 1
        assert window.job_tree.winfo_rootx() + window.job_tree.winfo_width() <= (
            root.winfo_rootx() + 860
        )
        # Explicit scrolling leaves the detail canvas at the bottom. Reopen
        # through the list affordance and check the actual canvas viewport.
        window._show_selected()
        root.update()
        canvas = window._detail_canvas
        assert canvas.yview()[0] == 0.0
        assert window.detail_text.winfo_rooty() >= canvas.winfo_rooty()
        assert window.detail_text.winfo_rooty() + window.detail_text.winfo_height() <= (
            canvas.winfo_rooty() + canvas.winfo_height()
        )
        # Headless Windows runners do not reliably dispatch synthetic key
        # events to a disabled Text. Check the installed bindings and their
        # shared handler against the real Tk canvas viewport.
        for key in ("Prior", "Next", "Home", "End"):
            assert window.detail_text.bind(f"<{key}>")
        assert window._scroll_detail_key("Next") == "break"
        root.update()
        assert canvas.yview()[0] > 0.0
        assert window._scroll_detail_key("Home") == "break"
        root.update()
        assert canvas.yview()[0] == 0.0
        assert window._scroll_detail_key("End") == "break"
        root.update()
        assert canvas.yview()[1] == 1.0
        assert window.preview_area.winfo_rooty() >= canvas.winfo_rooty()
        assert window.preview_area.winfo_rooty() + window.preview_area.winfo_height() <= (
            canvas.winfo_rooty() + canvas.winfo_height()
        )
        assert window.log_text.bind("<FocusIn>")
        window._reveal_detail_focus(SimpleNamespace(widget=window.log_text))
        root.update()
        assert window.log_text.winfo_rooty() >= canvas.winfo_rooty()
        assert window.log_text.winfo_rooty() + window.log_text.winfo_height() <= (
            canvas.winfo_rooty() + canvas.winfo_height()
        )
        window.back_button.invoke()
        root.update()
        assert window.job_tree.bind("<Return>")
        window._show_selected()
        root.update()
        assert window.job_tree.selection() == (job_id,)
        assert window.job_tree.winfo_ismapped()
        assert window.detail_text.winfo_rooty() >= canvas.winfo_rooty()
        assert window.detail_text.winfo_rooty() + window.detail_text.winfo_height() <= (
            canvas.winfo_rooty() + canvas.winfo_height()
        )
    finally:
        root.tk.call("tk", "scaling", original_scaling)
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
        assert window.detail_heading.get() == "job_image_1 | ✅ Succeeded"
        assert window.detail_heading_label.cget("style") == "Section.TLabel"
        assert window.detail_text.cget("font") == "TkTextFont"
        body = window.detail_text.get("1.0", "end-1c")
        assert "Stage  " not in body and "Result  " not in body and "Finished  " not in body
        assert "Registered  " in body and "Historical acknowledgement  " in body
        assert window.logs.phase == "not_applicable"
        assert window.log_marker.winfo_class() == "TLabel" and window.runner.reads == []
        assert window.image_context.get().startswith("Captured image |")
        assert window.job_cancel.cget("text") == "Cancel job..."
        assert window.copy_button.winfo_width() >= window.copy_button.winfo_reqwidth()
        assert window.job_cancel.winfo_manager() == ""
        window.job_tree.selection_set("build")
        window._select()
        assert window.runner.reads[-1][1] == "build"
        assert window.job_cancel.grid_info()["row"] > window.preview_status_label.grid_info()["row"]
        assert "Reading permitted output..." in window.log_text.get("1.0", "end-1c")
        assert window.detail_heading.get() == "build | ⏳ Running"
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
        assert window.logs.phase == "empty"
        assert window.log_text.get("1.0", "end-1c").startswith("No output")
        assert window.copy_button.instate(["!disabled"])
        fixed_text = [
            window.image_context.get(),
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


def test_native_status_backgrounds_and_review_labels_preserve_selection(monkeypatch):
    import tkinter as tk

    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("Native Tk requires a desktop display")
    root.withdraw()
    monkeypatch.setattr(gui_live, "LiveRunner", Runner)
    monkeypatch.setattr("cardputer_mcp.gui_job_status.high_contrast_enabled", lambda: False)
    window = None
    try:
        window = gui_live.LiveWindow(root, OperatorClient("http://127.0.0.1:8765", "synthetic"))
        rows = [
            card("failed", cancel_available=False),
            card(
                "unreviewed",
                cancel_available=False,
                result="serial_fault_marker",
                failure_reason="app_load_out_of_memory",
                backend="serial",
                test="derby_fixed_workload",
                stage="derby_fixed_workload",
            ),
            card("reviewed", cancel_available=False, result="serial_fault_marker"),
            card("uncertain", cancel_available=False, result="serial_close_unconfirmed"),
        ]
        rows[0]["state"] = "failed"
        for row in rows[1:]:
            row["state"] = "needs_review"
        rows[2]["reviewed"] = True
        window.runner.events.append(
            TransportEvent("message", window.state.generation, snapshot(rows), time.monotonic())
        )
        window._drain()
        assert window.state.connection == "live"
        failed_color = window.job_tree.tag_configure("job_failed", "background")
        assert str(failed_color) == "#FDE8E7"
        review_color = window.job_tree.tag_configure("job_unknown", "background")
        assert str(review_color) == "#FFF0CC"
        for job in ("failed", "unreviewed", "reviewed", "uncertain"):
            window.job_tree.selection_set(job)
            window.job_tree.focus(job)
            window._select()
            window._render()
            values = window.job_tree.item(job, "values")
            assert values[1] == ("❔ Unknown" if job == "uncertain" else "❌ Failed")
            from tkinter import font, ttk

            status_font = font.nametofont("TkDefaultFont", root=root)
            assert int(window.job_tree.column("state", "minwidth")) >= (
                status_font.measure(values[1]) + 16
            )
            assert int(window.job_tree.column("confirmation", "minwidth")) >= (
                status_font.measure(values[2]) + 16
            )
            assert int(ttk.Style(root).lookup("Treeview", "rowheight")) >= (
                status_font.metrics("linespace")
            )
            assert window.job_tree.selection() == (job,)
            assert window.job_tree.focus() == job
        window.job_tree.selection_set("unreviewed")
        window._select()
        assert "Reason: App load failed: out of memory observed" in window.detail_heading.get()
        assert "Not reviewed" in window.job_tree.item("unreviewed", "values")[2]
        assert "Reviewed" in window.job_tree.item("reviewed", "values")[2]
        for job in ("unreviewed", "reviewed"):
            assert tuple(map(str, window.job_tree.item(job, "tags"))) == ("job_failed",)
        assert tuple(map(str, window.job_tree.item("uncertain", "tags"))) == ("job_unknown",)
    finally:
        if window is not None:
            window.runner.close()
            if window.timer is not None:
                root.after_cancel(window.timer)
        root.destroy()


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


def test_both_connection_panels_are_reachable_at_minimum_high_dpi(monkeypatch):
    import tkinter as tk

    from gui_screenshot import save_mock_window

    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("Native Tk requires a desktop display")
    original = root.tk.call("tk", "scaling")
    root.tk.call("tk", "scaling", 168 / 72)
    monkeypatch.setattr(gui_live, "LiveRunner", Runner)
    window = None
    try:
        window = gui_live.LiveWindow(root, OperatorClient("http://127.0.0.1:8765", "synthetic"))
        root.geometry("860x600")
        window.tabs.select(2)
        for kind in ("mcp", "http"):
            window.connection_panels.show_result(
                kind, "Synthetic diagnostic\n" * 8, "2026-10-22T10:00:00+00:00"
            )
        root.update()
        canvas = window.connection_canvas
        assert canvas.yview()[1] < 1.0
        canvas.yview_moveto(1.0)
        root.update()
        text = window.connection_panels.panels["http"].text
        assert text.winfo_rooty() >= canvas.winfo_rooty()
        assert (
            text.winfo_rooty() + text.winfo_height() <= canvas.winfo_rooty() + canvas.winfo_height()
        )
        assert window.connection_panels.panels["mcp"].expanded
        assert window.connection_panels.panels["http"].expanded
        save_mock_window(root, "connection-both-panels-minimum")
    finally:
        root.tk.call("tk", "scaling", original)
        if window is not None:
            window._finish_close()
        else:
            root.destroy()
