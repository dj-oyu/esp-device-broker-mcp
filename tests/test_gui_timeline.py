"""Read-only UI integrity, retention and explicit cancellation; no device access."""

import copy
from typing import Any

from test_gui_images import detail as image_detail
from test_gui_images import row as image_row

from cardputer_mcp.gui_timeline import (
    CANCEL_UNCERTAIN,
    TimelineController,
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
    model = TimelineController(clock)
    op = model.refresh()
    assert model.accept(perform_timeline(api, op, clock))
    return model, api, clock


def test_refresh_is_only_read_and_never_approves_executes_or_recaptures():
    model, api, clock = loaded()
    assert api.calls == [("timeline",)]
    assert model.snapshot.cards[0].review is None
    assert not hasattr(model, "approve") and not hasattr(model, "decide")
    assert not model.can_cancel("job_image_1")
    assert model.load_preview("missing") is None
    assert api.calls == [("timeline",)]


def test_explicit_preview_exact_identity_without_delivery_approval():
    model, api, clock = loaded()
    operation = model.load_preview("job_image_1")
    assert operation is not None
    assert model.accept(perform_timeline(api, operation, clock))
    assert model.preview and model.preview.row.job_id == "job_image_1"
    assert api.calls == [("timeline",), ("image", "job_image_1")]


def test_stale_result_and_pending_close_do_not_release_action_guard():
    model, api, clock = loaded()
    op = model.load_preview("job_image_1")
    assert not model.can_close() and not model.stop()
    other = TimelineOperation(999, 0, "refresh", clock())
    assert not model.accept(perform_timeline(api, other, clock))
    assert model.busy
    assert model.accept(perform_timeline(api, op, clock))
    assert model.stop() and model.preview is None
    assert model.refresh() is None


def test_cancel_requires_fresh_exact_target_and_does_not_release_ownership():
    model, api, clock = loaded(cancellable=True)
    key = model.cancel_key("job_image_1")
    assert model.can_cancel("job_image_1")
    assert model.cancel_job("job_image_1", ("f" * 32, key[1], key[2])) is None
    operation = model.cancel_job("job_image_1", key)
    assert not model.can_cancel("job_image_1")
    assert model.accept(perform_timeline(api, operation, clock))
    assert not model.fresh and not model.can_cancel("job_image_1")
    assert api.calls == [("timeline",), ("cancel_job", key[1], key[2], key[0])]
    assert "release" not in str(api.calls)


def test_wrong_cancel_ack_is_uncertain_and_never_retried():
    model, api, clock = loaded(cancellable=True)
    api.cancel_response = dict(ok=True, job_id="other")
    op = model.cancel_job("job_image_1", model.cancel_key("job_image_1"))
    result = perform_timeline(api, op, clock)
    assert result.error == CANCEL_UNCERTAIN
    assert model.accept(result) and not model.can_cancel("job_image_1")
    assert len(api.calls) == 2


def test_network_errors_are_fixed_and_no_automatic_retry():
    model, api, clock = loaded()
    api.failure = True
    result = perform_timeline(api, model.refresh(), clock)
    assert "private" not in result.error
    assert model.accept(result) and not model.fresh
    assert len(api.calls) == 2
