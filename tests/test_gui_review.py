"""GUI acknowledgement and re-check controls; no Tk window, worker, broker or device."""

from unittest.mock import Mock, patch

import pytest
from test_gui_live_presentation import live_window
from test_gui_live_state import card, queue_state, ready, snapshot

from cardputer_mcp.gui import QueueSnapshot
from cardputer_mcp.gui_live import LiveWindow
from cardputer_mcp.gui_live_state import select
from cardputer_mcp.gui_review import (
    ACK_ALREADY,
    ACK_DONE,
    ACK_REFUSED,
    ACK_UNCERTAIN,
    RECHECK_NOTHING,
    RECHECK_UNCERTAIN,
    perform_recheck,
    perform_review_job,
    perform_review_request,
    recheck_available,
    review_available,
    uncertainty_summary,
)
from cardputer_mcp.gui_timeline import TimelineCard

EPOCH = "e" * 32
REQUEST = {"id": "restart-request", "state": "needs_review", "reviewed": 0}


def review_card(**changes):
    return card(
        "a", **{"state": "needs_review", "stage": None, "cancel_available": False, **changes}
    )


def queue(**changes):
    return QueueSnapshot.parse(queue_state(**changes))


# --- parsing and predicates ----------------------------------------------------------------


def test_snapshot_parses_recovery_requests_and_uncertainty():
    parsed = queue(
        recovery_requests=[
            {**REQUEST, "reason": "broker_restart_no_replay", "created": "2026-10-25 01:02:03"}
        ],
        uncertainty=[{"flag": "serial_ownership", "reason": "serial_close_unconfirmed"}],
    )
    assert [row.request_id for row in parsed.recovery_requests] == ["restart-request"]
    assert parsed.recovery_requests[0].reason == "broker_restart_no_replay"
    assert recheck_available(parsed)
    assert "Serial port close (serial_close_unconfirmed)" in uncertainty_summary(parsed)
    plain = queue()
    assert plain.recovery_requests == () and plain.uncertainty == ()
    assert not recheck_available(plain)
    assert uncertainty_summary(plain) == "Uncertainty flags: none reported"


@pytest.mark.parametrize(
    "changes",
    [
        {"recovery_requests": [{**REQUEST, "reviewed": 1}]},
        {"recovery_requests": [{**REQUEST, "state": "waiting"}]},
        {"recovery_requests": [{**REQUEST, "reason": "C:/private path"}]},
        {"recovery_requests": [REQUEST, REQUEST]},
        {"uncertainty": [{"flag": "serial_ownership", "reason": "Free text"}]},
        {"uncertainty": "serial"},
    ],
)
def test_snapshot_rejects_malformed_review_or_uncertainty_rows(changes):
    with pytest.raises(ValueError):
        queue(**changes)


@pytest.mark.parametrize(
    ("changes", "available"),
    [
        ({}, True),
        ({"reviewed": True}, False),
        ({"state": "failed"}, False),
    ],
)
def test_review_is_offered_only_for_unreviewed_needs_review(changes, available):
    parsed = TimelineCard.parse(review_card(**changes), EPOCH, 1000)
    assert review_available(parsed) is available
    assert review_available(None) is False


# --- one request each, fixed messages --------------------------------------------------------


def job_reply(**changes):
    return {
        "ok": True,
        "broker_epoch": EPOCH,
        "job_id": "a",
        "plan_hash": "a" * 64,
        "state": "needs_review",
        "reviewed": True,
        "already_reviewed": False,
        "replayed": False,
        **changes,
    }


@pytest.mark.parametrize(
    ("reply", "message"),
    [
        (job_reply(), ACK_DONE),
        (job_reply(already_reviewed=True), ACK_ALREADY),
        ({"ok": False, "error": "review_not_required"}, ACK_REFUSED["review_not_required"]),
        ({"ok": False, "error": "SYNTHETIC private"}, ACK_UNCERTAIN),
        (job_reply(state="failed"), ACK_UNCERTAIN),
        (job_reply(replayed=True), ACK_UNCERTAIN),
        (job_reply(broker_epoch="f" * 32), ACK_UNCERTAIN),
        (None, ACK_UNCERTAIN),
    ],
)
def test_job_acknowledgement_messages(reply, message):
    api = Mock()
    api.review_job.return_value = reply
    assert perform_review_job(api, "a", "a" * 64, EPOCH) == message
    api.review_job.assert_called_once_with("a", "a" * 64, EPOCH)


