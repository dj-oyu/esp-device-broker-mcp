"""Stable keyed rendering and bounded permitted-output presentation."""

import queue
import time
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from unittest.mock import Mock, patch
from xml.etree import ElementTree

import pytest
from test_gui_live_state import card, delta, ready, snapshot

from cardputer_mcp.gui import Outcome, QueueController
from cardputer_mcp.gui_live import LiveWindow
from cardputer_mcp.gui_connection_panels import DiagnosticOutcome
from cardputer_mcp.gui_live_presentation import (
    MAX_LOG_BYTES,
    MAX_LOG_LINES,
    LogRequestState,
    LogView,
    logs_applicable,
    read_log_result,
    replace_text,
    sync_rows,
)
from cardputer_mcp.gui_live_preview import PreviewState
from cardputer_mcp.gui_live_state import LiveState, begin, disconnect, select
from cardputer_mcp.gui_live_transport import TransportEvent
from cardputer_mcp.gui_timeline import CANCEL_UNCERTAIN


class Tree:
    def __init__(self):
        self.rows = {
            "a": {"text": "A", "values": ("running",)},
            "b": {"text": "B", "values": ("queued",)},
        }
        self.order = ["a", "b"]
        self.selected, self.focused = ("a",), "a"
        self.changed = []
        self.horizontal, self.vertical = (0.2, 0.7), (0.3, 0.8)

    def selection(self):
        return self.selected

    def focus(self, value=None):
        if value is not None:
            self.focused = value
        return self.focused

    def xview(self):
        return self.horizontal

    def yview(self):
        return self.vertical

    def get_children(self):
        return tuple(self.order)

    def delete(self, *items):
        self.changed.append(("delete", items))
        for item in items:
            del self.rows[item]
            self.order.remove(item)
        self.selected = tuple(item for item in self.selected if item in self.rows)

    def exists(self, item):
        return item in self.rows

    def item(self, item, **changes):
        if changes:
            self.changed.append(("item", item))
            self.rows[item].update(changes)
        return self.rows[item]

    def index(self, item):
        return self.order.index(item)

    def move(self, item, parent, position):
        self.changed.append(("move", item))
        self.order.remove(item)
        self.order.insert(position, item)

    def insert(self, parent, position, *, iid, text, values):
        self.changed.append(("insert", iid))
        self.rows[iid] = {"text": text, "values": values}
        self.order.insert(position, iid)

    def selection_set(self, items):
        self.selected = tuple(items)

    def xview_moveto(self, value):
        self.horizontal = (value, self.horizontal[1])

    def yview_moveto(self, value):
        self.vertical = (value, self.vertical[1])


def test_delta_changes_only_affected_row_and_preserves_selection_focus_scroll():
    tree = Tree()
    sync_rows(tree, [("b", "B", ("running",)), ("a", "A", ("running",))])
    assert tree.order == ["b", "a"] and tree.selected == ("a",) and tree.focused == "a"
    assert tree.horizontal == (0.2, 0.7) and tree.vertical == (0.3, 0.8)
    assert tree.changed == [("item", "b"), ("move", "b")]
    tree.changed.clear()
    sync_rows(tree, [("b", "B", ("running",)), ("a", "A", ("running",))])
    assert tree.changed == []


def test_remove_and_add_do_not_delete_unaffected_rows():
    tree = Tree()
    sync_rows(tree, [("a", "A", ("running",)), ("c", "C", ("queued",))])
    assert tree.changed == [("delete", ["b"]), ("insert", "c")] or tree.changed == [
        ("delete", ("b",)),
        ("insert", "c"),
    ]
    assert tree.selected == ("a",)


