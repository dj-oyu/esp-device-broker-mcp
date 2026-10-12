"""Independent headless timeline recovery tests; no Tk, HTTP, or device access."""

from __future__ import annotations

import copy
import hashlib
import json
import queue
import sys
import threading
import time
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest
from test_gui_images import detail as image_detail
from test_gui_images import row as image_row

from cardputer_mcp.backend import MockBackend
from cardputer_mcp.capture import mock_image
from cardputer_mcp.gui_approvals import APPROVAL_UNCERTAIN, ATTESTATIONS, PlanReview
from cardputer_mcp.gui_images import DECISION_FAILED, EXPIRED, ImagePreview
from cardputer_mcp.gui_timeline import (
    EXPIRED_REVIEW,
    INITIAL_FAILED,
    INVALID_TIMELINE,
    MAX_CARDS,
    MAX_PREVIEWS,
    PENDING_CLOSE,
    PREVIEW_FAILED,
    PREVIEWS_PER_CYCLE,
    STALE_TIMELINE,
    TimelineController,
    TimelineOperation,
    TimelineOutcome,
    TimelineSnapshot,
    TimelineWorker,
    perform_timeline,
    review_key,
)
from cardputer_mcp.models import TestName as DeviceTest

EPOCH = "e" * 32
PLAN_JOB = "job_plan"
IMAGE_JOB = "job_image_1"


class Clock:
    def __init__(self) -> None:
        self.value = 1000.0

    def __call__(self) -> float:
        return self.value


def plan_detail(
    *, job_id: str = PLAN_JOB, backend: str = "mock", test: str = "diagnostics"
) -> dict[str, Any]:
    mock = MockBackend()
    plan = {
        "version": 1,
        "broker_epoch": EPOCH,
        "session_id": "2" * 32,
        "device": mock.device if backend == "mock" else "configured-cardputer",
        "backend": backend,
        "job_id": job_id,
        "test": test,
        **(mock.prepare(DeviceTest(test)) if backend == "mock" else {"stages": [test]}),
    }
    canonical = json.dumps(plan, sort_keys=True, separators=(",", ":"))
    attestation = "none"
    if backend == "serial" and test in ("hello", "capture_home"):
        attestation = "device_free_firmware" if test == "hello" else "device_home_firmware"
    return {
        "ok": True,
        "plan_hash": hashlib.sha256(canonical.encode()).hexdigest(),
        "plan": plan,
        "review_revision": "3" * 64,
        "lease_remaining_seconds": 30,
        "attestation": attestation,
    }


def plan_card(detail: dict[str, Any] | None = None) -> dict[str, Any]:
    detail = detail or plan_detail()
    return {
        "job_id": detail["plan"]["job_id"],
        "plan_hash": detail["plan_hash"],
        "test": detail["plan"]["test"],
        "backend": detail["plan"]["backend"],
        "state": "approval_required",
        "stage": None,
        "result": None,
        "created_utc": "2026-10-03T13:30:00+00:00",
        "finished_utc": None,
        "reviewed": False,
        "review": {key: value for key, value in detail.items() if key != "ok"},
        "image": None,
    }


def capture_card(detail: dict[str, Any]) -> dict[str, Any]:
    return {
        "job_id": detail["job_id"],
        "plan_hash": "c" * 64,
        "test": "capture_home",
        "backend": "mock",
        "state": "succeeded",
        "stage": "mock_capture",
        "result": "synthetic_capture_not_device",
        "created_utc": "2026-10-03T13:30:00+00:00",
        "finished_utc": "2026-10-03T13:30:01+00:00",
        "reviewed": False,
        "review": None,
        "image": image_row(detail),
    }


