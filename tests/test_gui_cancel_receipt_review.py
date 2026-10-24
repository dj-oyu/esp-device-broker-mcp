"""Retained cancel controls reject malformed broker acknowledgements."""

import pytest
from test_gui_timeline import cancel_operation, loaded

from cardputer_mcp.gui_timeline import CANCEL_UNCERTAIN, perform_timeline


@pytest.mark.parametrize("field,value", [("state", "unknown"), ("stop_requested", "yes")])
def test_cancel_receipt_requires_known_state_and_boolean_stop_flag(field, value):
    timeline, api, clock = loaded(cancellable=True)
    operation = cancel_operation(timeline, clock)
    api.cancel_response = {
        "ok": True,
        "job_id": operation.job_id,
        "plan_hash": operation.plan_hash,
        "broker_epoch": operation.broker_epoch,
        "state": "running",
        "stop_requested": True,
        field: value,
    }
    result = perform_timeline(api, operation, clock)
    assert result.error == CANCEL_UNCERTAIN
    assert len(api.calls) == 2


def test_missing_cancel_stop_flag_confers_no_acknowledgement():
    timeline, api, clock = loaded(cancellable=True)
    operation = cancel_operation(timeline, clock)
    api.cancel_response = {
        "ok": True,
        "job_id": operation.job_id,
        "plan_hash": operation.plan_hash,
        "broker_epoch": operation.broker_epoch,
        "state": "running",
    }
    outcome = perform_timeline(api, operation, clock)
    assert outcome.error == CANCEL_UNCERTAIN


def test_delayed_cancel_operation_never_dispatches_after_its_ten_second_window():
    timeline, api, clock = loaded(cancellable=True)
    operation = cancel_operation(timeline, clock)
    clock.value += 10
    outcome = perform_timeline(api, operation, clock)
    assert outcome.error == CANCEL_UNCERTAIN
    assert api.calls == [("timeline",)]


def test_lost_cancel_reply_is_uncertain_and_never_automatically_retried():
    timeline, api, clock = loaded(cancellable=True)

    def lost_reply(job, digest, epoch):
        api.calls.append(("cancel_job", job, digest, epoch))
        raise RuntimeError("SYNTHETIC_PRIVATE_TRANSPORT_DETAILS")

    api.cancel_job = lost_reply
    outcome = perform_timeline(api, cancel_operation(timeline, clock), clock)
    assert outcome.error == CANCEL_UNCERTAIN
    assert "SYNTHETIC_PRIVATE" not in outcome.error
    assert len([call for call in api.calls if call[0] == "cancel_job"]) == 1