class Text:
    """Model Tk line indices and fractions as content grows; no constant fake view."""

    def __init__(self, bottom=False, text="old", top=1, page=10):
        self.text, self.bottom, self.calls = text, bottom, []
        self.top, self.page = top, page

    def get(self, *args):
        return self.text

    def index(self, position):
        assert position == "@0,0"
        return f"{self.top}.0"

    def xview(self):
        return 0.2, 0.8

    def yview(self, index=None):
        if index is not None:
            self.top = int(index.split(".")[0])
            self.calls.append(("yview", index))
        count = len(self.text.split("\n"))
        return (
            (self.top - 1) / count,
            1.0 if self.bottom else min(0.99, (self.top + self.page) / count),
        )

    def configure(self, **kwargs):
        self.calls.append(("configure", kwargs))

    def delete(self, *args):
        self.calls.append(("delete", args))
        self.text = ""
        self.top = 1

    def insert(self, index, text):
        self.text = self.text + text if index == "end-1c" else text

    def see(self, where):
        self.calls.append(("see", where))

    def xview_moveto(self, value):
        self.calls.append(("xview", value))


@pytest.mark.parametrize("bottom", [False, True])
def test_log_follow_only_when_the_reader_was_at_the_end(bottom):
    text = Text(bottom)
    replace_text(text, "new\noutput", follow=True)
    assert (("see", "end") in text.calls) is bottom
    assert any(call[0] == "yview" for call in text.calls) is not bottom
    assert ("xview", 0.2) in text.calls
    text.calls.clear()
    replace_text(text, "new\noutput", follow=True)
    assert text.calls == []


def test_append_keeps_actual_visible_line_as_buffer_length_doubles():
    original = "\n".join(f"line {n}" for n in range(1, 101))
    text = Text(text=original, top=25)
    replace_text(
        text, original + "\n" + "\n".join(f"line {n}" for n in range(101, 201)), follow=True
    )
    assert text.top == 25 and text.text.split("\n")[text.top - 1] == "line 25"
    assert not any(call[0] in ("delete", "see") for call in text.calls)


def test_retention_trim_preserves_surviving_top_line():
    text = Text(text="\n".join(f"line {n}" for n in range(1, 101)), top=25)
    replace_text(text, "\n".join(f"line {n}" for n in range(11, 111)), follow=True)
    assert text.top == 15 and text.text.split("\n")[text.top - 1] == "line 25"


def response(lines, *, epoch="a" * 32, gap=False, expired=False, sequence=None):
    return dict(
        ok=True,
        entries=[dict(sequence=n, text=text) for n, text in lines],
        next_cursor=dict(epoch=epoch, sequence=sequence if sequence is not None else len(lines)),
        gap=gap,
        expired=expired,
        has_more=False,
    )


def test_log_epoch_gap_duplicates_and_expiry_are_independent():
    first = read_log_result(LogView(), response([(1, "one"), (2, "two")]))
    duplicate = read_log_result(first, response([(1, "one"), (2, "two")]))
    assert duplicate.lines == ("one", "two")
    restarted = read_log_result(first, response([(1, "new")], epoch="b" * 32, gap=True))
    assert restarted.lines == ("new",) and restarted.gap
    expired = read_log_result(first, response([], expired=True, gap=True, sequence=2))
    assert expired.phase == "expired" and expired.lines == ()
    assert "expired" in expired.message


def test_log_body_is_removed_on_denial_and_never_echoes_error_or_manifest():
    first = read_log_result(LogView(), response([(1, "private output")]))
    denied = read_log_result(
        first, {"ok": False, "error": "not_authorized", "secret": "SYNTHETIC_SECRET"}
    )
    assert denied.phase == "denied" and denied.lines == ()
    invalid = read_log_result(first, {"ok": False, "error": "SYNTHETIC_SECRET"})
    assert invalid.phase == "unavailable" and "SYNTHETIC" not in invalid.message


def test_log_memory_is_bounded_and_omission_is_explicit():
    current = LogView()
    for start in range(0, 800, 100):
        current = read_log_result(
            current,
            response(
                [(n, "x" * 4096) for n in range(start + 1, start + 101)], sequence=start + 100
            ),
        )
    assert current.phase == "available" and current.gap
    assert len(current.lines) <= MAX_LOG_LINES
    assert sum(len(line.encode()) for line in current.lines) <= MAX_LOG_BYTES


