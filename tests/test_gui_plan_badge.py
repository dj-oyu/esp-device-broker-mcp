"""Authenticated snapshot badges with fake time; no Tk display or device."""

import queue
import threading
from dataclasses import replace
from unittest.mock import Mock

import pytest

from cardputer_mcp.gui import Operation, Outcome, QueueController, _Window, perform
from cardputer_mcp.gui_approvals import PlanList
from cardputer_mcp.gui_plan_badge import MAX_SNAPSHOT_AGE, PlanBadge
from cardputer_mcp.operator_client import OperatorClient


def listing(*numbers, started=100, remaining=30, epoch="a" * 32, more=False):
    return PlanList.parse(
        {
            "ok": True,
            "broker_epoch": epoch,
            "plans": [
                {
                    "plan_hash": f"{number:064x}",
                    "job_id": f"job-{number}",
                    "test": "capture_home",
                    "backend": "mock",
                    "lease_remaining_seconds": remaining,
                }
                for number in numbers
            ],
            "has_more": more,
        },
        started,
    )


def test_startup_error_and_stale_never_claim_zero():
    badge = PlanBadge()
    assert badge.view(100).label == "Pending (?)"
    badge.update(listing(1), 100, 100)
    assert badge.view(100).label == "Pending (1) NEW"
    badge.update(None, 101, 101)
    assert badge.view(101).label == "Pending (?)"
    badge.update(listing(1, started=102), 102, 102)
    assert badge.view(102 + MAX_SNAPSHOT_AGE).label == "Pending (?)"
    badge.update(listing(), 100, 110)
    assert badge.view(110).label == "Pending (?)"


def test_new_badge_is_acknowledged_without_approving_and_recovers_without_duplicate():
    badge = PlanBadge()
    badge.update(listing(1), 100, 100)
    badge.acknowledge(100)
    assert badge.view(100).label == "Pending (1)"
    badge.update(listing(1, started=101), 101, 101)
    assert not badge.view(101).new
    badge.update(None, 102, 102)
    badge.update(listing(1, started=103), 103, 103)
    assert not badge.view(103).new
    badge.update(listing(1, 2, started=104), 104, 104)
    assert badge.view(104).label == "Pending (2) NEW"


@pytest.mark.parametrize("reason", ["approved", "policy_reappeared", "new_epoch"])
def test_authoritative_removal_and_reappearance(reason):
    badge = PlanBadge()
    badge.update(listing(1), 100, 100)
    badge.acknowledge(100)
    badge.update(listing(started=101), 101, 101)
    assert badge.view(101).label == "Pending (0)"
    if reason != "approved":
        badge.update(
            listing(1, started=102, epoch="b" * 32 if reason == "new_epoch" else "a" * 32),
            102,
            102,
        )
        assert badge.view(102).label == "Pending (1) NEW"


def test_expired_rows_drop_locally_and_renewed_row_is_new():
    badge = PlanBadge()
    badge.update(listing(1, remaining=1), 100, 100)
    badge.acknowledge(100)
    assert badge.view(101).label == "Pending (?)"
    badge.update(listing(1, started=102), 102, 102)
    assert badge.view(102).label == "Pending (1) NEW"


def test_changed_epoch_marks_even_same_public_hash_as_new():
    badge = PlanBadge()
    badge.update(listing(1), 100, 100)
    badge.acknowledge(100)
    badge.update(listing(1, started=101, epoch="b" * 32), 101, 101)
    assert badge.view(101).new


def test_bounded_list_shows_lower_bound_and_unknown_after_visible_expiry():
    badge = PlanBadge()
    badge.update(listing(*range(64), remaining=1, more=True), 100, 100)
    assert badge.view(100).label == "Pending (64+) NEW"
    assert len(badge.known) == len(badge.unread) == 64
    assert badge.view(101).label == "Pending (?)"


def api():
    instance = Mock(spec=OperatorClient)
    instance.state.return_value = {
        "backend": "mock",
        "queue_revision": "revision",
        "sessions": [],
        "waiters": [],
        "owner": None,
        "needs_review": False,
        "ownership_uncertain": False,
    }
    instance.plans.return_value = {
        "ok": True,
        "broker_epoch": "a" * 32,
        "plans": [],
        "has_more": False,
    }
    return instance


def test_count_read_is_on_existing_worker_and_contains_only_bounded_parsed_data():
    instance = api()
    instance.plans.return_value["private"] = "synthetic-secret"
    called_threads = []
    response = instance.plans.return_value
    instance.plans.side_effect = lambda: called_threads.append(threading.get_ident()) or response
    window = object.__new__(_Window)
    window.api, window.stopped = instance, threading.Event()
    window.commands, window.results = queue.Queue(), queue.Queue()
    worker = threading.Thread(target=window._worker)
    worker.start()
    window.commands.put(Operation(1, "refresh"))
    result = window.results.get(timeout=2)
    window.stopped.set()
    window.commands.put(None)
    worker.join(timeout=2)
    assert not worker.is_alive()
    assert called_threads == [worker.ident] and worker.ident != threading.get_ident()
    assert [call[0] for call in instance.mock_calls] == ["state", "plans"]
    assert result.plans_checked and result.plans is not None
    assert "synthetic-secret" not in repr(result)