class FakeAPI:
    """The complete callable surface excludes execution, recapture, and HTTP."""

    def __init__(self, *, images: int = 1, review: dict[str, Any] | None = None) -> None:
        self.details = {
            f"job_image_{index + 1}": image_detail(job_id=f"job_image_{index + 1}")
            for index in range(images)
        }
        self.payload = {
            "ok": True,
            "broker_epoch": EPOCH,
            "cards": [plan_card(review), *(capture_card(data) for data in self.details.values())],
            "has_more": False,
            "plans_available": True,
        }
        self.calls: list[tuple[Any, ...]] = []
        self.threads: list[int] = []
        self.failures: dict[str, Exception] = {}
        self.approval_response: dict[str, Any] | None = None
        self.decision_response: dict[str, Any] | None = None

    def called(self, method: str, *args: Any) -> None:
        self.calls.append((method, *args))
        self.threads.append(threading.get_ident())
        if method in self.failures:
            raise self.failures[method]

    def timeline(self) -> dict[str, Any]:
        self.called("timeline")
        return copy.deepcopy(self.payload)

    def approve(self, plan_hash: str, review_revision: str, attestation: str) -> dict[str, Any]:
        self.called("approve", plan_hash, review_revision, attestation)
        return self.approval_response or {
            "ok": True,
            "approved": True,
            "plan_hash": plan_hash,
            "already_approved": False,
        }

    def image(self, job_id: str) -> dict[str, Any]:
        self.called("image", job_id)
        return copy.deepcopy(self.details[job_id])

    def decide_image(
        self, job_id: str, sha256: str, delivery_revision: str, decision: str
    ) -> dict[str, Any]:
        self.called("decide_image", job_id, sha256, delivery_revision, decision)
        return self.decision_response or {
            "ok": True,
            "job_id": job_id,
            "delivery_state": "approved" if decision == "approve" else "rejected",
        }


def refresh(controller: TimelineController, api: FakeAPI, *, manual: bool = False) -> None:
    operation = controller.refresh(retry_previews=manual)
    assert operation is not None
    assert controller.accept(perform_timeline(api, operation, controller.clock))


def loaded(
    *, images: int = 1, review: dict[str, Any] | None = None
) -> tuple[TimelineController, FakeAPI, Clock]:
    clock = Clock()
    controller, api = TimelineController(clock), FakeAPI(images=images, review=review)
    refresh(controller, api)
    assert controller.snapshot is not None and controller.fresh
    return controller, api, clock


def displayed_review(controller: TimelineController) -> PlanReview:
    card = controller.card(PLAN_JOB)
    assert card is not None and card.review is not None
    assert controller.mark_review_displayed(PLAN_JOB, card.review)
    return card.review


def load_preview(controller: TimelineController, api: FakeAPI) -> ImagePreview:
    operation = controller.next_preview()
    assert operation is not None
    assert controller.accept(perform_timeline(api, operation, controller.clock))
    preview = controller.previews[operation.job_id]
    assert controller.mark_displayed(operation.job_id, preview)
    return preview


def mutations(api: FakeAPI) -> list[tuple[Any, ...]]:
    return [call for call in api.calls if call[0] in ("approve", "decide_image")]


def test_snapshot_keeps_canonical_mock_plan_and_retained_image_in_separate_cards() -> None:
    api = FakeAPI()
    snapshot = TimelineSnapshot.parse(api.payload, 1000)
    plan, capture = snapshot.cards
    assert plan.review == PlanReview.parse(plan_detail(), 1000)
    assert plan.review is not None and plan.review.expires_at == 1030
    assert capture.image is not None and capture.image.expires_at == 1200
    assert capture.image.recipient_principal == api.details[IMAGE_JOB]["recipient_principal"]
    assert capture.image.sha256 == mock_image().sha256
    assert api.calls == []


@pytest.mark.parametrize(
    "field,value",
    [
        ("ok", 1),
        ("broker_epoch", "private"),
        ("cards", {}),
        ("has_more", 1),
        ("plans_available", 0),
    ],
)
def test_snapshot_rejects_malformed_envelope(field: str, value: Any) -> None:
    data = FakeAPI().payload
    data[field] = value
    with pytest.raises(ValueError, match=INVALID_TIMELINE):
        TimelineSnapshot.parse(data, 1000)