@pytest.mark.parametrize("bad", [None, {}, {"ok": True}, response([(1, "secret")], sequence=0)])
def test_invalid_log_response_cannot_be_displayed(bad):
    result = read_log_result(LogView(), bad)
    assert result.phase == "unavailable" and result.lines == ()


def test_deleted_selected_row_does_not_clear_the_missing_selection_detail():
    window = object.__new__(LiveWindow)
    window.state = select(LiveState(), "gone")
    window.job_tree = Mock()
    window.job_tree.selection.return_value = ()
    window._select()
    assert window.state.selected_job_id == "gone" and window.state.selection_missing


def test_close_cancels_view_only_and_ignores_late_second_close():
    window = object.__new__(LiveWindow)
    window.preview_image = Mock()
    window.state = replace(LiveState(), connection="live", generation=3)
    window.action_pending = False
    window.root, window.runner, window.api = Mock(), Mock(), Mock()
    window.timer = "view-timer"
    window._finish_close()
    window._finish_close()
    window.root.after_cancel.assert_called_once_with("view-timer")
    window.runner.close.assert_called_once()
    window.root.destroy.assert_called_once()
    assert window.state.connection == "closed" and window.api.mock_calls == []


def test_pending_mutation_result_prevents_close_before_tunnel_is_touched():
    window = object.__new__(LiveWindow)
    window.action_pending = True
    window.status, window.tunnel_panel, window.root = Mock(), Mock(), Mock()
    window._close()
    window.tunnel_panel.request_close.assert_not_called()
    window.root.destroy.assert_not_called()


def test_manifest_declares_system_dpi_without_elevation_and_spec_uses_it():
    root = Path(__file__).resolve().parents[1]
    manifest = ElementTree.parse(root / "packaging/cardputer.manifest")
    namespaces = {
        "v3": "urn:schemas-microsoft-com:asm.v3",
        "dpi": "http://schemas.microsoft.com/SMI/2016/WindowsSettings",
    }
    level = manifest.find(
        "v3:trustInfo/v3:security/v3:requestedPrivileges/v3:requestedExecutionLevel", namespaces
    )
    assert level.attrib == {"level": "asInvoker", "uiAccess": "false"}
    dpi = manifest.find("v3:application/v3:windowsSettings/dpi:dpiAwareness", namespaces)
    assert dpi.text == "System"
    spec = (root / "packaging/cardputer.spec").read_text()
    assert 'hide_console="hide-early"' in spec and "console=True" in spec
    assert (
        'manifest=str(root / "packaging" / "cardputer.manifest")'
        in (root / "packaging/cardputer.spec").read_text()
    )


def live_window():
    window = object.__new__(LiveWindow)
    window.state = select(ready(snapshot([card("a"), card("b")])), "a")
    window.logs = read_log_result(LogView(), response([(1, "old")]))
    window.log_requests = LogRequestState()
    window.preview_state = PreviewState()
    window.preview_photo = window.displayed_preview = None
    window.preview_image, window.preview_area, window.preview_status = Mock(), Mock(), Mock()
    window.close_prompt = False
    window.tunnel_panel = None
    window.action_pending = False
    window.action_results = queue.Queue(maxsize=2)
    window.queue_controller = QueueController()
    window.root, window.api, window.runner = Mock(), Mock(), Mock()
    window.runner.read_logs.side_effect = range(10, 100)
    window.action_status = Mock()
    window.status = Mock()
    window._render = Mock()
    window._render_logs = Mock()
    window._record_changes = Mock()
    window._controls = Mock()
    window.job_tree = Mock()
    return window


def drain(window, *events):
    window.runner.drain.return_value = list(events)
    window._drain()


def message(window, payload):
    return TransportEvent("message", window.state.generation, payload, 1001)


def log_result(window, request_id, job="a", text="new"):
    return TransportEvent(
        "logs",
        window.state.generation,
        {
            "job_id": job,
            "request_id": request_id,
            "response": response([(2, text)], sequence=2),
        },
        1001,
    )


