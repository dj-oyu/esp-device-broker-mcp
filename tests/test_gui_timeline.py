"""Headless timeline, exact-target, fake-Tk and worker tests. No device or network."""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import queue
import sys
import threading
import time
from dataclasses import replace
from datetime import datetime
from types import SimpleNamespace
from typing import Any

import pytest

from cardputer_mcp import gui_timeline
from cardputer_mcp.capture import mock_image
from cardputer_mcp.gui_approvals import APPROVAL_NOTICE, APPROVAL_UNCERTAIN, ATTESTATIONS
from cardputer_mcp.gui_images import DECISION_FAILED, ImagePreview
from cardputer_mcp.gui_timeline import (
    EXPIRED_IMAGE,
    EXPIRED_REVIEW,
    INITIAL_FAILED,
    INVALID_TIMELINE,
    MAX_CARDS,
    PENDING_CLOSE,
    PREVIEW_FAILED,
    STALE_TIMELINE,
    TimelineController,
    TimelineOperation,
    TimelineOutcome,
    TimelinePanel,
    TimelineSnapshot,
    TimelineWorker,
    perform_timeline,
    review_key,
)


class Clock:
    def __init__(self) -> None:
        self.value = 1000.0

    def __call__(self) -> float:
        return self.value


def card(
    job: str = "job-a", *, backend: str = "mock", test: str = "capture_home"
) -> dict[str, Any]:
    plan = {
        "version": 1,
        "broker_epoch": "e" * 32,
        "session_id": "b" * 32,
        "backend": backend,
        "test": test,
        "job_id": job,
        "device": "mock-cardputer" if backend == "mock" else "configured-cardputer",
        **({"port": "COM7"} if backend == "serial" else {}),
        "stages": ["open", "work", "close"],
    }
    digest = hashlib.sha256(
        json.dumps(plan, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    attestation = (
        "device_home_firmware"
        if backend == "serial" and test == "capture_home"
        else "device_free_firmware"
        if backend == "serial" and test == "hello"
        else "none"
    )
    return {
        "job_id": job,
        "plan_hash": digest,
        "test": test,
        "backend": backend,
        "state": "approval_required",
        "stage": None,
        "result": None,
        "created_utc": None,
        "finished_utc": None,
        "reviewed": False,
        "image": None,
        "review": {
            "plan": plan,
            "plan_hash": digest,
            "review_revision": "d" * 64,
            "lease_remaining_seconds": 30.0,
            "attestation": attestation,
        },
    }


def image(job: str = "job-a", **changes: Any) -> dict[str, Any]:
    capture = mock_image()
    return {
        "ok": True,
        "job_id": job,
        "sha256": capture.sha256,
        "metadata": capture.metadata(),
        "image_base64": base64.b64encode(capture.png).decode(),
        "recipient_principal": "key:" + "a" * 32,
        "born_utc": "2026-10-03T13:30:00+00:00",
        "remaining_seconds": 200.0,
        "delivery_revision": "c" * 64,
        "delivery_state": "pending",
        "policy_allowed": False,
        **changes,
    }


def image_row(data: dict[str, Any]) -> dict[str, Any]:
    return {
        key: data[key]
        for key in (
            "job_id",
            "sha256",
            "recipient_principal",
            "born_utc",
            "remaining_seconds",
            "delivery_state",
            "policy_allowed",
        )
    } | {"source": data["metadata"]["source"]}


def completed(job: str = "job-a", data: dict[str, Any] | None = None) -> dict[str, Any]:
    return card(job) | {
        "state": "succeeded",
        "review": None,
        "stage": "mock_capture",
        "result": "synthetic_capture_not_device",
        "image": image_row(data or image(job)),
        "finished_utc": "2026-10-03T13:30:00Z",
    }


def snapshot(*cards: dict[str, Any]) -> dict[str, Any]:
    return {
        "ok": True,
        "broker_epoch": "e" * 32,
        "cards": list(cards),
        "has_more": False,
        "plans_available": True,
    }


class FakeAPI:
    def __init__(self, *cards: dict[str, Any]) -> None:
        self.data = snapshot(*(cards or (card(),)))
        self.images = {row["job_id"]: image(row["job_id"]) for row in cards if row["image"]}
        self.calls: list[tuple[Any, ...]] = []
        self.threads: list[int] = []
        self.fail: set[str] = set()

    def called(self, *args: Any) -> None:
        self.calls.append(args)
        self.threads.append(threading.get_ident())
        if args[0] in self.fail:
            raise RuntimeError("private token secret body")

    def timeline(self) -> dict[str, Any]:
        self.called("timeline")
        return copy.deepcopy(self.data)

    def approve(self, plan_hash: str, review_revision: str, attestation: str) -> dict[str, Any]:
        self.called("approve", plan_hash, review_revision, attestation)
        for row in self.data["cards"]:
            if row["plan_hash"] == plan_hash:
                row.update(review=None, state="approved")
        return {"ok": True, "approved": True, "plan_hash": plan_hash, "already_approved": False}

    def image(self, job_id: str) -> dict[str, Any]:
        self.called("image", job_id)
        return copy.deepcopy(self.images[job_id])

    def decide_image(self, job: str, sha: str, revision: str, decision: str) -> dict[str, Any]:
        self.called("decide_image", job, sha, revision, decision)
        state = "approved" if decision == "approve" else "rejected"
        self.images[job]["delivery_state"] = state
        next(row for row in self.data["cards"] if row["job_id"] == job)["image"][
            "delivery_state"
        ] = state
        return {"ok": True, "job_id": job, "delivery_state": state}


def refresh(model: TimelineController, api: FakeAPI, clock: Clock, *, retry: bool = False) -> None:
    operation = model.refresh(retry_previews=retry)
    assert operation is not None
    assert model.accept(perform_timeline(api, operation, clock))


def loaded(*cards: dict[str, Any]) -> tuple[TimelineController, FakeAPI, Clock]:
    clock, api = Clock(), FakeAPI(*cards)
    model = TimelineController(clock)
    refresh(model, api, clock)
    return model, api, clock


def display_review(model: TimelineController, job: str = "job-a") -> Any:
    row = model.card(job)
    assert row is not None and row.review is not None
    assert model.mark_review_displayed(job, row.review)
    return row.review


def preview(model: TimelineController, api: FakeAPI, clock: Clock) -> ImagePreview:
    operation = model.next_preview()
    assert operation is not None
    assert model.accept(perform_timeline(api, operation, clock))
    result = model.previews[operation.job_id]
    assert model.mark_displayed(operation.job_id, result)
    return result


@pytest.mark.parametrize(
    "field,value",
    [
        ("state", "private injected state"),
        ("stage", "secret"),
        ("result", "secret"),
        ("job_id", "bad\nheader"),
        ("test", "injected"),
        ("backend", "network"),
        ("plan_hash", "z" * 64),
        ("created_utc", "2026-10-03"),
        ("finished_utc", "2026-10-03T13:00:00+01:00"),
        ("reviewed", None),
        ("reviewed", 0),
        ("reviewed", 1),
        ("reviewed", "true"),
        ("reviewed", []),
        ("reviewed", {}),
    ],
)
def test_snapshot_rejects_unknown_or_unsafe_card_values(field: str, value: Any) -> None:
    row = card()
    row[field] = value
    with pytest.raises(ValueError, match=INVALID_TIMELINE):
        TimelineSnapshot.parse(snapshot(row), 1000)


@pytest.mark.parametrize(
    "change",
    [
        {"cards": [card()] * 33},
        {"cards": [card(), card()]},
        {"has_more": "true"},
        {"ok": False},
        {"broker_epoch": "bad"},
        {"plans_available": False},
    ],
)
def test_snapshot_bounds_and_flags_fail_closed(change: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match=INVALID_TIMELINE):
        TimelineSnapshot.parse(snapshot(card()) | change, 1000)


def test_older_snapshot_without_reviewed_flag_keeps_device_review_required() -> None:
    row = card() | {"state": "needs_review", "review": None}
    del row["reviewed"]
    parsed = TimelineSnapshot.parse(snapshot(row), 1000).cards[0]
    assert parsed.reviewed is False and parsed.needs_device_review


def test_unknown_historical_metadata_does_not_hide_healthy_cards_or_retained_image() -> None:
    unknown = completed("old") | {"test": None, "backend": None, "plan_hash": None}
    parsed = TimelineSnapshot.parse(snapshot(unknown, card()), 1000)
    assert parsed.cards[0].image is not None and parsed.cards[0].review is None
    assert parsed.cards[1].review is not None


def test_review_is_rejected_for_prepared_state_without_manual_approval_requirement() -> None:
    with pytest.raises(ValueError, match=INVALID_TIMELINE):
        TimelineSnapshot.parse(snapshot(card() | {"state": "prepared"}), 1000)


@pytest.mark.parametrize("kind", ["epoch", "job", "hash", "test", "state"])
def test_embedded_review_must_match_exact_card_and_epoch(kind: str) -> None:
    row = card()
    if kind == "epoch":
        value = snapshot(row) | {"broker_epoch": "a" * 32}
    else:
        row[{"job": "job_id", "hash": "plan_hash", "test": "test", "state": "state"}[kind]] = {
            "job": "another",
            "hash": "a" * 64,
            "test": "hello",
            "state": "approved",
        }[kind]
        value = snapshot(row)
    with pytest.raises(ValueError, match=INVALID_TIMELINE):
        TimelineSnapshot.parse(value, 1000)


def test_one_click_requires_visible_exact_review_and_never_executes() -> None:
    model, api, clock = loaded()
    assert not model.can_approve("job-a")
    review = display_review(model)
    operation = model.approve("job-a", review_key(review), "")
    assert operation is not None and operation.action == "approve_plan"
    assert model.approve("job-a", review_key(review), "") is None
    assert model.refresh() is None
    assert model.accept(perform_timeline(api, operation, clock))
    assert not model.can_approve("job-a")
    assert api.calls == [("timeline",), ("approve", review.plan_hash, review.review_revision, "")]
    refresh(model, api, clock)
    assert model.card("job-a").state == "approved"
    assert model.card("job-a").review is None
    assert not model.can_approve("job-a")


def test_serial_attestation_is_exact_and_deadline_rechecked_in_worker() -> None:
    model, api, clock = loaded(card(backend="serial"))
    review = display_review(model)
    phrase = ATTESTATIONS[review.attestation]
    assert not model.can_approve("job-a")
    assert not model.can_approve("job-a", phrase + " ")
    operation = model.approve("job-a", review_key(review), phrase)
    assert operation is not None
    clock.value += 30
    outcome = perform_timeline(api, operation, clock)
    assert outcome.error == EXPIRED_REVIEW
    assert api.calls == [("timeline",)]


def test_same_review_refresh_preserves_identity_but_never_extends_deadline() -> None:
    model, api, clock = loaded()
    review = display_review(model)
    clock.value += 10
    refresh(model, api, clock)
    current = model.card("job-a").review
    assert current is not None and review_key(current) == review_key(review)
    assert current.expires_at == review.expires_at
    assert model.can_approve("job-a")
    api.data["cards"][0]["review"]["review_revision"] = "f" * 64
    refresh(model, api, clock)
    assert not model.can_approve("job-a")
    display_review(model)
    assert model.approve("job-a", review_key(review), "") is None


@pytest.mark.parametrize("committed", [False, True])
def test_uncertain_plan_has_no_replay_but_fresh_pending_allows_new_explicit_click(
    committed: bool,
) -> None:
    model, api, clock = loaded()
    review = display_review(model)
    api.fail.add("approve")
    operation = model.approve("job-a", review_key(review), "")
    assert operation is not None
    assert model.accept(perform_timeline(api, operation, clock))
    assert model.card_messages["job-a"] == APPROVAL_UNCERTAIN
    assert not model.can_approve("job-a")
    api.fail.clear()
    if committed:
        api.data["cards"][0].update(review=None, state="approved")
    refresh(model, api, clock)
    if not committed:
        display_review(model)
    assert model.can_approve("job-a") is (not committed)
    assert len([call for call in api.calls if call[0] == "approve"]) == 1


def test_autopreview_is_bounded_to_two_new_images_per_snapshot_and_cache_to_eight() -> None:
    model, api, clock = loaded(*(completed(f"job-{i}") for i in range(8)))
    for cycle in range(4):
        if cycle:
            refresh(model, api, clock)
        preview(model, api, clock)
        preview(model, api, clock)
        assert model.next_preview() is None
    assert len(model.previews) == 8
    refresh(model, api, clock)
    assert model.next_preview() is None
    assert [call[0] for call in api.calls].count("image") == 8
    assert all(call[0] in ("timeline", "image") for call in api.calls)


def test_failed_preview_requires_explicit_refresh_to_retry() -> None:
    model, api, clock = loaded(completed())
    api.fail.add("image")
    operation = model.next_preview()
    assert operation is not None
    model.accept(perform_timeline(api, operation, clock))
    assert model.card_messages["job-a"] == PREVIEW_FAILED
    api.fail.clear()
    refresh(model, api, clock)
    assert model.next_preview() is None
    refresh(model, api, clock, retry=True)
    assert preview(model, api, clock).png == mock_image().png


@pytest.mark.parametrize("decision", ["approve", "reject"])
def test_image_requires_visible_exact_preview_and_one_explicit_bound_decision(
    decision: Any,
) -> None:
    model, api, clock = loaded(completed())
    operation = model.next_preview()
    assert operation is not None
    model.accept(perform_timeline(api, operation, clock))
    retained = model.previews["job-a"]
    assert not model.can_decide("job-a")
    assert model.decide("job-a", retained, decision) is None
    assert model.mark_displayed("job-a", retained)
    assert model.decide("job-a", replace(retained, delivery_revision="f" * 64), decision) is None
    operation = model.decide("job-a", retained, decision)
    assert operation is not None
    assert model.decide("job-a", retained, decision) is None
    assert not model.can_close() and not model.stop()
    assert model.message == PENDING_CLOSE
    assert model.accept(perform_timeline(api, operation, clock))
    assert api.calls[-1] == (
        "decide_image",
        "job-a",
        retained.row.sha256,
        retained.delivery_revision,
        decision,
    )
    assert model.can_close()
    refresh(model, api, clock)
    assert not model.can_decide("job-a")


@pytest.mark.parametrize("committed", [False, True])
def test_uncertain_image_recovers_only_after_new_valid_snapshot(committed: bool) -> None:
    model, api, clock = loaded(completed())
    retained = preview(model, api, clock)
    api.fail.add("decide_image")
    operation = model.decide("job-a", retained, "approve")
    assert operation is not None
    model.accept(perform_timeline(api, operation, clock))
    assert model.card_messages["job-a"] == DECISION_FAILED
    assert not model.can_decide("job-a")
    api.fail.clear()
    if committed:
        api.data["cards"][0]["image"]["delivery_state"] = "approved"
        api.images["job-a"]["delivery_state"] = "approved"
    refresh(model, api, clock)
    if not committed:
        assert model.mark_displayed("job-a", retained)
    assert model.can_decide("job-a") is (not committed)
    assert len([call for call in api.calls if call[0] == "decide_image"]) == 1


def test_snapshot_failure_preserves_bounded_pixels_but_disables_all_authority_and_recovers() -> (
    None
):
    model, api, clock = loaded(completed())
    retained = preview(model, api, clock)
    api.fail.add("timeline")
    refresh(model, api, clock)
    assert model.message == STALE_TIMELINE and not model.fresh
    assert model.previews["job-a"] is retained and not model.can_decide("job-a")
    assert "secret" not in model.message
    api.fail.clear()
    refresh(model, api, clock)
    assert model.mark_displayed("job-a", retained) and model.can_decide("job-a")


def test_initial_failure_and_initial_empty_history_are_distinct() -> None:
    model, api, clock = TimelineController(Clock()), FakeAPI(), Clock()
    api.fail.add("timeline")
    refresh(model, api, clock)
    assert model.message == INITIAL_FAILED and model.snapshot is None
    api.fail.clear()
    api.data["cards"] = []
    refresh(model, api, clock)
    assert "No jobs" in model.message and "5 minutes" in model.message


def test_expiry_clears_pixels_and_blocks_pending_preview_and_decision_dispatch() -> None:
    model, api, clock = loaded(completed())
    retained = preview(model, api, clock)
    operation = model.decide("job-a", retained, "approve")
    assert operation is not None
    clock.value = retained.row.expires_at
    model.expire()
    assert not model.previews and not model.displayed
    model.accept(perform_timeline(api, operation, clock))
    assert not any(call[0] == "decide_image" for call in api.calls)
    assert model.next_preview() is None


@pytest.mark.parametrize(
    "field,value",
    [
        ("sha256", "b" * 64),
        ("recipient_principal", "key:other"),
        ("born_utc", "2026-10-03T13:31:00Z"),
        ("policy_allowed", True),
    ],
)
def test_refreshed_image_binding_changes_clear_old_bytes(field: str, value: Any) -> None:
    model, api, clock = loaded(completed())
    retained = preview(model, api, clock)
    api.data["cards"][0]["image"][field] = value
    refresh(model, api, clock)
    assert not model.previews and not model.can_decide("job-a")
    assert not model.mark_displayed("job-a", retained)


def test_epoch_change_and_late_outcomes_cannot_restore_old_preview_or_review() -> None:
    model, api, clock = loaded(completed())
    retained = preview(model, api, clock)
    operation = model.refresh()
    assert operation is not None
    old = perform_timeline(api, operation, clock)
    api.data["broker_epoch"] = "a" * 32
    model.accept(perform_timeline(api, operation, clock))
    assert not model.previews and not model.displayed
    assert not model.accept(old)
    assert not model.mark_displayed("job-a", retained)
    assert "restarted" in model.message


def test_stop_during_read_discards_late_pixels_but_never_abandons_submitted_decision() -> None:
    model, api, clock = loaded(completed())
    operation = model.next_preview()
    assert operation is not None
    outcome = perform_timeline(api, operation, clock)
    assert model.stop()
    assert not model.accept(outcome)
    assert model.snapshot is None and not model.previews and model.refresh() is None


def test_worker_performs_http_and_png_validation_off_tk_thread(monkeypatch: Any) -> None:
    api = FakeAPI(completed())
    threads: list[int] = []
    original = ImagePreview.parse

    def parse(*args: Any) -> ImagePreview:
        threads.append(threading.get_ident())
        return original(*args)

    monkeypatch.setattr(ImagePreview, "parse", parse)
    worker = TimelineWorker(api)
    try:
        assert not api.calls
        start = time.monotonic()
        worker.commands.put(TimelineOperation(1, 0, "refresh", start))
        outcome = worker.results.get(timeout=2)
        assert outcome.snapshot is not None
        row = outcome.snapshot.cards[0].image
        worker.commands.put(TimelineOperation(2, 0, "preview", start, "e" * 32, "job-a", row=row))
        outcome = worker.results.get(timeout=2)
        assert outcome.preview is not None and outcome.preview.png == mock_image().png
        assert api.threads == [worker.thread.ident, worker.thread.ident]
        assert threads == [worker.thread.ident]
        assert worker.thread.ident != threading.get_ident()
    finally:
        worker.stop()
        worker.thread.join(timeout=2)
    assert not worker.thread.is_alive() and worker.results.empty()


def test_stopping_worker_during_preview_drops_result_and_bytes() -> None:
    entered, release = threading.Event(), threading.Event()

    class BlockingAPI(FakeAPI):
        def image(self, job: str) -> dict[str, Any]:
            entered.set()
            assert release.wait(2)
            return super().image(job)

    api = BlockingAPI(completed())
    worker = TimelineWorker(api)
    started = time.monotonic()
    row = TimelineSnapshot.parse(api.data, started).cards[0].image
    worker.commands.put(TimelineOperation(1, 0, "preview", started, "e" * 32, "job-a", row=row))
    try:
        assert entered.wait(2)
        worker.stop()
        release.set()
        worker.thread.join(timeout=2)
        assert not worker.thread.is_alive() and worker.results.empty()
    finally:
        release.set()
        worker.stop()


class Variable:
    def __init__(self, value: str = "") -> None:
        self.value, self.callbacks = value, []

    def get(self) -> str:
        return self.value

    def set(self, value: str) -> None:
        self.value = value
        for callback in self.callbacks:
            callback()

    def trace_add(self, _event: str, callback: Any) -> None:
        self.callbacks.append(callback)


class Widget:
    def __init__(self, master: Any = None, **config: Any) -> None:
        self.master, self.config = master, config
        self.bindings: dict[str, Any] = {}
        self.content, self.writes, self.row, self.hidden = "", 0, 0, False
        self.destroyed, self.focused = False, False
        self.height: int | None = None
        self.kind, self.top, self.moves, self.callbacks = "Frame", 0.0, [], []

    def configure(self, **config: Any) -> None:
        self.config.update(config)

    def pack(self, **_config: Any) -> None:
        self.hidden = False

    def grid(self, **config: Any) -> None:
        self.row = config.get("row", self.row)
        self.config.update(config)
        self.hidden = False

    def grid_remove(self) -> None:
        self.hidden = True

    pack_forget = grid_remove

    def rowconfigure(self, *_args: Any, **_kwargs: Any) -> None:
        pass

    columnconfigure = rowconfigure
    itemconfigure = rowconfigure

    def bind(self, event: str, callback: Any, **_kwargs: Any) -> str:
        self.bindings[event] = callback
        return event

    def unbind(self, event: str, _binding: str) -> None:
        self.bindings.pop(event)

    def delete(self, *_args: Any) -> None:
        self.content, self.writes = "", self.writes + 1

    def insert(self, _position: str, text: str) -> None:
        self.content += text

    def destroy(self) -> None:
        self.destroyed = True

    def focus_set(self) -> None:
        self.focused = True

    def winfo_y(self) -> int:
        return self.row * 400

    def winfo_height(self) -> int:
        if self.height is not None:
            return self.height
        return 400 if self.config.get("highlightthickness") else 4000

    def winfo_class(self) -> str:
        return self.kind

    def create_window(self, *_args: Any, **_kwargs: Any) -> int:
        return 1

    def bbox(self, _what: str) -> tuple[int, int, int, int]:
        return 0, 0, 800, 4000

    def canvasy(self, _position: int) -> float:
        return self.top

    def yview(self, *_args: Any) -> None:
        pass

    def yview_scroll(self, *args: Any) -> None:
        self.moves.append(args)

    def yview_moveto(self, value: float) -> None:
        self.top = value * 4000
        self.moves.append(value)

    def set(self, *_args: Any) -> None:
        pass

    def after(self, delay: int, callback: Any) -> None:
        self.callbacks.append((delay, callback))

    def update_idletasks(self) -> None:
        pass


class FakeWorker:
    def __init__(self, _api: Any) -> None:
        self.commands, self.results = queue.Queue(), queue.Queue()
        self.stopped = threading.Event()

    def stop(self) -> None:
        self.stopped.set()


def panel_fixture(
    monkeypatch: Any, *cards: dict[str, Any]
) -> tuple[TimelinePanel, FakeAPI, Clock, list[Any]]:
    clock, api, photos = Clock(), FakeAPI(*cards), []
    model = TimelineController(clock)

    class Photo:
        def __init__(self, **kwargs: Any) -> None:
            assert threading.get_ident() == threading.main_thread().ident
            self.data = kwargs["data"]
            self.subsamples: list[int] = []
            self.small = object()
            photos.append(kwargs | {"photo": self})

        def subsample(self, factor: int) -> object:
            self.subsamples.append(factor)
            return self.small

    fake_ttk = SimpleNamespace(
        **{
            name: Widget
            for name in (
                "Frame",
                "LabelFrame",
                "Label",
                "Entry",
                "Button",
                "Scrollbar",
            )
        }
    )
    monkeypatch.setitem(
        sys.modules,
        "tkinter",
        SimpleNamespace(
            ttk=fake_ttk,
            StringVar=Variable,
            PhotoImage=Photo,
            Frame=Widget,
            Label=Widget,
            Checkbutton=Widget,
            TclError=ValueError,
            Text=Widget,
            Canvas=Widget,
        ),
    )
    monkeypatch.setitem(sys.modules, "tkinter.ttk", fake_ttk)
    monkeypatch.setattr(gui_timeline, "TimelineWorker", FakeWorker)
    monkeypatch.setattr(gui_timeline, "TimelineController", lambda: model)
    panel = TimelinePanel(Widget(), Widget(), api)
    operation = panel.worker.commands.get_nowait()
    model.accept(perform_timeline(api, operation, clock))
    panel._render()
    return panel, api, clock, photos


def test_inline_cards_keep_attestation_expansion_selection_and_widget_identity(
    monkeypatch: Any,
) -> None:
    panel, api, clock, _ = panel_fixture(monkeypatch, card(backend="serial"))
    view = panel.views["job-a"]
    view.attestation_var.set(ATTESTATIONS["device_home_firmware"])
    view._toggle_details()
    writes = view.json_text.writes
    assert "Esc" in view.summary_var.get() and "unsaved" in view.summary_var.get()
    assert view.approve_button.config["state"] == "normal"
    refresh(panel.controller, api, clock)
    panel._render()
    assert panel.views["job-a"] is view and view.expanded and view.details_expanded
    assert view.attestation_var.get() == ATTESTATIONS["device_home_firmware"]
    assert view.json_text.writes == writes
    api.data["cards"][0]["review"]["review_revision"] = "f" * 64
    refresh(panel.controller, api, clock)
    panel._render()
    assert view.attestation_var.get() == "" and view.approve_button.config["state"] == "disabled"


def test_primary_plan_approval_is_one_action_after_visible_effects(monkeypatch: Any) -> None:
    panel, _api, _clock, _ = panel_fixture(monkeypatch, card())
    view = panel.views["job-a"]
    assert view.summary_var.get() and view.expanded and not view.details_expanded
    assert view.approve_button.config["text"] == "Approve plan"
    view._arm_plan()
    view._approve()
    assert panel.controller.pending.action == "approve_plan"
    view._approve()
    assert panel.worker.commands.qsize() == 1


def test_press_refresh_release_never_substitutes_new_plan_target(monkeypatch: Any) -> None:
    panel, api, clock, _ = panel_fixture(monkeypatch, card())
    view = panel.views["job-a"]
    view._arm_plan()
    api.data["cards"][0]["review"]["review_revision"] = "f" * 64
    refresh(panel.controller, api, clock)
    panel._render()
    view._approve()
    assert panel.controller.pending is None and panel.worker.commands.empty()


def test_historical_json_is_cleared_if_same_job_gets_a_different_plan(monkeypatch: Any) -> None:
    panel, api, clock, _ = panel_fixture(monkeypatch, card())
    view = panel.views["job-a"]
    view._toggle_details()
    api.data["cards"][0].update(review=None, plan_hash="a" * 64, state="ready")
    refresh(panel.controller, api, clock)
    panel._render()
    assert view.last_review is None and not view.expanded and view.json_text.hidden
    assert view.json_text.content == ""


def test_png_is_rendered_automatically_in_same_card_and_cleared_at_ttl(monkeypatch: Any) -> None:
    panel, api, clock, photos = panel_fixture(monkeypatch, completed())
    preview(panel.controller, api, clock)
    panel._render()
    view = panel.views["job-a"]
    assert len(photos) == 1 and base64.b64decode(photos[0]["data"]) == mock_image().png
    assert "TEST / NOT DEVICE" in view.image_var.get()
    assert view.send_button.config["state"] == "normal"
    assert "client" not in view.image_var.get() or "sent" not in view.image_var.get()
    view._arm_image("approve")
    old_preview = view.rendered
    api.data["cards"][0]["image"]["recipient_principal"] = "key:other"
    refresh(panel.controller, api, clock)
    panel._render()
    view._decide("approve")
    assert panel.controller.pending is None and panel.worker.commands.empty()
    assert old_preview is not None
    clock.value = old_preview.row.expires_at
    panel._render()
    assert view.photo is None and view.rendered is None
    assert view.image_var.get() == EXPIRED_IMAGE


def test_succeeded_capture_without_pixels_truthfully_explains_expiry(monkeypatch: Any) -> None:
    panel, _api, _clock, photos = panel_fixture(monkeypatch, completed() | {"image": None})
    view = panel.views["job-a"]
    assert view.image_var.get() == EXPIRED_IMAGE
    assert "Capture completed" in view.state_var.get()
    assert not photos and view.image_actions.hidden


@pytest.mark.parametrize(
    "state",
    ["queued", "running", "succeeded", "failed", "cancelled", "needs_review", "expired", "unknown"],
)
def test_approval_waiting_copy_is_removed_when_job_moves_on(monkeypatch: Any, state: str) -> None:
    panel, api, clock, _ = panel_fixture(monkeypatch, card(backend="serial"))
    view = panel.views["job-a"]
    view.attestation_var.set(ATTESTATIONS["device_home_firmware"])
    view._arm_plan()
    view._approve()
    operation = panel.worker.commands.get_nowait()
    assert panel.controller.accept(perform_timeline(api, operation, clock))
    refresh(panel.controller, api, clock)
    panel._render()
    assert APPROVAL_NOTICE in view.message_var.get()
    assert "Waiting for the requesting client" in view.next_var.get()
    view._toggle_card()
    view._toggle_details()

    api.data["cards"][0].update(state=state)
    refresh(panel.controller, api, clock)
    panel._render()
    assert APPROVAL_NOTICE not in view.message_var.get()
    assert APPROVAL_NOTICE not in view.detail_var.get()
    assert view.message_label.hidden
    assert panel.views["job-a"] is view and view.expanded and view.details_expanded
    assert not panel.controller.can_approve("job-a")
    assert not panel.controller.can_decide("job-a")
    assert panel.worker.commands.empty()
    assert [call[0] for call in api.calls] == ["timeline", "approve", "timeline", "timeline"]


@pytest.mark.parametrize("state", ["failed", "needs_review"])
def test_failed_capture_reports_no_image_instead_of_retention_expiry(
    monkeypatch: Any, state: str
) -> None:
    row = card(backend="serial") | {
        "state": state,
        "review": None,
        "stage": "serial_close",
        "result": "serial_marker_timeout",
    }
    panel, api, _clock, photos = panel_fixture(monkeypatch, row)
    view = panel.views["job-a"]
    assert "No usable image was acquired" in view.image_var.get()
    assert "did not complete successfully" in view.image_var.get()
    assert "expired" not in view.image_var.get() and "5 minutes" not in view.image_var.get()
    assert "serial_marker_timeout" in view.detail_var.get()
    assert not photos and view.image_actions.hidden and view.plan_actions.hidden
    assert panel.worker.commands.empty() and api.calls == [("timeline",)]


@pytest.mark.parametrize("reviewed", [False, True])
def test_historical_failure_badge_and_current_review_prompt_survive_reopen_and_restart(
    monkeypatch: Any, reviewed: bool
) -> None:
    row = card(backend="serial") | {
        "state": "needs_review",
        "reviewed": reviewed,
        "review": None,
        "stage": "serial_close",
        "result": "serial_capture_timeout",
    }
    panel, api, clock, _ = panel_fixture(monkeypatch, row)
    for restarted in (False, True):
        if restarted:
            api.data["broker_epoch"] = "a" * 32
            refresh(panel.controller, api, clock)
            panel._render()
        view = panel.views["job-a"]
        assert view.state_var.get() == (
            "Failed · reviewed · SERIAL" if reviewed else "Review required · SERIAL"
        )
        assert view.expanded is not reviewed
        assert view.state_label.config["foreground"] == ("#991b1b" if reviewed else "#92400e")
        assert view.next_var.get() == (
            "Failure reviewed · Open for details"
            if reviewed
            else "Review device state before continuing"
        )
        assert view.summary_label.hidden is reviewed
        assert ("Inspect the device before continuing" in view.summary_var.get()) is not reviewed
        assert "No usable image was acquired" in view.image_var.get()
        assert "serial_capture_timeout" in view.detail_var.get()
        assert ("Device-state review: completed" in view.detail_var.get()) is reviewed
        assert not panel.controller.can_approve("job-a")
        assert not panel.controller.can_decide("job-a")
        assert view.plan_actions.hidden and view.image_actions.hidden
        assert panel.worker.commands.empty()
    assert api.calls == [("timeline",), ("timeline",)]
    panel.stop()
    reopened, reopened_api, _, _ = panel_fixture(monkeypatch, row)
    assert reopened.views["job-a"].expanded is not reviewed
    assert reopened.views["job-a"].state_var.get() == view.state_var.get()
    assert reopened_api.calls == [("timeline",)]


@pytest.mark.parametrize("manual_open", [False, True])
def test_reviewed_failure_refresh_preserves_manual_accordion_state(
    monkeypatch: Any, manual_open: bool
) -> None:
    row = card(backend="serial") | {"state": "needs_review", "review": None}
    panel, api, clock, _ = panel_fixture(monkeypatch, row)
    view = panel.views["job-a"]
    view._toggle_details()
    view._toggle_card()
    if manual_open:
        view._toggle_card()
    api.data["cards"][0]["reviewed"] = True
    refresh(panel.controller, api, clock)
    panel._render()
    assert panel.views["job-a"] is view and view.expanded is manual_open
    assert view.open_override is manual_open and view.details_expanded
    assert view.state_var.get() == "Failed · reviewed · SERIAL"
    assert view.summary_label.hidden and "before continuing" not in view.next_var.get()
    assert "before continuing" not in view.image_var.get()
    assert panel.worker.commands.empty()


def test_reviewed_history_does_not_hide_current_review_or_relabel_unknown(monkeypatch: Any) -> None:
    history = card("history") | {"state": "needs_review", "review": None, "reviewed": True}
    current = card("current") | {"state": "needs_review", "review": None}
    unknown = card("unknown") | {"state": "unknown", "review": None, "reviewed": True}
    panel, api, _, _ = panel_fixture(monkeypatch, history, current, unknown)
    assert panel.views["history"].state_var.get() == "Failed · reviewed · MOCK"
    assert not panel.views["history"].expanded
    assert panel.views["current"].state_var.get() == "Review required · MOCK"
    assert panel.views["current"].expanded
    assert panel.views["current"].next_var.get() == "Review device state before continuing"
    assert panel.views["unknown"].state_var.get() == "State unavailable · MOCK"
    assert "Capture outcome is unknown" in panel.views["unknown"].image_var.get()
    assert "Inspect the device" in panel.views["unknown"].image_var.get()
    assert api.calls == [("timeline",)] and panel.worker.commands.empty()


@pytest.mark.parametrize(
    "result",
    [None, "broker_restart_no_replay", "serial_operation_uncertain", "serial_close_unconfirmed"],
)
def test_reviewed_uncertain_capture_stays_unknown_without_current_review_cta(
    monkeypatch: Any, result: str | None
) -> None:
    row = card(backend="serial") | {
        "state": "needs_review",
        "reviewed": True,
        "review": None,
        "result": result,
    }
    panel, api, _, photos = panel_fixture(monkeypatch, row)
    view = panel.views["job-a"]
    assert "Capture outcome is unknown; no image is available" in view.image_var.get()
    assert "job was reviewed" in view.image_var.get()
    assert "Inspect" not in view.image_var.get() and "before continuing" not in view.image_var.get()
    assert "acquired" not in view.image_var.get() and "expired" not in view.image_var.get()
    assert not photos and view.image_actions.hidden
    assert api.calls == [("timeline",)] and panel.worker.commands.empty()


@pytest.mark.parametrize(
    "state,result",
    [
        ("unknown", None),
        ("failed", None),
        ("needs_review", None),
        ("needs_review", "broker_restart_no_replay"),
        ("needs_review", "serial_operation_uncertain"),
        ("needs_review", "operation_interrupted_review_required"),
        ("needs_review", "serial_close_unconfirmed"),
        ("needs_review", "backend_error_no_retry"),
        ("needs_review", "device_disconnected_no_retry"),
    ],
)
def test_uncertain_capture_does_not_claim_acquisition_or_expiry(
    monkeypatch: Any, state: str, result: str | None
) -> None:
    row = card(backend="serial") | {"state": state, "review": None, "result": result}
    panel, api, _clock, photos = panel_fixture(monkeypatch, row)
    view = panel.views["job-a"]
    assert "Capture outcome is unknown; no image is available" in view.image_var.get()
    assert "acquired" not in view.image_var.get() and "expired" not in view.image_var.get()
    assert not photos and view.image_actions.hidden
    assert panel.worker.commands.empty() and api.calls == [("timeline",)]


@pytest.mark.parametrize("state", ["cancelled", "expired"])
def test_unexecuted_capture_does_not_claim_a_retained_image(monkeypatch: Any, state: str) -> None:
    row = card() | {"state": state, "review": None}
    panel, _api, _clock, photos = panel_fixture(monkeypatch, row)
    view = panel.views["job-a"]
    assert "no image was acquired" in view.image_var.get()
    assert "retained" not in view.image_var.get() and "5 minutes" not in view.image_var.get()
    assert not photos and view.image_actions.hidden


def test_scrolling_is_scoped_to_timeline_children_and_preserves_visible_anchor(
    monkeypatch: Any,
) -> None:
    panel, api, clock, _ = panel_fixture(monkeypatch, card("first"), card("second"))
    view = panel.views["second"]
    panel.canvas.top = 425
    assert panel._wheel(SimpleNamespace(widget=view.summary_label, delta=-120)) == "break"
    assert panel.canvas.moves[-1] == (3, "units")
    assert panel._wheel(SimpleNamespace(widget=Widget(), delta=-120)) is None
    view.json_text.kind = "Text"
    assert panel._wheel(SimpleNamespace(widget=view.json_text, delta=-120)) is None
    api.data["cards"].insert(0, card("new"))
    refresh(panel.controller, api, clock)
    panel._render()
    assert panel.views["second"] is view and panel.canvas.top == 825
    panel.stop()
    assert panel.root.bindings == {}


def test_panel_timers_only_queue_reads_and_never_approve(monkeypatch: Any) -> None:
    panel, _api, clock, _ = panel_fixture(monkeypatch, card())
    clock.value += 2
    panel._tick()
    operation = panel.worker.commands.get_nowait()
    assert operation.action == "refresh"
    assert not panel.controller.plan_attempts and not panel.controller.decision_attempts
    assert all(delay in (75, 250) for delay, _ in panel.root.callbacks)


def test_timeline_accepts_maximum_cards_and_rejects_excess_retained_images() -> None:
    parsed = TimelineSnapshot.parse(snapshot(*(card(f"job-{i}") for i in range(MAX_CARDS))), 1000)
    assert len(parsed.cards) == MAX_CARDS
    with pytest.raises(ValueError, match=INVALID_TIMELINE):
        TimelineSnapshot.parse(snapshot(*(completed(f"job-{i}") for i in range(9))), 1000)


def test_changed_generation_outcome_is_discarded_and_cannot_restore_authority() -> None:
    model, api, clock = loaded()
    operation = model.refresh()
    assert operation is not None
    outcome = perform_timeline(api, operation, clock)
    model.generation += 1
    assert model.accept(outcome) and not model.fresh
    assert not model.can_approve("job-a")
    assert not model.accept(TimelineOutcome(replace(operation, sequence=999), approved=True))


def visible(widget: Widget) -> bool:
    return not widget.hidden and (widget.master is None or visible(widget.master))


def test_completed_card_is_compact_and_technical_identifiers_are_nested(monkeypatch: Any) -> None:
    data = image(delivery_state="approved")
    panel, api, clock, photos = panel_fixture(monkeypatch, completed(data=data))
    api.images["job-a"] = data
    view = panel.views["job-a"]
    assert not view.expanded and not visible(view.body)
    assert view.frame.config["highlightthickness"] == 2
    assert view.frame.config["background"] != panel.canvas.config["background"]
    assert "TEST / NOT DEVICE" in view.next_var.get()
    assert "Image delivery approved" in view.next_var.get()
    local_time = datetime.fromisoformat(data["born_utc"]).astimezone().strftime("%H:%M")
    assert "MOCK" in view.state_var.get() and local_time in view.time_var.get()
    header = " ".join((view.state_var.get(), view.next_var.get(), view.time_var.get()))
    for private in (data["sha256"], data["recipient_principal"], "mock_capture", "host UTC"):
        assert private not in header
        assert private in view.detail_var.get()
    preview(panel.controller, api, clock)
    panel._render()
    assert not photos and not panel.controller.can_decide("job-a")
    view._toggle_card()
    assert view.expanded and len(photos) == 1 and not view.details_expanded
    assert not visible(view.detail_label)
    assert data["recipient_principal"] not in view.image_var.get()
    assert not visible(view.image_actions)
    view._toggle_details()
    assert visible(view.detail_label)
    assert not panel.worker.commands.qsize()


def test_collapsing_plan_revokes_capability_and_discards_pressed_target(monkeypatch: Any) -> None:
    panel, api, clock, _ = panel_fixture(monkeypatch, card(backend="serial"))
    view = panel.views["job-a"]
    phrase = ATTESTATIONS["device_home_firmware"]
    view.attestation_var.set(phrase)
    assert 'Port: "COM7"' in view.summary_var.get()
    for effect in ("Esc", "unsaved", "reset", "firmware", "separate permission"):
        assert effect in view.summary_var.get()
    assert visible(view.summary_label) and visible(view.phrase)
    view._arm_plan()
    view._toggle_card()
    assert not panel.controller.displayed_reviews and view.plan_activation is None
    assert view.approve_button.config["state"] == "disabled"
    assert not panel.controller.can_approve("job-a", phrase)
    view._arm_plan()
    view._approve()
    refresh(panel.controller, api, clock)
    panel._render()
    assert not view.expanded and view.attestation_var.get() == phrase
    view._toggle_card()
    assert panel.controller.can_approve("job-a", phrase)
    view._approve()
    assert panel.worker.commands.empty() and panel.controller.pending is None
    view._arm_plan()
    view._approve()
    assert panel.worker.commands.get_nowait().action == "approve_plan"
    assert not panel.controller.can_approve("job-a", phrase)


def test_explicit_open_and_close_survive_status_changes_and_polling(monkeypatch: Any) -> None:
    panel, api, clock, _ = panel_fixture(monkeypatch, card())
    view = panel.views["job-a"]
    assert view.expanded and view.open_override is None
    api.data["cards"][0].update(review=None, state="approved")
    refresh(panel.controller, api, clock)
    panel._render()
    assert not view.expanded
    view._toggle_card()
    view._toggle_details()
    assert view.open_override is True
    api.data["cards"][0] = completed()
    refresh(panel.controller, api, clock)
    panel._render()
    assert view.expanded and view.details_expanded
    view._toggle_card()
    refresh(panel.controller, api, clock)
    panel._render()
    assert not view.expanded and view.open_override is False and view.details_expanded
    assert view is panel.views["job-a"]


def test_hidden_image_cannot_confer_authority_or_replay_an_armed_click(monkeypatch: Any) -> None:
    panel, api, clock, _ = panel_fixture(monkeypatch, completed())
    view = panel.views["job-a"]
    assert view.expanded  # Image delivery still needs review although capture finished.
    preview(panel.controller, api, clock)
    panel._render()
    assert visible(view.image_label) and visible(view.image_details)
    assert "AI recipient: key:" in view.image_var.get()
    assert "Reject is final" in view.image_var.get()
    assert panel.controller.can_decide("job-a")
    view._arm_image("approve")
    view._toggle_card()
    assert not panel.controller.displayed and not view.image_activation
    assert not panel.controller.can_decide("job-a")
    assert view.send_button.config["state"] == "disabled"
    refresh(panel.controller, api, clock)
    panel._render()
    assert not view.expanded and not panel.controller.can_decide("job-a")
    view._arm_image("approve")
    view._decide("approve")
    view._toggle_card()
    view._decide("approve")
    assert panel.controller.pending is None and panel.worker.commands.empty()
    view._arm_image("approve")
    view._decide("approve")
    assert panel.worker.commands.get_nowait().action == "approve_image"


def test_thumbnail_and_full_size_reuse_exact_validated_preview_without_requests(
    monkeypatch: Any,
) -> None:
    panel, api, clock, photos = panel_fixture(monkeypatch, completed())
    retained = preview(panel.controller, api, clock)
    panel._render()
    view = panel.views["job-a"]
    full = photos[0]["photo"]
    assert full.subsamples == [2]
    assert base64.b64decode(full.data) == retained.png == mock_image().png
    assert view.image_label.config["image"] is full.small
    calls = list(api.calls)
    view._toggle_image()
    assert view.large_preview and view.image_label.config["image"] is full
    view._toggle_card()
    view._toggle_card()
    panel._render()
    assert view.large_preview and view.image_label.config["image"] is full
    view._toggle_image()
    assert view.image_label.config["image"] is full.small
    assert len(photos) == 1 and full.subsamples == [2]
    assert api.calls == calls and panel.worker.commands.empty()
    clock.value = retained.row.expires_at
    panel._render()
    assert view.photo is view.thumbnail is view.rendered is None
    assert not panel.controller.can_decide("job-a")
    assert "Preview expired" in view.next_var.get()
    assert not visible(view.image_size_button)


@pytest.mark.parametrize("state", ["approved", "rejected"])
def test_image_final_state_survives_expiry_in_collapsed_summary(
    monkeypatch: Any, state: str
) -> None:
    data = image(delivery_state=state)
    panel, _api, clock, _ = panel_fixture(monkeypatch, completed(data=data))
    view = panel.views["job-a"]
    assert not view.expanded
    assert f"Image delivery {state}" in view.next_var.get()
    assert "received" not in view.next_var.get() and "sent" not in view.next_var.get()
    clock.value += 201
    panel._render()
    assert f"Image delivery {state}" in view.next_var.get()
    assert "Preview expired" in view.next_var.get()
    assert view.image_var.get() == EXPIRED_IMAGE
    assert not panel.controller.can_decide("job-a")


def test_focus_pending_expands_same_card_without_submitting(monkeypatch: Any) -> None:
    panel, _api, _clock, _ = panel_fixture(monkeypatch, card())
    view = panel.views["job-a"]
    view._toggle_card()
    panel.focus_pending()
    assert panel.views["job-a"] is view and view.expanded
    assert view.open_override is True and view.approve_button.focused
    assert panel.worker.commands.empty()


def test_hidden_expired_review_cannot_be_enabled_by_reopening_or_typing(monkeypatch: Any) -> None:
    panel, _api, clock, _ = panel_fixture(monkeypatch, card(backend="serial"))
    view = panel.views["job-a"]
    view._toggle_card()
    clock.value += 31
    panel._render()
    assert "Plan review expired" in view.next_var.get()
    view._toggle_card()
    view.attestation_var.set(ATTESTATIONS["device_home_firmware"])
    assert view.approve_button.config["state"] == "disabled"
    view._arm_plan()
    view._approve()
    assert panel.controller.pending is None and panel.worker.commands.empty()


def test_passive_serial_approval_keeps_reset_risk_and_target_visible(monkeypatch: Any) -> None:
    panel, _api, _clock, _ = panel_fixture(
        monkeypatch, card(backend="serial", test="passive_observe")
    )
    view = panel.views["job-a"]
    assert "without sending commands" in view.summary_var.get()
    assert "opening/closing serial may reset" in view.summary_var.get()
    assert 'Port: "COM7"' in view.summary_var.get()
    assert view.approve_button.config["state"] == "normal"
    assert not view.details_expanded and not visible(view.detail_label)


def test_collapsing_a_tall_anchored_card_returns_to_its_header(monkeypatch: Any) -> None:
    panel, _api, _clock, _ = panel_fixture(monkeypatch, card("first"), card("second"))
    view = panel.views["second"]
    view.frame.height = 800
    panel.canvas.top = 950  # 550 px into the second card.
    original = view.render

    def shrink(card: Any) -> None:
        original(card)
        view.frame.height = 110 if not view.expanded else 800

    monkeypatch.setattr(view, "render", shrink)
    view._toggle_card()
    assert not view.expanded and panel.canvas.top == 400


def test_policy_allowed_completed_image_defaults_closed_and_is_not_pending_focus(
    monkeypatch: Any,
) -> None:
    row = completed(data=image(policy_allowed=True))
    panel, _api, _clock, _ = panel_fixture(monkeypatch, row, card("manual"))
    view = panel.views["job-a"]
    assert not view.expanded
    assert "Existing policy permits image delivery" in view.next_var.get()
    panel.focus_pending()
    assert panel.views["manual"].approve_button.focused
    assert not view.expanded
    view._toggle_card()
    assert "Existing key policy already permits sending" in view.image_var.get()
    assert not panel.controller.can_decide("job-a")  # No validated preview yet.


def test_late_cards_and_preview_toggle_follow_current_canvas_width(monkeypatch: Any) -> None:
    panel, api, clock, _ = panel_fixture(monkeypatch, card())
    panel._canvas_size(SimpleNamespace(width=478))
    api.data["cards"].insert(0, completed("late"))
    api.images["late"] = image("late")
    refresh(panel.controller, api, clock)
    panel._render()
    view = panel.views["late"]
    assert view.summary_label.config["wraplength"] == 440
    assert view.image_details.config["wraplength"] == 306
    assert view.image_details.config["column"] == 1
    preview(panel.controller, api, clock)
    panel._render()
    view._toggle_image()
    assert view.large_preview
    assert view.image_details.config["column"] == 0 and view.image_details.row == 2
    assert view.image_details.config["wraplength"] == 440
    panel._canvas_size(SimpleNamespace(width=678))
    assert view.image_details.config["column"] == 1
    assert view.image_details.config["wraplength"] == 386
    view._toggle_image()
    assert view.image_details.config["wraplength"] == 506


def test_header_job_identity_is_bounded_but_exact_approval_target_is_visible(
    monkeypatch: Any,
) -> None:
    job = "synthetic-job-" + "a" * 40 + "-ending"
    panel, _api, _clock, _ = panel_fixture(monkeypatch, card(job))
    view = panel.views[job]
    title = view.title_label.config["text"]
    assert job not in title and job[:12] in title and job[-12:] in title
    assert job in view.summary_var.get()
    assert visible(view.summary_label) and panel.controller.can_approve(job)


def test_worker_result_arriving_under_collapsed_card_requires_visible_review_before_click(
    monkeypatch: Any,
) -> None:
    panel, api, clock, photos = panel_fixture(monkeypatch, completed())
    view = panel.views["job-a"]
    panel._drain()
    operation = panel.worker.commands.get_nowait()
    assert operation.action == "preview"
    view._toggle_card()
    panel.worker.results.put(perform_timeline(api, operation, clock))
    panel._drain()
    assert panel.controller.previews and not photos
    assert not panel.controller.displayed and not panel.controller.can_decide("job-a")
    view._toggle_card()
    assert len(photos) == 1 and panel.controller.can_decide("job-a")
    view.send_button.bindings["<ButtonPress-1>"](None)
    view.send_button.config["command"]()
    decision = panel.worker.commands.get_nowait()
    assert decision.action == "approve_image"
    view.send_button.config["command"]()
    assert panel.worker.commands.empty()
    panel.worker.results.put(perform_timeline(api, decision, clock))
    panel._drain()
    assert not panel.controller.fresh and not panel.controller.can_decide("job-a")
    panel._tick()
    refresh_operation = panel.worker.commands.get_nowait()
    assert refresh_operation.action == "refresh"
    panel.worker.results.put(perform_timeline(api, refresh_operation, clock))
    panel._drain()
    assert "Image delivery approved" in view.next_var.get()
    assert not visible(view.image_actions)
    assert [call[0] for call in api.calls] == ["timeline", "image", "decide_image", "timeline"]
    assert panel.worker.commands.get_nowait().action == "preview"  # Read-only final preview.


def test_review_target_text_escapes_control_and_bidi_characters(monkeypatch: Any) -> None:
    row = card(backend="serial")
    row["review"]["plan"]["port"] = "COM7\nUNTRUSTED\u202e"
    digest = hashlib.sha256(
        json.dumps(row["review"]["plan"], sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    row["plan_hash"] = row["review"]["plan_hash"] = digest
    panel, _api, _clock, _ = panel_fixture(monkeypatch, row)
    view = panel.views["job-a"]
    assert 'Port: "COM7\\nUNTRUSTED\\u202e"' in view.summary_var.get()
    assert "\nUNTRUSTED" not in view.summary_var.get() and "\u202e" not in view.summary_var.get()
    assert ATTESTATIONS["device_home_firmware"] in view.phrase.config["text"]


@pytest.mark.parametrize(
    "test,backend,stage,result",
    [
        (
            "firmware_fetch_source",
            "registered_project",
            "source_publish",
            "source_snapshot_fetched_no_build_no_flash",
        ),
        ("firmware_build", "registered_project", "idf_build", "firmware_build_succeeded_no_flash"),
        ("firmware_backup", "serial", "flash_backup_read", "firmware_backup_completed"),
        ("firmware_flash", "serial", "flash_readback_verify", "firmware_flash_completed"),
        ("firmware_restore", "serial", "flash_restore_write", "firmware_restore_completed"),
    ],
)
def test_firmware_operation_names_stages_and_results_survive_timeline_parse(
    test, backend, stage, result
):
    row = card("firmware-job", backend=backend, test=test)
    row.update(state="succeeded", review=None, stage=stage, result=result)
    parsed = TimelineSnapshot.parse(snapshot(row), 1000).cards[0]
    assert parsed.stage == stage and parsed.result == result
    assert test in gui_timeline.REQUESTS
    if backend == "registered_project":
        queued = replace(parsed, state="queued")
        assert "device" not in gui_timeline._CardView._next_action(queued, 1000).lower()