@pytest.mark.parametrize("failure", ["exception", "malformed", "queue_error"])
def test_poll_error_is_unknown_and_never_echoes_payload(failure):
    instance = api()
    if failure == "exception":
        instance.plans.side_effect = RuntimeError("synthetic-secret")
    elif failure == "malformed":
        instance.plans.return_value = {"ok": True, "plans": "synthetic-secret"}
    else:
        instance.state.side_effect = RuntimeError("synthetic-secret")
    result = perform(instance, Operation(1, "refresh"))
    assert result.plans_checked and result.plans is None
    assert "synthetic-secret" not in repr(result)
    if failure == "queue_error":
        instance.plans.assert_not_called()
    else:
        assert result.snapshot is not None and not result.failed


def test_click_only_acknowledges_badge_and_opens_inspector():
    window = object.__new__(_Window)
    window.plan_badge = Mock()
    window.timeline_panel = Mock()
    window.api = Mock(spec=OperatorClient)
    window._render_plan_badge = Mock()
    window._show_plans()
    window.plan_badge.acknowledge.assert_called_once()
    window.timeline_panel.focus_pending.assert_called_once()
    assert window.api.mock_calls == []


@pytest.mark.parametrize("disposition", ["accepted", "stale", "closed", "stopped"])
def test_only_current_poll_result_updates_badge(disposition):
    window = object.__new__(_Window)
    window.controller = QueueController()
    window.stopped, window.results, window.root = threading.Event(), queue.Queue(), Mock()
    window.plan_badge = Mock()
    window._render, window._render_plan_badge = Mock(), Mock()
    operation = window.controller.refresh()
    outcome = replace(perform(api(), operation), plans=listing(1))
    window.results.put(outcome)
    window.results.put(outcome)
    if disposition == "stale":
        window.controller.accept(outcome)
        window.controller.refresh()
    elif disposition == "closed":
        window.controller.close()
    elif disposition == "stopped":
        window.stopped.set()
    window._poll()
    assert window.plan_badge.update.call_count == int(disposition == "accepted")


def test_busy_auto_refresh_cannot_build_a_poll_backlog():
    window = object.__new__(_Window)
    window.stopped, window.root = threading.Event(), Mock()
    window.controller = QueueController()
    window.controller.refresh()
    window._refresh = Mock()
    for _ in range(100):
        window._auto_refresh()
    window._refresh.assert_not_called()
    assert window.root.after.call_count == 100


def test_badge_renders_only_on_change_with_expiry_and_new_emphasis(monkeypatch):
    import cardputer_mcp.gui as gui

    window = object.__new__(_Window)
    window.plan_badge = PlanBadge()
    window._badge_view = window.plan_badge.view(100)
    window.plans_button, window.plans_notice = Mock(), Mock()
    monkeypatch.setattr(gui.time, "monotonic", lambda: 100)
    window.plan_badge.update(listing(1, remaining=1), 100, 100)
    window._render_plan_badge()
    window.plans_button.configure.assert_called_once_with(
        text="Pending (1) NEW", style="NewPlans.TButton"
    )
    window._render_plan_badge()
    assert window.plans_button.configure.call_count == 1
    monkeypatch.setattr(gui.time, "monotonic", lambda: 101)
    window._render_plan_badge()
    window.plans_button.configure.assert_called_with(text="Pending (?)", style="TButton")


@pytest.mark.parametrize("dialog", ["catalog", "diagnostics"])
def test_modal_close_does_not_render_destroyed_badge_widgets(dialog):
    window = object.__new__(_Window)
    window.controller = QueueController()
    window.stopped, window.results, window.root = threading.Event(), queue.Queue(), Mock()
    window._render, window._render_plan_badge = Mock(), Mock()
    window._show_catalog = Mock(side_effect=lambda *_: window.stopped.set())
    window._show_diagnostics = Mock(side_effect=lambda *_: window.stopped.set())
    operation = getattr(window.controller, dialog)()
    window.results.put(Outcome(operation.sequence, None, "loaded", **{dialog: "safe summary"}))
    window._poll()
    window._render_plan_badge.assert_not_called()
    window.root.after.assert_not_called()


def test_main_close_waits_for_inline_decision_without_touching_broker_or_tunnel():
    window = object.__new__(_Window)
    window.timeline_panel = Mock()
    window.timeline_panel.can_close_window.return_value = False
    window.tunnel_panel, window.root = Mock(), Mock()
    window.api = Mock(spec=OperatorClient)
    window._close()
    window.timeline_panel.can_close_window.assert_called_once()
    window.root.destroy.assert_not_called()
    window.tunnel_panel.request_close.assert_not_called()
    assert window.api.mock_calls == []


def test_main_close_rechecks_inline_decision_after_tunnel_close_delay():
    window = object.__new__(_Window)
    window.timeline_panel = Mock()
    window.timeline_panel.can_close_window.return_value = False
    window.root = Mock()
    window.controller = QueueController()
    window.stopped, window.commands = threading.Event(), queue.Queue()
    window._finish_close()
    assert not window.stopped.is_set() and not window.controller.closed
    window.timeline_panel.stop.assert_not_called()
    window.root.destroy.assert_not_called()


def test_main_refresh_requests_queue_and_same_inline_timeline_without_navigation():
    window = object.__new__(_Window)
    window.controller = QueueController()
    window.timeline_panel = Mock()
    window._submit = Mock()
    window._refresh()
    window.timeline_panel.refresh.assert_called_once()
    assert window._submit.call_args.args[0].action == "refresh"


def test_queue_auto_refresh_does_not_trigger_explicit_timeline_preview_retry():
    window = object.__new__(_Window)
    window.controller = QueueController()
    window.stopped, window.root = threading.Event(), Mock()
    window.timeline_panel, window._submit = Mock(), Mock()
    window._auto_refresh()
    window.timeline_panel.refresh.assert_not_called()
    assert window._submit.call_args.args[0].action == "refresh"