def test_log_request_state_is_frozen_and_keeps_notifications_during_read():
    initial = LogRequestState()
    pending = initial.started(10).changed().changed()
    assert pending.pending and pending.dirty
    assert pending.completed(9) is pending
    completed = pending.completed(10)
    assert not completed.pending and completed.dirty
    next_read = completed.started(11)
    assert next_read.pending and not next_read.dirty
    with pytest.raises(FrozenInstanceError):
        initial.version = 4


def test_reconnect_snapshot_revalidates_retained_selected_output():
    window = live_window()
    window._reconnect()
    assert window.state.connection == "connecting"
    drain(window, message(window, snapshot([card("a"), card("b")], sequence=5)))
    window.runner.read_logs.assert_called_once_with(
        window.state.generation, "a", window.logs.cursor
    )
    assert window.log_requests.pending


def test_notification_during_pending_read_requires_followup_even_without_more():
    window = live_window()
    window._read_logs()
    first_id = window.log_requests.request_id
    drain(
        window,
        message(window, delta(order=["a", "b"], logs_changed=True)),
        log_result(window, first_id),
    )
    assert window.runner.read_logs.call_count == 2
    assert window.log_requests.request_id != first_id
    assert window.log_requests.pending and not window.log_requests.dirty


def test_denied_output_is_not_automatically_retried_by_other_jobs_log_notifications():
    window = live_window()
    window.logs = LogView("denied")
    window.log_requests = window.log_requests.started(1).completed(1)
    drain(window, message(window, delta(order=["a", "b"], logs_changed=True)))
    window.runner.read_logs.assert_not_called()
    window._reconnect()
    drain(window, message(window, snapshot([card("a"), card("b")], sequence=5)))
    assert window.runner.read_logs.call_count == 1


def test_notification_during_denied_read_does_not_turn_into_a_retry_loop():
    window = live_window()
    window._read_logs()
    denied = log_result(window, window.log_requests.request_id)
    denied.payload["response"] = {"ok": False, "error": "not_authorized"}
    drain(window, message(window, delta(order=["a", "b"], logs_changed=True)), denied)
    assert window.runner.read_logs.call_count == 1 and not window.log_requests.dirty
    drain(window, message(window, delta(sequence=2, base=1, order=["a", "b"], logs_changed=True)))
    assert window.runner.read_logs.call_count == 1


def test_late_log_completion_after_selection_a_b_a_cannot_replace_current_output():
    window = live_window()
    window._read_logs()
    old_id = window.log_requests.request_id
    for job in ("b", "a"):
        window.job_tree.selection.return_value = (job,)
        window._select()
    current_id = window.log_requests.request_id
    assert current_id != old_id
    drain(window, log_result(window, old_id, text="STALE"))
    assert window.logs.lines == () and window.log_requests.request_id == current_id
    drain(window, log_result(window, current_id, text="current"))
    assert window.logs.lines == ("current",) and not window.log_requests.pending


@pytest.mark.parametrize(
    "change", ["disconnect", "reconnect", "selection", "pending", "closing", "closed", "epoch"]
)
def test_cancel_rechecks_current_authority_after_modal_feed_callbacks(change):
    window = live_window()
    window._run_action = Mock()

    def confirmation(*args, **kwargs):
        if change == "disconnect":
            window.state = disconnect(window.state, window.state.generation)
        elif change == "reconnect":
            window.state = begin(window.state)
        elif change == "selection":
            window.state = select(window.state, "b")
        elif change == "pending":
            window.action_pending = True
        elif change == "closing":
            window.close_prompt = True
        elif change == "closed":
            window._finish_close()
        else:
            window.state = replace(
                window.state, timeline=replace(window.state.timeline, broker_epoch="f" * 32)
            )
        return True

    window.timer = None
    with patch("tkinter.messagebox.askyesno", side_effect=confirmation):
        window._cancel_job()
    window._run_action.assert_not_called()
    window.api.cancel_job.assert_not_called()


