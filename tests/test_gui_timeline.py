"""Read-only UI integrity, retention and explicit cancellation; no device access."""

import copy
from typing import Any

import pytest
from test_gui_images import detail as image_detail
from test_gui_images import row as image_row

from cardputer_mcp.gui_timeline import (
    CANCEL_UNCERTAIN,
    PREVIEW_FAILED,
    TimelineController,
    TimelineOperation,
    TimelineSnapshot,
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


@pytest.mark.parametrize(
    "field,value",
    [
        ("job_id", "other"),
        ("recipient_principal", "other"),
        ("sha256", "b" * 64),
    ],
)
def test_changed_preview_target_is_rejected_without_retry(field, value):
    model, api, clock = loaded()
    operation = model.load_preview("job_image_1")
    api.data[field] = value
    outcome = perform_timeline(api, operation, clock)
    assert outcome.error == PREVIEW_FAILED
    assert model.accept(outcome) and model.preview is None
    assert len(api.calls) == 2


def test_epoch_change_between_list_and_preview_is_rejected():
    model, api, clock = loaded()
    operation = model.load_preview("job_image_1")
    api.status["broker_epoch"] = "f" * 32
    assert model.accept(perform_timeline(api, operation, clock))
    assert model.preview is None


def test_expiration_before_network_and_during_read_never_retains_pixels():
    model, api, clock = loaded()
    operation = model.load_preview("job_image_1")
    clock.value += 200
    assert model.accept(perform_timeline(api, operation, clock))
    assert api.calls == [("timeline",)]
    assert model.preview is None
    model, api, clock = loaded()
    api.clock = clock
    operation = model.load_preview("job_image_1")
    assert model.accept(perform_timeline(api, operation, clock))
    assert model.preview is None and len(api.calls) == 2


def test_expiry_and_restart_clear_local_preview():
    model, api, clock = loaded()
    assert model.accept(perform_timeline(api, model.load_preview("job_image_1"), clock))
    assert model.preview
    clock.value += 200
    model.expire()
    assert model.preview is None
    model, api, clock = loaded()
    assert model.accept(perform_timeline(api, model.load_preview("job_image_1"), clock))
    api.status["broker_epoch"] = "f" * 32
    assert model.accept(perform_timeline(api, model.refresh(), clock))
    assert model.preview is None and model.generation == 1


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


@pytest.mark.parametrize(
    "change",
    [
        {"cards": [] * 33, "broker_epoch": "bad"},
        {"cards": [card()] * 33},
        {"cards": [card()] * 2},
        {"cards": [card(review={"approval": "old"})]},
        {"cards": [card(cancel_available=True)]},
    ],
)
def test_unbounded_wrong_epoch_or_obsolete_action_projection_rejected(change):
    data = snapshot()
    data.update(change)
    with pytest.raises(ValueError):
        TimelineSnapshot.parse(data, 1000)


def test_network_errors_are_fixed_and_no_automatic_retry():
    model, api, clock = loaded()
    api.failure = True
    result = perform_timeline(api, model.refresh(), clock)
    assert "private" not in result.error
    assert model.accept(result) and not model.fresh
    assert len(api.calls) == 2


def test_selection_change_invalidates_pending_and_displayed_preview():
    model, api, clock = loaded()
    operation = model.load_preview("job_image_1")
    model.select("other")
    assert model.accept(perform_timeline(api, operation, clock))
    assert not model.busy and model.preview is None
    operation = model.load_preview("job_image_1")
    assert model.accept(perform_timeline(api, operation, clock))
    assert model.preview
    model.select("other")
    assert model.preview is None


def test_timeline_worker_only_reads_on_background_thread_without_tk():
    import queue
    import threading

    from cardputer_mcp.gui_timeline import TimelinePanel

    api = FakeAPI()
    called = []
    original = api.timeline

    def tracked():
        called.append(threading.get_ident())
        return original()

    api.timeline = tracked
    panel = object.__new__(TimelinePanel)
    panel.api = api
    panel.commands = queue.Queue()
    panel.results = queue.Queue()
    worker = threading.Thread(target=panel._worker)
    worker.start()
    assert not called
    panel.commands.put(TimelineOperation(1, 0, "refresh", 1000))
    outcome = panel.results.get(timeout=2)
    assert outcome.snapshot and called == [worker.ident]
    assert threading.get_ident() not in called
    panel.commands.put(None)
    worker.join(timeout=2)
    assert not worker.is_alive()
    assert api.calls == [("timeline",)]
