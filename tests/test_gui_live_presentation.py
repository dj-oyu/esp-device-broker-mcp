"""Stable keyed rendering and bounded permitted-output presentation."""

import queue
import time
from dataclasses import replace
from unittest.mock import Mock, patch

import pytest
from test_gui_live_state import card, delta, ready, snapshot

from cardputer_mcp.gui import QueueController
from cardputer_mcp.gui_connection_panels import DiagnosticOutcome
from cardputer_mcp.gui_live import LiveWindow
from cardputer_mcp.gui_live_presentation import (
    LogRequestState,
    LogView,
    read_log_result,
)
from cardputer_mcp.gui_live_preview import PreviewState
from cardputer_mcp.gui_live_state import begin, disconnect, select
from cardputer_mcp.gui_live_transport import TransportEvent
from cardputer_mcp.gui_timeline import CANCEL_UNCERTAIN


def response(lines, *, epoch="a" * 32, gap=False, expired=False, sequence=None):
    return dict(
        ok=True,
        entries=[dict(sequence=n, text=text) for n, text in lines],
        next_cursor=dict(epoch=epoch, sequence=sequence if sequence is not None else len(lines)),
        gap=gap,
        expired=expired,
        has_more=False,
    )


def test_log_body_is_removed_on_denial_and_never_echoes_error_or_manifest():
    first = read_log_result(LogView(), response([(1, "private output")]))
    denied = read_log_result(
        first, {"ok": False, "error": "not_authorized", "secret": "SYNTHETIC_SECRET"}
    )
    assert denied.phase == "denied" and denied.lines == ()
    invalid = read_log_result(first, {"ok": False, "error": "SYNTHETIC_SECRET"})
    assert invalid.phase == "unavailable" and "SYNTHETIC" not in invalid.message


@pytest.mark.parametrize("bad", [None, {}, {"ok": True}, response([(1, "secret")], sequence=0)])
def test_invalid_log_response_cannot_be_displayed(bad):
    result = read_log_result(LogView(), bad)
    assert result.phase == "unavailable" and result.lines == ()


def test_pending_mutation_result_prevents_close_before_tunnel_is_touched():
    window = object.__new__(LiveWindow)
    window.action_pending = True
    window.status, window.tunnel_panel, window.root = Mock(), Mock(), Mock()
    window._close()
    window.tunnel_panel.request_close.assert_not_called()
    window.root.destroy.assert_not_called()


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