def test_cancel_uses_existing_identity_validation_and_starts_deadline_after_confirmation():
    window = live_window()
    times = []
    now = time.monotonic()
    with (
        patch(
            "tkinter.messagebox.askyesno",
            side_effect=lambda *args, **kwargs: times.append("confirmed") or True,
        ),
        patch(
            "cardputer_mcp.gui_live.time.monotonic",
            side_effect=lambda: times.append("clock") or now,
        ),
    ):
        window._run_action = Mock()
        window._cancel_job()
    assert times == ["confirmed", "clock"]
    window.api.cancel_job.return_value = {"ok": True}
    action = window._run_action.call_args.args[0]
    assert action() == CANCEL_UNCERTAIN
    window.api.cancel_job.assert_called_once()


def test_diagnostic_completion_clears_pending_message_without_losing_detail():
    window = live_window()
    window.connection_panels = Mock()
    window.action_pending = True
    window.action_results.put(("diagnostic_http", DiagnosticOutcome("HTTP diagnostics loaded")))
    drain(window)
    assert not window.action_pending
    assert window.connection_panels.show_result.call_args.args[:2] == (
        "http",
        "HTTP diagnostics loaded",
    )
    window.action_status.set.assert_called_with("")


def test_queue_failure_clears_busy_and_result_message_survives_reconnect():
    window = live_window()
    operation = window.queue_controller.refresh()
    window.action_pending = True
    window.action_results.put(
        ("queue", Outcome(operation.sequence, None, "Action outcome uncertain", True))
    )
    drain(window)
    assert not window.queue_controller.busy and not window.action_pending
    window.action_status.set.assert_called_with("Action outcome uncertain")
    assert window.state.connection == "connecting"


def test_close_callback_during_tunnel_confirmation_is_allowed_and_cancels_only_reads():
    window = live_window()
    window.timer = "timer"
    window.tunnel_panel = Mock()
    window.tunnel_panel.controller.closing = False
    window.tunnel_panel.request_close.side_effect = lambda callback: callback()
    window._close()
    assert window.state.connection == "closed"
    window.runner.close.assert_called_once()
    window.root.destroy.assert_called_once()
    assert window.api.mock_calls == []
    drain(window, log_result(window, 10))
    assert window.api.mock_calls == []


@pytest.mark.parametrize("change", ["disconnect", "reconnect", "closing", "lease", "epoch"])
def test_release_rechecks_owner_and_live_state_after_modal_callback(change):
    window = live_window()
    window.queue_controller = Mock()
    window.queue_controller.selected_lease = "lease-before"
    window.queue_controller.can_release.return_value = True
    window._queue_action = Mock()

    def confirmation(*args, **kwargs):
        if change == "disconnect":
            window.state = disconnect(window.state, window.state.generation)
        elif change == "reconnect":
            window.state = begin(window.state)
        elif change == "closing":
            window.close_prompt = True
        elif change == "epoch":
            window.state = replace(
                window.state, timeline=replace(window.state.timeline, broker_epoch="f" * 32)
            )
        else:
            window.queue_controller.selected_lease = "lease-after"
            window.queue_controller.release.return_value = None
        return True

    with patch("tkinter.messagebox.askyesno", side_effect=confirmation):
        window._release()
    if change == "lease":
        window.queue_controller.release.assert_called_once_with("lease-before")
        window._queue_action.assert_called_once_with(None)
    else:
        window._queue_action.assert_not_called()
    window.api.release.assert_not_called()


def test_async_tunnel_close_blocks_actions_and_later_callback_finishes_view():
    window = live_window()
    window.timer = "timer"
    window.tunnel_panel = Mock()
    window.tunnel_panel.controller.closing = False
    callbacks = []

    def request_close(callback):
        callbacks.append(callback)
        window.tunnel_panel.controller.closing = True

    window.tunnel_panel.request_close.side_effect = request_close
    window._close()
    assert window.state.connection == "live" and window._is_closing()
    action = Mock()
    window._run_action(action)
    window._cancel_wait()
    assert not window.action_pending and not window.queue_controller.busy
    action.assert_not_called()
    window.root.destroy.assert_not_called()
    callbacks[0]()
    assert window.state.connection == "closed"
    window.runner.close.assert_called_once()
    window.root.destroy.assert_called_once()
    assert window.api.mock_calls == []