@pytest.mark.parametrize("case", ["duplicate", "too_many_cards", "too_many_images", "no_plans"])
def test_snapshot_enforces_unique_jobs_and_bounded_history(case: str) -> None:
    data = FakeAPI(images=MAX_PREVIEWS + (case == "too_many_images")).payload
    if case == "duplicate":
        data["cards"].append(copy.deepcopy(data["cards"][0]))
    elif case == "too_many_cards":
        data["cards"] = [plan_card(plan_detail(job_id=f"plan_{i}")) for i in range(MAX_CARDS + 1)]
    elif case == "no_plans":
        data["plans_available"] = False
    with pytest.raises(ValueError, match=INVALID_TIMELINE):
        TimelineSnapshot.parse(data, 1000)


@pytest.mark.parametrize(
    "field,value",
    [
        ("job_id", "wrong_job"),
        ("plan_hash", "f" * 64),
        ("test", "capture_home"),
        ("backend", "serial"),
        ("state", "approved"),
        ("stage", "private response"),
        ("result", "private response"),
        ("created_utc", "2026-10-03T13:30:00"),
    ],
)
def test_snapshot_rejects_plan_review_bound_to_wrong_card(field: str, value: Any) -> None:
    data = FakeAPI(images=0).payload
    data["cards"][0][field] = value
    with pytest.raises(ValueError, match=INVALID_TIMELINE):
        TimelineSnapshot.parse(data, 1000)


def test_snapshot_rejects_noncanonical_or_other_epoch_plan_review() -> None:
    for field, value in (("device", "different-device"), ("broker_epoch", "d" * 32)):
        data = FakeAPI(images=0).payload
        review = data["cards"][0]["review"]
        review["plan"][field] = value
        if field == "broker_epoch":
            canonical = json.dumps(review["plan"], sort_keys=True, separators=(",", ":"))
            review["plan_hash"] = hashlib.sha256(canonical.encode()).hexdigest()
            data["cards"][0]["plan_hash"] = review["plan_hash"]
        with pytest.raises(ValueError, match=INVALID_TIMELINE):
            TimelineSnapshot.parse(data, 1000)


@pytest.mark.parametrize("field,value", [("job_id", "wrong_job"), ("source", "render_buffer")])
def test_snapshot_rejects_retained_image_from_wrong_job_or_backend(field: str, value: str) -> None:
    data = FakeAPI().payload
    data["cards"][1]["image"][field] = value
    with pytest.raises(ValueError, match=INVALID_TIMELINE):
        TimelineSnapshot.parse(data, 1000)


def test_plan_approval_requires_exact_displayed_review_and_sends_one_approval_only() -> None:
    controller, api, clock = loaded()
    card = controller.card(PLAN_JOB)
    assert card is not None and card.review is not None
    review = card.review
    assert not controller.can_approve(PLAN_JOB)
    assert controller.approve(PLAN_JOB, review_key(review), "") is None
    assert not controller.mark_review_displayed(PLAN_JOB, replace(review))
    assert controller.mark_review_displayed(PLAN_JOB, review)
    assert (
        controller.approve(PLAN_JOB, review_key(replace(review, review_revision="f" * 64)), "")
        is None
    )
    assert controller.approve("other_job", review_key(review), "") is None
    operation = controller.approve(PLAN_JOB, review_key(review), "")
    assert operation is not None
    assert controller.approve(PLAN_JOB, review_key(review), "") is None
    assert controller.refresh() is None and controller.next_preview() is None
    assert controller.accept(perform_timeline(api, operation, clock))
    assert not controller.can_approve(PLAN_JOB)
    assert controller.approve(PLAN_JOB, review_key(review), "") is None
    assert mutations(api) == [("approve", review.plan_hash, review.review_revision, "")]