def test_lost_acknowledgement_is_uncertain_and_not_retried():
    api = Mock()
    api.review_request.side_effect = RuntimeError("SYNTHETIC_PRIVATE")
    assert perform_review_request(api, "restart-request", EPOCH) == ACK_UNCERTAIN
    api.review_request.assert_called_once()
    api.review_request.side_effect = None
    api.review_request.return_value = {**job_reply(), "request_id": "restart-request"}
    assert perform_review_request(api, "restart-request", EPOCH) == ACK_DONE


@pytest.mark.parametrize(
    ("reply", "message"),
    [
        ({"ok": True, "broker_epoch": EPOCH, "checked": []}, RECHECK_NOTHING),
        (
            {
                "ok": True,
                "broker_epoch": EPOCH,
                "checked": [
                    {
                        "flag": "serial_ownership",
                        "outcome": "cleared",
                        "reason": "serial_close_confirmed",
                    },
                    {
                        "flag": "build_output",
                        "outcome": "kept",
                        "reason": "no_side_effect_free_check",
                    },
                ],
            },
            "Re-check: cleared Serial port close (serial_close_confirmed). "
            "Kept Build output release (no_side_effect_free_check).",
        ),
        (
            {
                "ok": True,
                "broker_epoch": EPOCH,
                "checked": [{"flag": "execution", "outcome": "maybe", "reason": "x_y"}],
            },
            RECHECK_UNCERTAIN,
        ),
        ({"ok": True, "broker_epoch": "f" * 32, "checked": []}, RECHECK_UNCERTAIN),
        ({"ok": False, "error": "stale_operator_snapshot"}, ACK_REFUSED["stale_operator_snapshot"]),
    ],
)
def test_recheck_messages(reply, message):
    api = Mock()
    api.recheck.return_value = reply
    assert perform_recheck(api, EPOCH) == message
    api.recheck.assert_called_once_with(EPOCH)


# --- Tk adapter: buttons and confirmation ------------------------------------------------------


def controls(window):
    for name in (
        "job_cancel",
        "job_review",
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


@pytest.mark.parametrize("reviewed", [False, True])
def test_acknowledge_button_follows_the_selected_card(reviewed):
    window = live_window()
    window.state = select(ready(snapshot([review_card(reviewed=reviewed)])), "a")
    controls(window)
    (window.job_review.grid_remove if reviewed else window.job_review.grid).assert_called()
    assert window.job_review.configure.call_args.kwargs["state"] == (
        "disabled" if reviewed else "normal"
    )


@pytest.mark.parametrize("uncertain", [False, True])
def test_recheck_button_is_enabled_only_with_uncertainty(uncertain):
    window = live_window()
    state = queue_state(
        uncertainty=[{"flag": "execution", "reason": "job_final_commit_failed"}]
        if uncertain
        else []
    )
    window.state = ready(snapshot(state=state))
    controls(window)
    assert window.recheck_button.configure.call_args.kwargs["state"] == (
        "normal" if uncertain else "disabled"
    )


def test_request_button_requires_a_listed_selection():
    window = live_window()
    window.state = ready(snapshot(state=queue_state(recovery_requests=[REQUEST])))
    window.review_tree = Mock()
    window.review_tree.selection.return_value = ()
    controls(window)
    assert window.request_review.configure.call_args.kwargs["state"] == "disabled"
    window.review_tree.selection.return_value = ("restart-request",)
    controls(window)
    assert window.request_review.configure.call_args.kwargs["state"] == "normal"
    window.review_tree.selection.return_value = ("unlisted",)
    controls(window)
    assert window.request_review.configure.call_args.kwargs["state"] == "disabled"


@pytest.mark.parametrize("confirmed", [False, True])
def test_acknowledge_job_asks_first_and_sends_one_request(confirmed):
    window = live_window()
    window.state = select(ready(snapshot([review_card()])), "a")
    window._run_action = Mock()
    with patch("tkinter.messagebox.askyesno", return_value=confirmed) as ask:
        window._review_job()
    ask.assert_called_once()
    if not confirmed:
        window._run_action.assert_not_called()
        return
    window.api.review_job.return_value = job_reply()
    action = window._run_action.call_args.args[0]
    assert action() == ACK_DONE
    window.api.review_job.assert_called_once_with("a", "a" * 64, EPOCH)


def test_recheck_asks_first_and_sends_one_request():
    window = live_window()
    state = queue_state(uncertainty=[{"flag": "execution", "reason": "job_final_commit_failed"}])
    window.state = ready(snapshot(state=state))
    window._run_action = Mock()
    with patch("tkinter.messagebox.askyesno", return_value=True):
        window._recheck()
    window.api.recheck.return_value = {"ok": True, "broker_epoch": EPOCH, "checked": []}
    assert window._run_action.call_args.args[0]() == RECHECK_NOTHING
    window.api.recheck.assert_called_once_with(EPOCH)