def test_rejected_tunnel_confirmation_leaves_view_usable():
    window = live_window()
    window.tunnel_panel = Mock()
    window.tunnel_panel.controller.closing = False
    window._close()
    assert not window._is_closing() and window._actions_ready()
    window.root.destroy.assert_not_called()


def test_closed_generation_ignores_late_log_and_live_completions():
    window = live_window()
    window.timer = None
    window._read_logs()
    completion = log_result(window, window.log_requests.request_id, text="LATE")
    event = message(window, snapshot([card("a")], sequence=100))
    previous = window.logs
    window._finish_close()
    drain(window, completion, event)
    assert window.logs is previous and window.state.connection == "closed"
    assert window.api.mock_calls == []


def test_retained_selection_reappearing_revalidates_output_without_log_notification():
    window = live_window()
    window.log_requests = LogRequestState().started(10).completed(10)
    drain(window, message(window, delta(remove=["a"], order=["b"])))
    assert window.state.selection_missing
    drain(window, message(window, delta(2, 1, upsert=[card("a")], order=["a", "b"])))
    window.runner.read_logs.assert_called_once()


def test_native_tk_append_keeps_visible_line_when_a_desktop_is_available():
    import tkinter as tk

    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("Native Tk desktop is unavailable")
    try:
        root.geometry("420x240")
        text = tk.Text(root, wrap="none")
        text.pack(fill="both", expand=True)
        original = "\n".join(f"line {n}" for n in range(1, 101))
        text.insert("1.0", original)
        root.update()
        text.yview("25.0")
        root.update()
        top = text.index("@0,0")
        replace_text(
            text, original + "\n" + "\n".join(f"line {n}" for n in range(101, 201)), follow=True
        )
        root.update()
        assert text.index("@0,0") == top
    finally:
        root.destroy()


@pytest.mark.parametrize(
    "test,expected",
    [
        (None, True),
        ("firmware_fetch_source", True),
        ("firmware_build", True),
        ("firmware_backup", True),
        ("firmware_flash", True),
        ("firmware_restore", True),
        ("app_session", True),
        ("capture_home", False),
        ("derby_smoke", False),
        ("diagnostics", False),
        ("firmware_observe", False),
    ],
)
def test_private_log_applicability_uses_model_request_names(test, expected):
    assert logs_applicable(test) is expected


def test_known_nonlog_job_does_not_request_http_and_discards_old_body():
    window = live_window()
    window.state = select(
        ready(snapshot([card("a", test="capture_home", cancel_available=False)])), "a"
    )
    window.log_requests = window.log_requests.started(25)
    window._read_logs()
    window.runner.read_logs.assert_not_called()
    assert window.logs.phase == "not_applicable" and window.logs.lines == ()
    assert not window.log_requests.pending and not window.log_requests.dirty


def test_valid_empty_output_is_distinct_from_unavailable_and_loading():
    empty = read_log_result(LogView(), response([], sequence=0))
    assert empty.phase == "empty" and empty.message.startswith("No output")
    assert LogView("loading").message == "Reading permitted output..."
    assert (
        len({empty.message, LogView("unavailable").message, LogView("not_applicable").message}) == 3
    )
    text = "日本語 ... … — ·"
    received = read_log_result(empty, response([(1, text)]))
    assert received.phase == "available" and received.lines == (text,)


