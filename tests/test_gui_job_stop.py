"""GUI stop control for a device job owned by another client; no Tk, broker or device."""

from unittest.mock import Mock, patch

import pytest
from test_gui_live_presentation import live_window
from test_gui_live_state import card, ready, snapshot

from cardputer_mcp.gui_live import LiveWindow
from cardputer_mcp.gui_live_state import select
from cardputer_mcp.gui_review import (
    STOP_NOT_ACCEPTED,
    STOP_QUEUED,
    STOP_REFUSED,
    STOP_REQUESTED,
    STOP_UNCERTAIN,
    perform_stop_job,
    stop_available,
)
from cardputer_mcp.gui_timeline import TimelineCard

EPOCH = "e" * 32


def device_card(**changes):
    return card(
        "a",
        **{
            "test": "app_session",
            "backend": "serial",
            "stage": None,
            "cancel_available": False,
            "stop_available": True,
            **changes,
        },
    )


def reply(**changes):
    return {
        "ok": True,
        "broker_epoch": EPOCH,
        "job_id": "a",
        "plan_hash": "a" * 64,
        "state": "running",
        "stop_requested": True,
        "requires_review": False,
        "stopped_by": "local_operator",
        "audit_recorded": True,
        **changes,
    }


def test_timeline_parses_stop_available_for_active_device_jobs():
    parsed = TimelineCard.parse(device_card(), EPOCH, 1000)
    assert parsed.stop_available and stop_available(parsed)
    assert not stop_available(TimelineCard.parse(device_card(stop_available=False), EPOCH, 1000))
    assert not stop_available(None)


@pytest.mark.parametrize(
    "changes",
    [
        {"stop_available": "yes"},
        {"state": "succeeded"},
        {"backend": "registered_project", "test": "firmware_build"},
        {"cancel_available": True, "backend": "registered_project", "test": "firmware_build"},
    ],
)
def test_timeline_rejects_inconsistent_stop_flags(changes):
    with pytest.raises(ValueError):
        TimelineCard.parse(device_card(**changes), EPOCH, 1000)


@pytest.mark.parametrize(
    ("answer", "message"),
    [
        (reply(), STOP_REQUESTED),
        (reply(stop_requested=False), STOP_NOT_ACCEPTED),
        (reply(state="cancelled", stop_requested=False), STOP_QUEUED),
        (
            {"ok": False, "error": "operator_stop_refused_device_write"},
            STOP_REFUSED["operator_stop_refused_device_write"],
        ),
        ({"ok": False, "error": "SYNTHETIC private"}, STOP_UNCERTAIN),
        (reply(stopped_by="owner"), STOP_UNCERTAIN),
        (reply(broker_epoch="f" * 32), STOP_UNCERTAIN),
        (reply(job_id="b"), STOP_UNCERTAIN),
        (reply(state="succeeded"), STOP_UNCERTAIN),
        (None, STOP_UNCERTAIN),
    ],
)
def test_stop_messages_send_one_request(answer, message):
    api = Mock()
    api.stop_job.return_value = answer
    assert perform_stop_job(api, "a", "a" * 64, EPOCH) == message
    api.stop_job.assert_called_once_with("a", "a" * 64, EPOCH)


def test_lost_stop_is_uncertain_and_not_retried():
    api = Mock()
    api.stop_job.side_effect = RuntimeError("SYNTHETIC_PRIVATE")
    assert perform_stop_job(api, "a", "a" * 64, EPOCH) == STOP_UNCERTAIN
    api.stop_job.assert_called_once()


def controls(window):
    for name in (
        "job_cancel",
        "job_review",
        "job_stop",
        "recheck_button",
        "request_review",
        "release_button",
        "wait_cancel",
        "up_button",
        "down_button",
        "cancel_context",
    ):
        setattr(window, name, Mock())
    LiveWindow._controls(window)


@pytest.mark.parametrize("available", [False, True])
def test_stop_button_follows_the_selected_card(available):
    window = live_window()
    window.state = select(ready(snapshot([device_card(stop_available=available)])), "a")
    controls(window)
    (window.job_stop.grid if available else window.job_stop.grid_remove).assert_called()
    assert window.job_stop.configure.call_args.kwargs["state"] == (
        "normal" if available else "disabled"
    )
    window.job_cancel.grid_remove.assert_called()


@pytest.mark.parametrize("confirmed", [False, True])
def test_stop_asks_first_and_sends_one_request(confirmed):
    window = live_window()
    window.state = select(ready(snapshot([device_card()])), "a")
    window._run_action = Mock()
    with patch("tkinter.messagebox.askyesno", return_value=confirmed) as ask:
        window._stop_job()
    ask.assert_called_once()
    if not confirmed:
        window._run_action.assert_not_called()
        return
    window.api.stop_job.return_value = reply()
    assert window._run_action.call_args.args[0]() == STOP_REQUESTED
    window.api.stop_job.assert_called_once_with("a", "a" * 64, EPOCH)


def test_stop_is_not_sent_without_availability():
    window = live_window()
    window.state = select(ready(snapshot([device_card(stop_available=False)])), "a")
    window._run_action = Mock()
    with patch("tkinter.messagebox.askyesno", return_value=True) as ask:
        window._stop_job()
    ask.assert_not_called()
    window._run_action.assert_not_called()
