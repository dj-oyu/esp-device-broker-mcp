"""Retained cancel controls reject malformed broker acknowledgements."""

from dataclasses import replace

import pytest
from test_gui_timeline import loaded

from cardputer_mcp.gui_timeline import CANCEL_UNCERTAIN, perform_timeline


@pytest.mark.parametrize("field,value", [("state", "unknown"), ("stop_requested", "yes")])
def test_cancel_receipt_requires_known_state_and_boolean_stop_flag(field, value):
    model, api, clock = loaded(cancellable=True)
    epoch, job, digest = model.cancel_key("job_image_1")
    api.cancel_response = {
        "ok": True,
        "job_id": job,
        "plan_hash": digest,
        "broker_epoch": epoch,
        "state": "running",
        "stop_requested": True,
        field: value,
    }
    result = perform_timeline(api, model.cancel_job(job, (epoch, job, digest)), clock)
    assert result.error == CANCEL_UNCERTAIN
    assert model.accept(result) and not model.can_cancel(job)
    assert len(api.calls) == 2


def test_missing_cancel_stop_flag_confers_no_acknowledgement():
    model, api, clock = loaded(cancellable=True)
    epoch, job, digest = model.cancel_key("job_image_1")
    api.cancel_response = {
        "ok": True,
        "job_id": job,
        "plan_hash": digest,
        "broker_epoch": epoch,
        "state": "running",
    }
    outcome = perform_timeline(api, model.cancel_job(job, (epoch, job, digest)), clock)
    assert outcome.error == CANCEL_UNCERTAIN
    assert model.accept(outcome) and not model.can_cancel(job)


def test_delayed_cancel_operation_never_dispatches_after_its_ten_second_window():
    model, api, clock = loaded(cancellable=True)
    operation = model.cancel_job("job_image_1", model.cancel_key("job_image_1"))
    clock.value += 10
    outcome = perform_timeline(api, operation, clock)
    assert outcome.error == CANCEL_UNCERTAIN
    assert api.calls == [("timeline",)]


@pytest.mark.parametrize(
    "field,value",
    [
        ("job_id", "bad/job"),
        ("plan_hash", "not-a-hash"),
        ("broker_epoch", "bad"),
    ],
)
def test_malformed_cancel_target_is_rejected_before_network(field, value):
    model, api, clock = loaded(cancellable=True)
    operation = model.cancel_job("job_image_1", model.cancel_key("job_image_1"))
    outcome = perform_timeline(api, replace(operation, **{field: value}), clock)
    assert outcome.error == CANCEL_UNCERTAIN
    assert api.calls == [("timeline",)]


def test_lost_cancel_reply_and_duplicate_outcome_never_automatically_retry():
    model, api, clock = loaded(cancellable=True)

    def lost_reply(job, digest, epoch):
        api.calls.append(("cancel_job", job, digest, epoch))
        raise RuntimeError("SYNTHETIC_PRIVATE_TRANSPORT_DETAILS")

    api.cancel_job = lost_reply
    operation = model.cancel_job("job_image_1", model.cancel_key("job_image_1"))
    outcome = perform_timeline(api, operation, clock)
    assert outcome.error == CANCEL_UNCERTAIN
    assert "SYNTHETIC_PRIVATE" not in outcome.error
    assert model.accept(outcome)
    assert not model.accept(outcome)
    assert not model.can_cancel("job_image_1")
    assert len([call for call in api.calls if call[0] == "cancel_job"]) == 1
    # Only a fresh, explicit refresh restores the possibility of a new click.
    assert model.accept(perform_timeline(api, model.refresh(), clock))
    assert model.can_cancel("job_image_1")
    assert len([call for call in api.calls if call[0] == "cancel_job"]) == 1