def test_transient_disconnect_retries_new_generation_and_never_mutations():
    from cardputer_mcp.gui_live_retry import diagnostic

    window = live_window()
    window.status = Mock()
    old = window.state.generation
    failure = TransportEvent(
        "disconnected",
        old,
        {"error": "fixed"},
        0,
        diagnostic("read_timeout", old, 35000, retryable=True),
    )
    with patch("cardputer_mcp.gui_live.time.monotonic", return_value=100):
        drain(window, failure)
    assert window.state.connection == "disconnected"
    window.live_retry.deadline = 101
    with patch("cardputer_mcp.gui_live.time.monotonic", return_value=102):
        drain(window)
    assert window.state.generation == old + 1 and window.state.connection == "connecting"
    window.runner.reconnect.assert_called_once_with(old + 1)
    assert window.logs.lines == ("old",)
    window.api.cancel_job.assert_not_called()
    window.api.release_idle.assert_not_called()
    window.api.execute.assert_not_called()
    drain(window, TransportEvent("message", old, snapshot([card("stale")]), 0))
    assert window.state.connection == "connecting"
    drain(window, TransportEvent("message", old + 1, snapshot([card("fresh")]), 0))
    assert window.state.connection == "live"
    assert window.state.card("fresh") is not None and window.state.card("stale") is None


@pytest.mark.parametrize("blocked", ["pending", "closing", "closed"])
def test_scheduled_live_reconnect_respects_owned_action_and_close(blocked):
    from cardputer_mcp.gui_live_retry import diagnostic

    window = live_window()
    window.status = Mock()
    window.state = disconnect(window.state, window.state.generation)
    window._retry_state().failed(
        diagnostic("read_timeout", window.state.generation, 0, retryable=True), 0, jitter=1
    )
    if blocked == "pending":
        window.action_pending = True
    elif blocked == "closing":
        window.close_prompt = True
    else:
        from cardputer_mcp.gui_live_state import close

        window.state = close(window.state)
    with patch("cardputer_mcp.gui_live.time.monotonic", return_value=100):
        drain(window)
    window.runner.reconnect.assert_not_called()


def test_manual_reconnect_cancels_scheduled_retry_and_drops_old_failure():
    from cardputer_mcp.gui_live_retry import diagnostic

    window = live_window()
    window.status = Mock()
    old = window.state.generation
    window.state = disconnect(window.state, old)
    window._retry_state().failed(diagnostic("read_timeout", old, 0, retryable=True), 0, jitter=1)
    window._reconnect()
    assert window.live_retry.deadline is None and window.live_retry.attempts == 0
    with patch("cardputer_mcp.gui_live.time.monotonic", return_value=100):
        drain(window, TransportEvent("disconnected", old, {"error": "fixed"}, 0))
    window.runner.reconnect.assert_called_once_with(old + 1)
    assert window.state.connection == "connecting"


def test_repeated_reducer_invalidity_stops_after_one_full_resync():
    window = live_window()
    with patch("cardputer_mcp.gui_live.time.monotonic", return_value=100):
        drain(window, message(window, {"ok": True, "kind": "invalid"}))
    window.runner.pause.assert_called_once_with(window.state.generation)
    assert window.live_retry.invalid_count == 1 and window.live_retry.deadline is not None
    window.live_retry.deadline = 101
    with patch("cardputer_mcp.gui_live.time.monotonic", return_value=102):
        drain(window)
    with patch("cardputer_mcp.gui_live.time.monotonic", return_value=103):
        drain(window, message(window, {"ok": True, "kind": "invalid"}))
    assert window.live_retry.stopped and window.live_retry.deadline is None
    window.runner.reconnect.assert_called_once()


def test_auth_failure_records_diagnostic_and_never_schedules_retry():
    from cardputer_mcp.gui_live_retry import diagnostic
    from cardputer_mcp.gui_live_transport import AUTH_FAILED

    window = live_window()
    old = window.state.generation
    details = diagnostic("authentication_failed", old, 5, status=401)
    drain(window, TransportEvent("disconnected", old, {"error": AUTH_FAILED}, 0, details))
    assert window.live_retry.stopped and window.live_retry.deadline is None
    assert window.live_diagnostics == [details]
    assert window.state.connection == "disconnected"
    window.runner.reconnect.assert_not_called()


