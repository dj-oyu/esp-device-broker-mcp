"""Read-only timeline integrity and explicit cancellation; no device access."""

import copy
from typing import Any

from test_gui_images import detail as image_detail
from test_gui_images import row as image_row

from cardputer_mcp.gui_timeline import (
    CANCEL_REQUESTED,
    CANCEL_UNCERTAIN,
    TimelineOperation,
    perform_timeline,
)


class Clock:
    value = 1000.0

    def __call__(self):
        return self.value


def card(**changes: Any):
    return {
        "job_id": "job_image_1",
        "plan_hash": "a" * 64,
        "test": "capture_home",
        "backend": "mock",
        "state": "succeeded",
        "stage": None,
        "result": None,
        "created_utc": "2026-10-03T13:30:00+00:00",
        "finished_utc": None,
        "reviewed": False,
        "review": None,
        "image": None,
        "cancel_available": False,
        **changes,
    }


def snapshot(rows=None, **changes):
    return {
        "ok": True,
        "broker_epoch": "e" * 32,
        "cards": rows if rows is not None else [card()],
        "has_more": False,
        "plans_available": False,
        **changes,
    }


class FakeAPI:
    def __init__(self):
        self.data = image_detail()
        self.status = snapshot([card(image=image_row(self.data))])
        self.calls = []
        self.failure = False
        self.clock = None
        self.cancel_response = None

    def timeline(self):
        self.calls.append(("timeline",))
        if self.failure:
            raise RuntimeError("private local path")
        return copy.deepcopy(self.status)

    def image(self, job_id):
        self.calls.append(("image", job_id))
        if self.failure:
            raise RuntimeError("private local path")
        if self.clock:
            self.clock.value += 300
        return {**copy.deepcopy(self.data), "broker_epoch": self.status["broker_epoch"]}

    def cancel_job(self, job_id, plan_hash, broker_epoch):
        self.calls.append(("cancel_job", job_id, plan_hash, broker_epoch))
        return self.cancel_response or dict(
            ok=True,
            job_id=job_id,
            plan_hash=plan_hash,
            broker_epoch=broker_epoch,
            state="running",
            stop_requested=False,
        )


def loaded(*, cancellable=False):
    """Read one timeline snapshot exactly as the live window's refresh does."""
    clock, api = Clock(), FakeAPI()
    if cancellable:
        api.status = snapshot(
            [
                card(
                    test="firmware_build",
                    backend="registered_project",
                    state="running",
                    cancel_available=True,
                )
            ]
        )
    outcome = perform_timeline(api, TimelineOperation(1, 0, "refresh", clock()), clock)
    assert outcome.snapshot is not None and not outcome.error
    return outcome.snapshot, api, clock


def cancel_operation(timeline, clock, job_id="job_image_1"):
    """Build the cancel operation the live window sends for the selected card."""
    selected = next(row for row in timeline.cards if row.job_id == job_id)
    return TimelineOperation(
        2,
        0,
        "cancel_job",
        clock(),
        job_id,
        selected.plan_hash or "",
        timeline.broker_epoch,
    )


def test_refresh_is_only_read_and_never_approves_executes_or_recaptures():
    timeline, api, clock = loaded()
    assert api.calls == [("timeline",)]
    assert timeline.cards[0].review is None
    assert not timeline.cards[0].cancel_available


def test_explicit_preview_exact_identity_without_delivery_approval():
    timeline, api, clock = loaded()
    row = timeline.cards[0].image
    operation = TimelineOperation(
        2, 0, "preview", clock(), "job_image_1", broker_epoch=timeline.broker_epoch, row=row
    )
    outcome = perform_timeline(api, operation, clock)
    assert not outcome.error
    assert outcome.preview and outcome.preview.row.job_id == "job_image_1"
    assert api.calls == [("timeline",), ("image", "job_image_1")]


def test_cancel_sends_the_exact_target_and_does_not_release_ownership():
    timeline, api, clock = loaded(cancellable=True)
    outcome = perform_timeline(api, cancel_operation(timeline, clock), clock)
    assert outcome.message == CANCEL_REQUESTED and not outcome.error
    plan_hash = timeline.cards[0].plan_hash
    assert api.calls == [
        ("timeline",),
        ("cancel_job", "job_image_1", plan_hash, timeline.broker_epoch),
    ]
    assert "release" not in str(api.calls)


def test_wrong_cancel_ack_is_uncertain_and_never_retried():
    timeline, api, clock = loaded(cancellable=True)
    api.cancel_response = dict(ok=True, job_id="other")
    result = perform_timeline(api, cancel_operation(timeline, clock), clock)
    assert result.error == CANCEL_UNCERTAIN
    assert len(api.calls) == 2


def test_network_errors_are_fixed_and_no_automatic_retry():
    clock, api = Clock(), FakeAPI()
    api.failure = True
    result = perform_timeline(api, TimelineOperation(1, 0, "refresh", clock()), clock)
    assert result.error and "private" not in result.error
    assert result.snapshot is None
    assert len(api.calls) == 1