@pytest.mark.parametrize("test", ["hello", "capture_home"])
def test_serial_approval_needs_exact_attestation_for_that_review(test: str) -> None:
    controller, api, clock = loaded(review=plan_detail(backend="serial", test=test))
    review = displayed_review(controller)
    expected = ATTESTATIONS[review.attestation]
    for supplied in ("", expected.lower(), expected + " ", "DEVICE VERIFIED"):
        assert not controller.can_approve(PLAN_JOB, supplied)
        assert controller.approve(PLAN_JOB, review_key(review), supplied) is None
    operation = controller.approve(PLAN_JOB, review_key(review), expected)
    assert operation is not None
    assert controller.accept(perform_timeline(api, operation, clock))
    assert mutations(api) == [("approve", review.plan_hash, review.review_revision, expected)]


def test_plan_expiry_at_queue_and_refresh_never_extends_original_lease() -> None:
    controller, api, clock = loaded()
    original = displayed_review(controller)
    clock.value += 10
    refresh(controller, api)
    review = displayed_review(controller)
    assert review.expires_at == original.expires_at == 1030
    operation = controller.approve(PLAN_JOB, review_key(review), "")
    assert operation is not None
    clock.value = 1030
    result = perform_timeline(api, operation, clock)
    assert result.error == EXPIRED_REVIEW
    assert controller.accept(result)
    assert not mutations(api)
    refresh(controller, api)
    assert not controller.can_approve(PLAN_JOB)
    assert controller.approve(PLAN_JOB, review_key(review), "") is None


@pytest.mark.parametrize(
    "field,value", [("broker_epoch", "f" * 32), ("job_id", "wrong"), ("attestation", "wrong")]
)
def test_worker_rejects_mismatched_plan_operation_before_any_mutation(
    field: str, value: str
) -> None:
    controller, api, clock = loaded()
    review = displayed_review(controller)
    operation = controller.approve(PLAN_JOB, review_key(review), "")
    assert operation is not None
    result = perform_timeline(api, replace(operation, **{field: value}), clock)
    assert result.error and not result.approved
    assert not mutations(api)


