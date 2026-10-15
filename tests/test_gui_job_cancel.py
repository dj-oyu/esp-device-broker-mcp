"""Exact-job cancellation presentation; no device, network or cancellation replay."""

import copy

import pytest
from test_gui_timeline import Clock, FakeAPI, card, panel_fixture, refresh, snapshot, visible

from cardputer_mcp.gui_timeline import (
    CANCEL_PENDING,
    CANCEL_REJECTED,
    CANCEL_REQUESTED,
    CANCEL_REVIEW,
    CANCEL_UNCERTAIN,
    TimelineController,
    TimelineSnapshot,
    perform_timeline,
)


def running(job="build-job", **changes):
    row = card(job, backend="registered_project", test="firmware_build")
    row.update(state="running", review=None, stage="idf_build", cancel_available=True)
    row.update(changes)
    return row


class CancelAPI(FakeAPI):
    response = None

    def cancel_job(self, job_id, plan_hash, broker_epoch):
        self.called("cancel_job", job_id, plan_hash, broker_epoch)
        if self.response is not None:
            return copy.deepcopy(self.response)
        return dict(
            ok=True,
            job_id=job_id,
            plan_hash=plan_hash,
            broker_epoch=broker_epoch,
            state="running",
            stop_requested=True,
        )


def loaded():
    clock, api = Clock(), CancelAPI(running())
    model = TimelineController(clock)
    refresh(model, api, clock)
    return model, api, clock


def submit(model):
    return model.cancel_job("build-job", model.cancel_key("build-job"))


def test_double_click_sends_once_and_waits_for_authoritative_cleanup():
    model, api, clock = loaded()
    operation = submit(model)
    assert operation is not None and operation.mutates
    assert submit(model) is None
    assert model.cancel_messages["build-job"] == CANCEL_PENDING
    assert model.accept(perform_timeline(api, operation, clock))
    assert model.cancel_messages["build-job"] == CANCEL_REQUESTED
    assert model.card("build-job").state == "running"
    refresh(model, api, clock)
    assert model.cancel_messages["build-job"] == CANCEL_REQUESTED
    assert not model.can_cancel("build-job") and submit(model) is None
    assert len([call for call in api.calls if call[0] == "cancel_job"]) == 1


@pytest.mark.parametrize("terminal", ["succeeded", "failed", "cancelled", "needs_review"])
def test_completion_race_uses_fresh_terminal_state_without_rewriting_history(terminal):
    model, api, clock = loaded()
    operation = submit(model)
    api.response = dict(
        ok=True,
        job_id=operation.job_id,
        plan_hash=operation.plan_hash,
        broker_epoch=operation.broker_epoch,
        state=terminal,
        stop_requested=False,
    )
    assert model.accept(perform_timeline(api, operation, clock))
    assert model.card("build-job").state == "running"
    api.data = snapshot(running(state=terminal, cancel_available=False))
    refresh(model, api, clock)
    assert model.card("build-job").state == terminal
    assert not model.can_cancel("build-job")
    if terminal == "needs_review":
        assert model.cancel_messages["build-job"] == CANCEL_REVIEW
    if terminal == "succeeded":
        assert "No rollback" in model.cancel_messages["build-job"]


def test_cleanup_uncertainty_never_claims_stop_or_usb_release():
    model, api, clock = loaded()
    operation = submit(model)
    model.accept(perform_timeline(api, operation, clock))
    api.data = snapshot(
        running(
            state="needs_review", cancel_available=False, result="build_output_release_unconfirmed"
        )
    )
    refresh(model, api, clock)
    assert model.cancel_messages["build-job"] == CANCEL_REVIEW
    assert model.card("build-job").needs_device_review
    assert model.card("build-job").result == "build_output_release_unconfirmed"


def test_rejected_cancel_has_fixed_message_and_no_automatic_retry():
    model, api, clock = loaded()
    api.response = {"ok": False, "error": "synthetic private token detail"}
    operation = submit(model)
    outcome = perform_timeline(api, operation, clock)
    assert outcome.error == CANCEL_REJECTED
    assert model.accept(outcome)
    refresh(model, api, clock)
    assert model.cancel_messages["build-job"] == CANCEL_REJECTED
    assert submit(model) is None
    assert len([call for call in api.calls if call[0] == "cancel_job"]) == 1


def test_lost_response_is_uncertain_and_not_replayed_after_refresh():
    model, api, clock = loaded()
    api.fail.add("cancel_job")
    operation = submit(model)
    assert model.accept(perform_timeline(api, operation, clock))
    assert model.cancel_messages["build-job"] == CANCEL_UNCERTAIN
    refresh(model, api, clock)
    assert submit(model) is None
    assert len([call for call in api.calls if call[0] == "cancel_job"]) == 1


def test_delayed_worker_does_not_cancel_a_stale_target():
    model, api, clock = loaded()
    operation = submit(model)
    clock.value += 10
    assert perform_timeline(api, operation, clock).error == CANCEL_UNCERTAIN
    assert not any(call[0] == "cancel_job" for call in api.calls)


@pytest.mark.parametrize(
    "field,value",
    [
        ("job_id", "other-job"),
        ("plan_hash", "0" * 64),
        ("broker_epoch", "0" * 32),
        ("stop_requested", "yes"),
        ("state", "unknown"),
    ],
)
def test_wrong_or_malformed_cancel_receipt_confers_no_success(field, value):
    model, api, clock = loaded()
    operation = submit(model)
    api.response = dict(
        ok=True,
        job_id=operation.job_id,
        plan_hash=operation.plan_hash,
        broker_epoch=operation.broker_epoch,
        state="running",
        stop_requested=True,
    )
    api.response[field] = value
    assert perform_timeline(api, operation, clock).error == CANCEL_UNCERTAIN
    assert model.card("build-job").state == "running"


def test_fake_tk_button_names_exact_job_and_cannot_replay_armed_click(monkeypatch):
    panel, api, clock, _photos = panel_fixture(monkeypatch, running())
    view = panel.views["build-job"]
    assert visible(view.cancel_button)
    assert view.cancel_button.config["text"] == "Cancel job build-job"
    assert view.cancel_button.config["state"] == "normal"
    assert not view.expanded  # Cancellation is visible in the collapsed header.
    view._arm_cancel()
    view._cancel_job()
    operation = panel.worker.commands.get_nowait()
    assert operation.job_id == "build-job" and operation.action == "cancel_job"
    view._cancel_job()
    view._arm_cancel()
    view._cancel_job()
    assert panel.worker.commands.empty()
    panel._render()
    assert view.cancel_button.config["state"] == "disabled"
    assert "build-job" in view.next_var.get() and "not confirmed" in view.next_var.get()


def test_completion_between_button_press_and_invoke_does_not_cancel(monkeypatch):
    panel, api, clock, _photos = panel_fixture(monkeypatch, running())
    view = panel.views["build-job"]
    view._arm_cancel()
    api.data = snapshot(running(state="succeeded", cancel_available=False))
    refresh(panel.controller, api, clock)
    panel._render()
    view._cancel_job()
    assert panel.worker.commands.empty() and panel.controller.pending is None


@pytest.mark.parametrize(
    "changes",
    [
        {"cancel_available": "yes"},
        {"state": "succeeded"},
        {"backend": "serial"},
        {"test": "firmware_flash", "backend": "serial"},
    ],
)
def test_cancel_advertisement_cannot_enable_unsupported_job(changes):
    with pytest.raises(ValueError):
        TimelineSnapshot.parse(snapshot(running(**changes)), 1000)