@pytest.mark.parametrize(
    "phase", ["idle", "empty", "denied", "expired", "unavailable", "not_applicable"]
)
def test_result_state_is_inside_the_copyable_content_not_below_an_empty_pane(phase):
    window = live_window()
    window.logs = LogView(phase)
    window.log_text = Text(text="old output")
    window.log_status = Mock()
    window.log_context = Mock()
    LiveWindow._render_logs(window)
    assert window.log_text.text == window.logs.message
    assert window.log_context.set.call_args.args[0].startswith("Logs | firmware_build")
    assert window.api.mock_calls == []
    window.root.clipboard_clear.assert_not_called()


def test_loading_is_visible_inside_empty_content_and_received_time_is_not_emission_time():
    window = live_window()
    window.logs = LogView()
    window.log_requests = window.log_requests.started(5)
    window.log_text = Text(text="")
    window.log_status, window.log_context = Mock(), Mock()
    LiveWindow._render_logs(window)
    assert window.log_text.text == "Reading permitted output..."
    window.logs = LogView(
        "available", lines=("observed output",), received_utc="2026-10-22T10:00:00+00:00"
    )
    window.log_requests = window.log_requests.completed(5)
    LiveWindow._render_logs(window)
    assert "Read 2026-10-22T10:00:00+00:00" in window.log_context.set.call_args.args[0]
    assert window.log_text.text == "observed output"


def test_whole_text_copy_requires_deliberate_action_and_preserves_unicode():
    window = live_window()
    for name, value in {
        "detail_heading": "a | Running",
        "detail_text": "Selected status",
        "log_context": "Logs | Retained",
        "log_text": "日本語 output",
        "log_status": "Some output is missing",
        "preview_status": "No retained image",
    }.items():
        widget = Mock()
        widget.get.return_value = value
        setattr(window, name, widget)
    window.root.clipboard_clear.assert_not_called()
    window._copy_result()
    window.root.clipboard_clear.assert_called_once()
    copied = window.root.clipboard_append.call_args.args[0]
    assert "日本語 output" in copied and "Logs | Retained" in copied
    assert "Some output is missing" in copied and "Selected status" in copied
    window.state = select(window.state, "")
    window._copy_result()
    assert window.root.clipboard_append.call_count == 1


def test_cancel_visibility_uses_capability_predicate_and_rechecks_after_state_change():
    window = live_window()
    for name in (
        "job_cancel",
        "release_button",
        "wait_cancel",
        "up_button",
        "down_button",
        "cancel_context",
    ):
        setattr(window, name, Mock())
    for available in (False, True, False):
        window.state = select(ready(snapshot([card("a", cancel_available=available)])), "a")
        LiveWindow._controls(window)
        (window.job_cancel.grid if available else window.job_cancel.grid_remove).assert_called()
        assert window.job_cancel.configure.call_args.kwargs["state"] == (
            "normal" if available else "disabled"
        )
    window.state = select(
        ready(snapshot([card("a", test="firmware_flash", cancel_available=False)])), "a"
    )
    LiveWindow._controls(window)
    assert window.cancel_context.set.call_args.args[0] == "Cancel via the owning client session"


def test_failed_diagnostic_is_not_presented_as_a_received_success():
    window = live_window()
    window.connection_panels = Mock()
    window.action_pending = True
    window.action_results.put(("diagnostic_mcp", DiagnosticOutcome(None)))
    drain(window)
    window.connection_panels.show_error.assert_called_once_with("mcp")
    window.connection_panels.show_result.assert_not_called()
    assert not window.action_pending


def test_raised_diagnostic_client_error_traverses_real_worker_and_drain_without_receipt():
    from cardputer_mcp.operator_client import OperatorClientError

    window = live_window()
    window.connection_panels = Mock()
    window.api.catalog.side_effect = OperatorClientError("private synthetic exception")
    window._diagnostic(True)
    deadline = time.monotonic() + 2
    while window.action_results.empty() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not window.action_results.empty()
    drain(window)
    window.connection_panels.show_loading.assert_called_once_with("mcp")
    window.connection_panels.show_error.assert_called_once_with("mcp")
    window.connection_panels.show_result.assert_not_called()
    assert "private synthetic" not in repr(window.action_status.mock_calls)
    assert not window.action_pending