def test_two_previews_per_refresh_and_at_most_eight_cached_without_refetching() -> None:
    controller, api, _clock = loaded(images=MAX_PREVIEWS)
    for cycle in range(MAX_PREVIEWS // PREVIEWS_PER_CYCLE):
        if cycle:
            refresh(controller, api)
        for _ in range(PREVIEWS_PER_CYCLE):
            preview = load_preview(controller, api)
            assert preview.png == mock_image().png
            assert len(controller.previews) <= MAX_PREVIEWS
        assert controller.next_preview() is None
    assert len(controller.previews) == MAX_PREVIEWS == 8
    refresh(controller, api, manual=True)
    assert controller.next_preview() is None
    assert [call[1] for call in api.calls if call[0] == "image"] == list(api.details)
    assert not mutations(api)


def test_failed_preview_retries_only_after_manual_refresh_and_keeps_good_previews() -> None:
    controller, api, clock = loaded(images=2)
    first = load_preview(controller, api)
    api.failures["image"] = RuntimeError("PRIVATE PIXELS OR TOKEN")
    operation = controller.next_preview()
    assert operation is not None
    result = perform_timeline(api, operation, clock)
    assert result.error and "PRIVATE" not in repr(result)
    assert controller.accept(result)
    assert controller.card_messages[operation.job_id] == PREVIEW_FAILED
    assert controller.previews[IMAGE_JOB] is first
    del api.failures["image"]
    for _ in range(3):
        refresh(controller, api)
        assert controller.next_preview() is None
    assert len([call for call in api.calls if call[0] == "image"]) == 2
    refresh(controller, api, manual=True)
    retry = controller.next_preview()
    assert retry is not None and retry.job_id == "job_image_2"
    assert controller.accept(perform_timeline(api, retry, clock))
    assert controller.previews[IMAGE_JOB] is first
    assert list(controller.previews) == [IMAGE_JOB, "job_image_2"]
    assert controller.next_preview() is None and not mutations(api)


def test_transient_timeline_failure_keeps_pixels_but_requires_fresh_authority() -> None:
    controller, api, _clock = loaded()
    preview = load_preview(controller, api)
    review = displayed_review(controller)
    assert controller.can_decide(IMAGE_JOB) and controller.can_approve(PLAN_JOB)
    api.failures["timeline"] = RuntimeError("PRIVATE SERVER BODY")
    refresh(controller, api)
    assert controller.previews[IMAGE_JOB] is preview
    assert controller.snapshot is not None and not controller.fresh
    assert controller.message == STALE_TIMELINE and "PRIVATE" not in controller.message
    assert not controller.displayed and not controller.displayed_reviews
    assert not controller.mark_displayed(IMAGE_JOB, preview)
    assert controller.decide(IMAGE_JOB, preview, "approve") is None
    assert controller.approve(PLAN_JOB, review_key(review), "") is None
    del api.failures["timeline"]
    refresh(controller, api)
    assert controller.previews[IMAGE_JOB] is preview and controller.next_preview() is None
    assert not controller.can_decide(IMAGE_JOB)
    assert controller.mark_displayed(IMAGE_JOB, preview)
    assert controller.can_decide(IMAGE_JOB)
    assert len([call for call in api.calls if call[0] == "image"]) == 1
    assert not mutations(api)


def test_initial_failed_fetch_does_not_invent_history_and_valid_empty_fetch_recovers() -> None:
    controller, api = TimelineController(Clock()), FakeAPI()
    api.failures["timeline"] = RuntimeError("private")
    refresh(controller, api)
    assert controller.snapshot is None and controller.message == INITIAL_FAILED
    assert controller.next_preview() is None and not controller.can_approve(PLAN_JOB)
    del api.failures["timeline"]
    api.payload["cards"] = []
    refresh(controller, api)
    assert controller.fresh and not controller.failed
    assert controller.snapshot is not None and controller.snapshot.cards == ()
    assert "No jobs" in controller.message and not mutations(api)


@pytest.mark.parametrize("action", ["approve_plan", "approve_image", "reject_image"])
def test_uncertain_mutation_is_never_replayed_but_fresh_pending_snapshot_allows_new_click(
    action: str,
) -> None:
    controller, api, clock = loaded()
    review = displayed_review(controller)
    preview = load_preview(controller, api)
    decision = "reject" if action == "reject_image" else "approve"
    operation = (
        controller.approve(PLAN_JOB, review_key(review), "")
        if action == "approve_plan"
        else controller.decide(IMAGE_JOB, preview, decision)
    )
    assert operation is not None
    method = "approve" if action == "approve_plan" else "decide_image"
    api.failures[method] = RuntimeError("PRIVATE TRANSPORT ERROR")
    outcome = perform_timeline(api, operation, clock)
    assert outcome.error == (APPROVAL_UNCERTAIN if action == "approve_plan" else DECISION_FAILED)
    assert "PRIVATE" not in repr(outcome) and controller.accept(outcome)
    assert not controller.accept(outcome)
    assert controller.approve(PLAN_JOB, review_key(review), "") is None
    assert controller.decide(IMAGE_JOB, preview, decision) is None
    assert len(mutations(api)) == 1
    del api.failures[method]
    refresh(controller, api)
    assert len(mutations(api)) == 1 and controller.pending is None
    if action == "approve_plan":
        assert not controller.can_approve(PLAN_JOB)
        current_review = displayed_review(controller)
        new_operation = controller.approve(PLAN_JOB, review_key(current_review), "")
    else:
        assert not controller.can_decide(IMAGE_JOB)
        current_preview = controller.previews[IMAGE_JOB]
        assert controller.mark_displayed(IMAGE_JOB, current_preview)
        new_operation = controller.decide(IMAGE_JOB, current_preview, decision)
    assert new_operation is not None and new_operation.sequence > operation.sequence
    assert controller.accept(perform_timeline(api, new_operation, clock))
    assert len(mutations(api)) == 2


@pytest.mark.parametrize(
    "response",
    [
        {"ok": False},
        {"ok": True, "job_id": "other_job", "delivery_state": "approved"},
        {"ok": True, "job_id": IMAGE_JOB, "delivery_state": "rejected"},
    ],
)
def test_wrong_image_ack_disables_authority_without_automatic_retry(
    response: dict[str, Any],
) -> None:
    controller, api, clock = loaded()
    preview = load_preview(controller, api)
    api.decision_response = response
    operation = controller.decide(IMAGE_JOB, preview, "approve")
    assert operation is not None
    result = perform_timeline(api, operation, clock)
    assert result.error == DECISION_FAILED and controller.accept(result)
    assert not controller.fresh and not controller.can_decide(IMAGE_JOB)
    assert controller.decide(IMAGE_JOB, preview, "approve") is None
    assert len(mutations(api)) == 1


@pytest.mark.parametrize("decision", ["approve", "reject"])
def test_image_decision_requires_displayed_exact_preview_and_is_not_double_submitted(
    decision: str,
) -> None:
    controller, api, clock = loaded()
    assert controller.decide(IMAGE_JOB, None, decision) is None  # type: ignore[arg-type]
    operation = controller.next_preview()
    assert operation is not None
    assert controller.accept(perform_timeline(api, operation, clock))
    preview = controller.previews[IMAGE_JOB]
    assert controller.decide(IMAGE_JOB, preview, decision) is None
    assert not controller.mark_displayed(IMAGE_JOB, replace(preview))
    assert controller.mark_displayed(IMAGE_JOB, preview)
    assert controller.decide(IMAGE_JOB, replace(preview), decision) is None
    mutation = controller.decide(IMAGE_JOB, preview, decision)
    assert mutation is not None
    assert controller.decide(IMAGE_JOB, preview, decision) is None
    assert controller.accept(perform_timeline(api, mutation, clock))
    assert IMAGE_JOB not in controller.previews and not controller.can_decide(IMAGE_JOB)
    assert mutations(api) == [
        ("decide_image", IMAGE_JOB, preview.row.sha256, preview.delivery_revision, decision)
    ]


@pytest.mark.parametrize(
    "field,value",
    [
        ("recipient_principal", "key:" + "d" * 32),
        ("sha256", "f" * 64),
        ("job_id", "wrong_job"),
        ("delivery_state", "approved"),
    ],
)
def test_preview_detail_from_wrong_principal_hash_or_row_never_enables_delivery(
    field: str,
    value: str,
) -> None:
    controller, api, clock = loaded()
    api.details[IMAGE_JOB][field] = value
    operation = controller.next_preview()
    assert operation is not None
    outcome = perform_timeline(api, operation, clock)
    assert outcome.error and outcome.preview is None and controller.accept(outcome)
    assert not controller.previews and not controller.can_decide(IMAGE_JOB)
    assert not mutations(api)


@pytest.mark.parametrize(
    "field,value",
    [
        ("recipient_principal", "key:" + "d" * 32),
        ("sha256", "f" * 64),
        ("delivery_state", "approved"),
        ("policy_allowed", True),
    ],
)
def test_changed_authoritative_image_row_discards_cached_pixels_and_display_authority(
    field: str,
    value: Any,
) -> None:
    controller, api, _clock = loaded()
    previous = load_preview(controller, api)
    api.payload["cards"][1]["image"][field] = value
    refresh(controller, api)
    assert not controller.previews and not controller.displayed
    assert not controller.mark_displayed(IMAGE_JOB, previous)
    assert controller.decide(IMAGE_JOB, previous, "approve") is None
    assert not mutations(api)


def test_image_expiry_erases_pixels_and_refresh_never_extends_retention() -> None:
    controller, api, clock = loaded()
    preview = load_preview(controller, api)
    clock.value += 100
    refresh(controller, api)
    card = controller.card(IMAGE_JOB)
    assert card is not None and card.image is not None
    assert card.image.expires_at == preview.row.expires_at == 1200
    assert controller.mark_displayed(IMAGE_JOB, preview)
    clock.value = 1200
    controller.expire()
    assert not controller.previews and not controller.displayed
    assert controller.next_preview() is None
    assert controller.decide(IMAGE_JOB, preview, "approve") is None
    assert not mutations(api)


@pytest.mark.parametrize("action", ["preview", "approve", "reject"])
def test_queued_image_operation_expires_before_worker_makes_any_request(action: str) -> None:
    controller, api, clock = loaded()
    if action == "preview":
        operation = controller.next_preview()
    else:
        preview = load_preview(controller, api)
        operation = controller.decide(IMAGE_JOB, preview, action)
    assert operation is not None
    previous_calls = list(api.calls)
    clock.value = 1200
    outcome = perform_timeline(api, operation, clock)
    assert outcome.error == EXPIRED
    assert controller.accept(outcome)
    controller.expire()
    assert api.calls == previous_calls and not controller.previews


def test_restart_clears_old_review_pixels_attempts_and_rejects_late_results() -> None:
    controller, api, clock = loaded()
    old_review = displayed_review(controller)
    old_preview = load_preview(controller, api)
    old_generation = controller.generation
    api.payload["broker_epoch"] = "d" * 32
    api.payload["cards"] = [capture_card(api.details[IMAGE_JOB])]
    refresh(controller, api)
    assert controller.generation == old_generation + 1
    assert not controller.previews and not controller.displayed and not controller.displayed_reviews
    assert not controller.preview_attempts and not controller.plan_attempts
    assert not controller.decision_attempts and not controller.card_messages
    assert "restarted" in controller.message
    assert not controller.mark_review_displayed(PLAN_JOB, old_review)
    assert not controller.mark_displayed(IMAGE_JOB, old_preview)
    current = controller.next_preview()
    assert current is not None
    stale = TimelineOutcome(replace(current, generation=old_generation), preview=old_preview)
    assert not controller.accept(stale) and controller.pending is current
    assert controller.accept(perform_timeline(api, current, clock))
    assert not mutations(api)


def test_removed_job_clears_cached_review_and_image_state() -> None:
    controller, api, _clock = loaded()
    displayed_review(controller)
    preview = load_preview(controller, api)
    api.payload["cards"] = []
    refresh(controller, api)
    assert not controller.previews and not controller.displayed_reviews
    assert not controller.preview_attempts and not controller.card_messages
    assert not controller.mark_displayed(IMAGE_JOB, preview)
    assert controller.next_preview() is None and not mutations(api)


@pytest.mark.parametrize("pending_read", ["none", "refresh", "preview"])
def test_close_clears_all_state_and_ignores_late_read_outcomes(pending_read: str) -> None:
    controller, api, clock = loaded()
    displayed_review(controller)
    if pending_read == "preview":
        operation = controller.next_preview()
    else:
        load_preview(controller, api)
        operation = controller.refresh() if pending_read == "refresh" else None
    outcome = perform_timeline(api, operation, clock) if operation is not None else None
    assert controller.stop()
    assert controller.closed and not controller.fresh and not controller.busy
    assert controller.snapshot is None and not controller.previews and not controller.displayed
    assert not controller.displayed_reviews and not controller.preview_attempts
    assert not controller.plan_attempts and not controller.decision_attempts
    assert controller.refresh() is None and controller.next_preview() is None
    if outcome is not None:
        assert not controller.accept(outcome)
    assert not mutations(api)


@pytest.mark.parametrize("action", ["approve_plan", "approve_image", "reject_image"])
def test_close_waits_for_submitted_mutation_and_does_not_cancel_or_replay_it(action: str) -> None:
    controller, api, clock = loaded()
    if action == "approve_plan":
        review = displayed_review(controller)
        operation = controller.approve(PLAN_JOB, review_key(review), "")
    else:
        preview = load_preview(controller, api)
        decision = "approve" if action == "approve_image" else "reject"
        operation = controller.decide(IMAGE_JOB, preview, decision)
    assert operation is not None
    assert not controller.stop() and not controller.closed and controller.pending is operation
    assert controller.message == PENDING_CLOSE
    assert controller.refresh() is None
    outcome = perform_timeline(api, operation, clock)
    assert controller.accept(outcome) and controller.stop()
    assert not controller.accept(outcome)
    assert controller.snapshot is None and not controller.previews and len(mutations(api)) == 1


def test_worker_uses_single_background_thread_and_never_constructs_tk(monkeypatch: Any) -> None:
    ui_calls: list[str] = []

    def forbidden_ui(*_args: Any, **_kwargs: Any) -> None:
        ui_calls.append("called")
        raise AssertionError("The timeline worker touched Tk")

    monkeypatch.setitem(
        sys.modules, "tkinter", SimpleNamespace(Tk=forbidden_ui, PhotoImage=forbidden_ui)
    )
    controller, api = TimelineController(), FakeAPI()
    worker = TimelineWorker(api)
    try:
        assert api.calls == []
        operation = controller.refresh()
        assert operation is not None
        worker.commands.put(operation)
        assert controller.accept(worker.results.get(timeout=2))
        operation = controller.next_preview()
        assert operation is not None
        worker.commands.put(operation)
        assert controller.accept(worker.results.get(timeout=2))
        preview = controller.previews[IMAGE_JOB]
        assert controller.mark_displayed(IMAGE_JOB, preview)
        operation = controller.decide(IMAGE_JOB, preview, "reject")
        assert operation is not None
        worker.commands.put(operation)
        assert controller.accept(worker.results.get(timeout=2))
        operation = controller.refresh()
        assert operation is not None
        worker.commands.put(operation)
        assert controller.accept(worker.results.get(timeout=2))
        review = displayed_review(controller)
        operation = controller.approve(PLAN_JOB, review_key(review), "")
        assert operation is not None
        worker.commands.put(operation)
        assert controller.accept(worker.results.get(timeout=2))
        assert api.threads == [worker.thread.ident] * 5
        assert worker.thread.ident != threading.get_ident()
        assert [call[0] for call in api.calls] == [
            "timeline",
            "image",
            "decide_image",
            "timeline",
            "approve",
        ]
        assert not ui_calls
    finally:
        worker.stop()
        worker.thread.join(timeout=2)
    assert not worker.thread.is_alive() and worker.results.empty()


def test_stopped_worker_drops_inflight_result_and_never_processes_queued_followup() -> None:
    api = FakeAPI()
    started, release = threading.Event(), threading.Event()

    def blocking_timeline() -> dict[str, Any]:
        api.called("timeline")
        started.set()
        assert release.wait(timeout=2)
        return copy.deepcopy(api.payload)

    api.timeline = blocking_timeline  # type: ignore[method-assign]
    worker = TimelineWorker(api)
    try:
        worker.commands.put(TimelineOperation(1, 0, "refresh", time.monotonic()))
        assert started.wait(timeout=2)
        worker.commands.put(TimelineOperation(2, 0, "refresh", time.monotonic()))
        worker.stop()
        release.set()
        worker.thread.join(timeout=2)
        assert not worker.thread.is_alive() and api.calls == [("timeline",)]
        with pytest.raises(queue.Empty):
            worker.results.get_nowait()
    finally:
        release.set()
        worker.stop()
        worker.thread.join(timeout=2)
