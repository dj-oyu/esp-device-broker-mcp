"""Headless plan review, lifecycle and worker tests; no display, device or network."""

import copy
import hashlib
import json
import queue
import threading
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest

from cardputer_mcp.gui import QueueController, _Window
from cardputer_mcp.gui_approvals import (
    APPROVAL_NOTICE,
    APPROVAL_UNCERTAIN,
    CAPTURE_NOTICE,
    MAX_PLAN_BYTES,
    PENDING_CLOSE,
    REQUEST_FAILED,
    PlanApprovalPanel,
    PlanController,
    PlanList,
    PlanOperation,
    PlanOutcome,
    PlanReview,
    PlanWorker,
    perform_plan,
    review_warning,
)


def digest(plan: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(plan, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def detail(
    *, backend: str = "mock", test: str = "diagnostics", job: str = "job-a"
) -> dict[str, Any]:
    plan = {
        "version": 1,
        "broker_epoch": "1" * 32,
        "session_id": "2" * 32,
        "backend": backend,
        "test": test,
        "job_id": job,
        "device": "mock-cardputer" if backend == "mock" else "configured-cardputer",
        "stages": ["open", "work", "close"],
    }
    attestation = "none"
    if backend == "serial" and test in ("hello", "capture_home"):
        attestation = "device_free_firmware" if test == "hello" else "device_home_firmware"
    return {
        "ok": True,
        "plan_hash": digest(plan),
        "plan": plan,
        "review_revision": "3" * 64,
        "lease_remaining_seconds": 30,
        "attestation": attestation,
    }


def listing(*details: dict[str, Any]) -> dict[str, Any]:
    return {
        "ok": True,
        "broker_epoch": "1" * 32,
        "plans": [
            {
                "plan_hash": data["plan_hash"],
                "job_id": data["plan"]["job_id"],
                "test": data["plan"]["test"],
                "backend": data["plan"]["backend"],
                "lease_remaining_seconds": data["lease_remaining_seconds"],
            }
            for data in details
        ],
        "has_more": False,
    }


class FakeAPI:
    def __init__(self, *details: dict[str, Any]) -> None:
        self.details = details or (detail(),)
        self.calls: list[tuple[Any, ...]] = []
        self.error: Exception | None = None
        self.approval_override: dict[str, Any] | None = None

    def plans(self) -> dict[str, Any]:
        self.calls.append(("plans",))
        if self.error:
            raise self.error
        return listing(*self.details)

    def plan(self, plan_hash: str) -> dict[str, Any]:
        self.calls.append(("plan", plan_hash))
        if self.error:
            raise self.error
        return copy.deepcopy(next(item for item in self.details if item["plan_hash"] == plan_hash))

    def approve(self, plan_hash: str, review_revision: str, attestation: str) -> dict[str, Any]:
        self.calls.append(("approve", plan_hash, review_revision, attestation))
        if self.error:
            raise self.error
        return self.approval_override or {
            "ok": True,
            "approved": True,
            "plan_hash": plan_hash,
            "already_approved": False,
        }


def ready(*details: dict[str, Any]) -> tuple[PlanController, FakeAPI, list[float]]:
    now = [100.0]
    controller = PlanController(lambda: now[0])
    api = FakeAPI(*details)
    operation = controller.refresh()
    assert operation is not None
    assert controller.accept(perform_plan(api, operation))
    controller.select(api.details[0]["plan_hash"])
    return controller, api, now


def inspected(*details: dict[str, Any]) -> tuple[PlanController, FakeAPI, list[float]]:
    controller, api, now = ready(*details)
    operation = controller.inspect()
    assert operation is not None
    assert controller.accept(perform_plan(api, operation))
    return controller, api, now


def approve(controller: PlanController, attestation: str = "") -> PlanOperation | None:
    assert controller.review is not None
    return controller.approve(
        controller.review.plan_hash, controller.review.review_revision, attestation
    )


def test_no_approval_before_list_and_exact_inspection() -> None:
    controller = PlanController()
    controller.select("a" * 64)
    assert not controller.can_inspect()
    assert not controller.can_approve()
    assert controller.approve("a" * 64, "b" * 64, "") is None
    controller, api, _ = ready()
    assert controller.can_inspect()
    assert not controller.can_approve()
    assert api.calls == [("plans",)]


def test_success_sends_exact_immutable_review_once_and_never_executes() -> None:
    controller, api, _ = inspected()
    assert controller.can_approve()
    operation = approve(controller)
    assert operation is not None
    assert not controller.can_approve()
    assert approve(controller) is None
    assert controller.refresh() is None
    assert controller.inspect() is None
    assert controller.accept(perform_plan(api, operation))
    assert api.calls == [
        ("plans",),
        ("plan", operation.plan_hash),
        ("approve", operation.plan_hash, "3" * 64, ""),
    ]
    assert APPROVAL_NOTICE in controller.message
    assert operation.plan_hash in controller.message
    assert not controller.can_approve()
    assert not controller.fresh
    assert not controller.accept(perform_plan(FakeAPI(), operation))


@pytest.mark.parametrize(
    "backend,test,phrase",
    [
        ("mock", "capture_home", ""),
        ("serial", "passive_observe", ""),
        ("serial", "hello", "DEVICE FREE AND FIRMWARE VERIFIED"),
        ("serial", "capture_home", "DEVICE HOME AND FIRMWARE VERIFIED"),
    ],
)
def test_exact_attestation_and_capture_warning(backend: str, test: str, phrase: str) -> None:
    controller, _, _ = inspected(detail(backend=backend, test=test))
    assert controller.review is not None
    assert controller.can_approve(phrase)
    assert not controller.can_approve(" " + phrase)
    if phrase:
        assert not controller.can_approve()
    text = review_warning(controller.review)
    if test == "capture_home":
        assert CAPTURE_NOTICE in text
    if backend == "serial" and test == "capture_home":
        assert all(
            value in text
            for value in ("HOME", "Esc", "unsaved state", "AI service", "five minutes")
        )
    if backend == "serial" and test != "passive_observe":
        assert "reset" in text and "other serial tools" in text


@pytest.mark.parametrize("delay", [30, 100])
def test_lease_expiry_blocks_review_and_final_submission(delay: float) -> None:
    controller, _, now = inspected()
    now[0] += delay
    assert not controller.can_approve()
    assert approve(controller) is None
    assert not controller.can_inspect()


def test_network_latency_counts_against_review_lease() -> None:
    controller, api, now = ready()
    operation = controller.inspect()
    assert operation is not None
    now[0] += 31
    assert controller.accept(perform_plan(api, operation))
    assert not controller.can_approve()
    assert "expired" in controller.message


def test_changed_revision_or_target_cannot_use_prior_confirmation() -> None:
    controller, _, _ = inspected()
    assert controller.review is not None
    assert controller.approve("f" * 64, controller.review.review_revision, "") is None
    assert controller.approve(controller.review.plan_hash, "f" * 64, "") is None
    assert controller.can_approve()


def test_changed_selection_away_and_back_invalidates_inflight_inspection() -> None:
    first, second = detail(), detail(job="job-b")
    controller, api, _ = ready(first, second)
    operation = controller.inspect()
    assert operation is not None
    controller.select(second["plan_hash"])
    controller.select(first["plan_hash"])
    assert controller.accept(perform_plan(api, operation))
    assert controller.selected == first["plan_hash"]
    assert controller.review is None
    assert not controller.can_approve()
    assert "selection changed" in controller.message


def test_approval_result_preserves_new_selection_and_names_actual_approved_plan() -> None:
    first, second = detail(), detail(job="job-b")
    controller, api, _ = inspected(first, second)
    operation = approve(controller)
    assert operation is not None
    controller.select(second["plan_hash"])
    assert controller.accept(perform_plan(api, operation))
    assert controller.selected == second["plan_hash"]
    assert first["plan_hash"] in controller.message
    assert controller.review is None


def test_old_and_duplicate_results_cannot_clear_new_pending_request() -> None:
    controller, api, _ = ready()
    operation = controller.inspect()
    assert operation is not None
    result = perform_plan(api, operation)
    assert not controller.accept(replace(result, operation=replace(operation, sequence=0)))
    assert controller.pending is operation
    assert controller.accept(result)
    next_operation = controller.refresh()
    assert next_operation is not None
    assert not controller.accept(result)
    assert controller.pending is next_operation


@pytest.mark.parametrize("change", ["epoch", "job", "backend", "test"])
def test_detail_must_match_listed_identity(change: str) -> None:
    controller, api, _ = ready()
    operation = controller.inspect()
    assert operation is not None
    review = PlanReview.parse(api.details[0], operation.started_at)
    field = {"epoch": "broker_epoch", "job": "job_id", "backend": "backend", "test": "test"}[change]
    assert controller.accept(PlanOutcome(operation, review=replace(review, **{field: "different"})))
    assert not controller.can_approve()
    assert controller.review is None


@pytest.mark.parametrize("action", ["refresh", "inspect", "approve"])
def test_fixed_failures_preserve_selection_and_block_replay(action: str) -> None:
    controller, api, _ = inspected()
    selected, old_review = controller.selected, controller.review
    api.error = RuntimeError("private response body and synthetic credential")
    operation = approve(controller) if action == "approve" else getattr(controller, action)()
    assert operation is not None
    outcome = perform_plan(api, operation)
    assert outcome.failed
    assert controller.accept(outcome)
    assert controller.selected == selected and controller.review is old_review
    assert controller.message == (APPROVAL_UNCERTAIN if action == "approve" else REQUEST_FAILED)
    assert "credential" not in repr(outcome)
    assert not controller.can_approve()
    assert approve(controller) is None
    assert not controller.can_inspect()
    assert controller.refresh() is not None


def test_malformed_approval_response_is_uncertain_and_not_retried() -> None:
    controller, api, _ = inspected()
    api.approval_override = {"ok": True, "approved": True, "plan_hash": "e" * 64}
    operation = approve(controller)
    assert operation is not None
    controller.accept(perform_plan(api, operation))
    assert controller.message == APPROVAL_UNCERTAIN
    assert len([call for call in api.calls if call[0] == "approve"]) == 1


def test_idempotent_approval_result_is_reported_without_repeating() -> None:
    controller, api, _ = inspected()
    operation = approve(controller)
    assert operation is not None
    api.approval_override = {
        "ok": True,
        "approved": True,
        "plan_hash": operation.plan_hash,
        "already_approved": True,
    }
    controller.accept(perform_plan(api, operation))
    assert "already approved" in controller.message
    assert not controller.can_approve()


def test_full_plan_is_immutable_and_controls_and_bidi_are_escaped() -> None:
    data = detail()
    data["plan"]["effects"] = ["quoted\nAPPROVED\x1b[31m\u202e\x00☃"]
    data["plan_hash"] = digest(data["plan"])
    review = PlanReview.parse(data, 100)
    assert "\\nAPPROVED\\u001b[31m\\u202e\\u0000\\u2603" in review.json_text
    assert json.loads(review.json_text) == data["plan"]
    data["plan"]["effects"].append("later mutation")
    assert "later mutation" not in review.json_text
    assert "\u202e" not in review.json_text and "\x1b" not in review.json_text


@pytest.mark.parametrize("value", [-1, 301, True, "1", float("nan"), float("inf")])
def test_invalid_lease_snapshot_is_rejected(value: Any) -> None:
    data = detail()
    data["lease_remaining_seconds"] = value
    with pytest.raises(ValueError):
        PlanReview.parse(data, 100)
    with pytest.raises(ValueError):
        PlanList.parse(listing(data), 100)


@pytest.mark.parametrize(
    "kind", ["huge", "duplicate", "bad_hash", "bad_job", "wrong_test", "bad_more"]
)
def test_bounded_pending_list_rejects_malformed_rows(kind: str) -> None:
    data = listing(detail())
    if kind == "huge":
        data["plans"] *= 65
    elif kind == "duplicate":
        data["plans"] *= 2
    elif kind == "bad_hash":
        data["plans"][0]["plan_hash"] = "bad\nvalue"
    elif kind == "bad_job":
        data["plans"][0]["job_id"] = "job\u202e"
    elif kind == "wrong_test":
        data["plans"][0]["test"] = "injected"
    else:
        data["has_more"] = "false"
    with pytest.raises(ValueError):
        PlanList.parse(data, 100)


@pytest.mark.parametrize("kind", ["modified", "oversize", "deep", "nan", "attestation"])
def test_invalid_detail_never_enables_approval(kind: str) -> None:
    data = detail()
    if kind == "modified":
        data["plan"]["job_id"] = "changed"
    elif kind == "oversize":
        data["plan"]["effects"] = "x" * MAX_PLAN_BYTES
    elif kind == "deep":
        nested: Any = "leaf"
        for _ in range(15):
            nested = [nested]
        data["plan"]["effects"] = nested
    elif kind == "nan":
        data["plan"]["value"] = float("nan")
    else:
        data["attestation"] = "device_free_firmware"
    with pytest.raises(ValueError):
        PlanReview.parse(data, 100)


def test_close_waits_for_request_result_without_claiming_cancellation() -> None:
    controller, api, _ = inspected()
    operation = approve(controller)
    assert operation is not None
    assert not controller.close()
    assert controller.message == PENDING_CLOSE
    assert not controller.closed
    assert controller.accept(perform_plan(api, operation))
    assert controller.close()
    assert controller.closed and not controller.can_approve()
    assert controller.refresh() is None


def test_worker_calls_api_only_on_worker_thread_without_tk_or_replay() -> None:
    calls: list[int] = []
    started, release = threading.Event(), threading.Event()

    class BlockingAPI(FakeAPI):
        def approve(self, *args: Any) -> dict[str, Any]:
            calls.append(threading.get_ident())
            started.set()
            assert release.wait(2)
            return super().approve(*args)

    api = BlockingAPI()
    worker = PlanWorker(api)
    operation = PlanOperation(1, "approve", 100, api.details[0]["plan_hash"], "3" * 64)
    try:
        worker.commands.put(operation)
        assert started.wait(2)
        assert not worker.results.qsize()
        release.set()
        outcome = worker.results.get(timeout=2)
        assert outcome.approved
        assert calls == [worker.thread.ident]
        assert calls[0] != threading.get_ident()
        assert api.calls == [("approve", operation.plan_hash, "3" * 64, "")]
    finally:
        release.set()
        worker.stop()
        worker.thread.join(timeout=2)
    assert not worker.thread.is_alive()


class Value:
    def __init__(self, value: str = "") -> None:
        self.value = value

    def get(self) -> str:
        return self.value


def approval_panel(controller: PlanController, attestation: str = "") -> PlanApprovalPanel:
    panel = object.__new__(PlanApprovalPanel)
    panel.controller = controller
    panel.rendered_review = controller.review
    panel.attestation_var = Value(attestation)
    panel.worker = SimpleNamespace(commands=queue.Queue())
    panel._render = lambda: None
    return panel


def test_show_existing_panel_refreshes_on_same_worker_without_duplicate_or_ui_api_call() -> None:
    controller, _, _ = inspected()
    old_review = controller.review
    panel = approval_panel(controller)
    calls: list[str] = []
    panel.dialog = SimpleNamespace(
        deiconify=lambda: calls.append("show"), lift=lambda: calls.append("lift")
    )
    started, release = threading.Event(), threading.Event()
    threads: list[int] = []

    class BlockingAPI(FakeAPI):
        def plans(self) -> dict[str, Any]:
            threads.append(threading.get_ident())
            assert threads[-1] != threading.main_thread().ident
            started.set()
            assert release.wait(2)
            return super().plans()

    api = BlockingAPI(detail(), detail(job="new-plan"))
    panel.worker = worker = PlanWorker(api)
    try:
        panel.show()
        assert started.wait(2)
        operation = controller.pending
        assert operation is not None and operation.action == "refresh"
        assert controller.review is old_review
        assert not controller.review_valid and not controller.can_approve()
        panel.show()
        assert calls == ["show", "lift", "show", "lift"]
        assert panel.worker is worker
        assert controller.pending is operation
        assert worker.commands.empty()
        release.set()
        assert controller.accept(worker.results.get(timeout=2))
        assert api.calls == [("plans",)]
        assert threads == [worker.thread.ident]
        assert controller.listing is not None
        assert [row.job_id for row in controller.listing.plans] == ["job-a", "new-plan"]
        assert controller.can_inspect() and not controller.can_approve()
    finally:
        release.set()
        worker.stop()
        worker.thread.join(timeout=2)
    assert not worker.thread.is_alive()


@pytest.mark.parametrize("action", ["inspect", "approve"])
def test_show_existing_busy_panel_only_raises_without_replacing_request(action: str) -> None:
    controller, _, _ = inspected()
    panel = approval_panel(controller)
    calls: list[str] = []
    panel.dialog = SimpleNamespace(
        deiconify=lambda: calls.append("show"), lift=lambda: calls.append("lift")
    )
    operation = approve(controller) if action == "approve" else controller.inspect()
    assert operation is not None
    panel.show()
    assert calls == ["show", "lift"]
    assert panel.worker.commands.empty()
    assert controller.pending is operation


@pytest.mark.parametrize(
    "backend,test,phrase",
    [
        ("mock", "diagnostics", ""),
        ("serial", "passive_observe", ""),
        ("serial", "hello", "DEVICE FREE AND FIRMWARE VERIFIED"),
        ("serial", "capture_home", "DEVICE HOME AND FIRMWARE VERIFIED"),
    ],
)
def test_one_approval_click_submits_displayed_review_once_without_popup(
    backend: str, test: str, phrase: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    controller, api, _ = inspected(detail(backend=backend, test=test))
    review = controller.review
    assert review is not None
    panel = approval_panel(controller, phrase)

    def unexpected_confirmation(*_args: Any, **_kwargs: Any) -> bool:
        pytest.fail("The approval button must be the final confirmation click")

    monkeypatch.setattr("tkinter.messagebox.askyesno", unexpected_confirmation)
    panel._approve()
    assert controller.busy
    assert panel.worker.commands.qsize() == 1
    operation = panel.worker.commands.get_nowait()
    assert operation is controller.pending
    assert operation.action == "approve"
    assert (operation.plan_hash, operation.review_revision, operation.attestation) == (
        review.plan_hash,
        review.review_revision,
        phrase,
    )

    # Further clicks cannot enqueue a duplicate while pending or after success.
    panel._approve()
    assert panel.worker.commands.empty()
    assert controller.accept(perform_plan(api, operation))
    panel._approve()
    assert panel.worker.commands.empty()
    assert api.calls == [
        ("plans",),
        ("plan", review.plan_hash),
        ("approve", review.plan_hash, review.review_revision, phrase),
    ]


@pytest.mark.parametrize(
    "change",
    ["uninspected", "unrendered", "expire", "select", "revision", "epoch", "stale", "closed"],
)
def test_one_click_approval_rejects_missing_stale_or_changed_review(change: str) -> None:
    controller, _, now = inspected()
    panel = approval_panel(controller)
    review = controller.review
    assert review is not None
    if change == "uninspected":
        controller.review = None
    elif change == "unrendered":
        panel.rendered_review = None
    elif change == "expire":
        now[0] += 30
    elif change == "select":
        controller.select("a" * 64)
        controller.select(review.plan_hash)
    elif change == "revision":
        controller.review = replace(review, review_revision="b" * 64)
    elif change == "epoch":
        controller.review = replace(review, broker_epoch="b" * 32)
    elif change == "stale":
        controller.review_valid = False
    else:
        assert controller.close()
    panel._approve()
    assert panel.worker.commands.empty()
    assert not controller.busy


@pytest.mark.parametrize("test", ["hello", "capture_home"])
@pytest.mark.parametrize("phrase", ["", "DEVICE FREE AND FIRMWARE VERIFIED ", "verified"])
def test_one_click_serial_approval_requires_exact_attestation(test: str, phrase: str) -> None:
    controller, _, _ = inspected(detail(backend="serial", test=test))
    panel = approval_panel(controller, phrase)
    panel._approve()
    assert panel.worker.commands.empty()
    assert not controller.busy


@pytest.mark.parametrize("action", ["refresh", "inspect"])
def test_one_click_approval_cannot_replace_an_inflight_request(action: str) -> None:
    controller, _, _ = inspected()
    panel = approval_panel(controller)
    operation = getattr(controller, action)()
    assert operation is not None
    panel._approve()
    assert panel.worker.commands.empty()
    assert controller.pending is operation


def test_main_window_pending_guard_does_not_close_or_stop_other_panels() -> None:
    calls: list[str] = []

    class Panel:
        def can_close_window(self) -> bool:
            calls.append("guard")
            return False

        def stop(self) -> bool:
            calls.append("stop")
            return True

    window = object.__new__(_Window)
    window.plan_panel = Panel()
    window._finish_close = lambda: calls.append("finish")
    window._close()
    assert calls == ["guard"]


def test_main_window_stops_panels_only_after_tunnel_close_succeeds() -> None:
    calls: list[str] = []

    class Panel:
        def can_close_window(self) -> bool:
            calls.append("guard")
            return True

        def stop(self) -> bool:
            calls.append("stop")
            return True

    class Tunnel:
        def request_close(self, callback: Any) -> None:
            self.callback = callback

    class Root:
        def destroy(self) -> None:
            calls.append("destroy")

    window = object.__new__(_Window)
    window.plan_panel = Panel()
    window.image_panel = Panel()
    window.tunnel_panel = tunnel = Tunnel()
    window.controller = QueueController()
    window.stopped = threading.Event()
    window.commands = queue.Queue()
    window.root = Root()
    window._close()
    assert calls == ["guard", "guard"]
    assert not window.stopped.is_set()
    tunnel.callback()
    assert calls == ["guard", "guard", "guard", "guard", "stop", "stop", "destroy"]
    assert window.stopped.is_set()


def test_panel_pending_close_is_visible_and_does_not_stop_worker() -> None:
    controller, _, _ = inspected()
    assert approve(controller) is not None
    panel = object.__new__(PlanApprovalPanel)
    panel.controller = controller
    calls: list[str] = []
    panel.show = lambda: calls.append("show")
    panel._render = lambda: calls.append("render")
    assert not panel.can_close_window()
    assert controller.message == PENDING_CLOSE
    assert calls == ["show", "render"]
    assert not panel.stop()
    assert not controller.closed


def test_dismiss_while_pending_preserves_dialog_then_idle_dismiss_invalidates_review() -> None:
    controller, api, _ = inspected()
    calls: list[str] = []

    class Dialog:
        def destroy(self) -> None:
            calls.append("destroy")

    panel = object.__new__(PlanApprovalPanel)
    panel.controller = controller
    panel.dialog = dialog = Dialog()
    panel.show = lambda: calls.append("show")
    panel._render = lambda: calls.append("render")
    operation = controller.inspect()
    assert operation is not None
    panel._dismiss()
    assert panel.dialog is dialog
    assert "destroy" not in calls
    controller.accept(perform_plan(api, operation))
    assert controller.can_approve()
    panel._dismiss()
    assert panel.dialog is None
    assert calls[-1] == "destroy"
    assert controller.review is not None and not controller.can_approve()


def test_full_list_supports_64_unique_rows_and_reports_truncation() -> None:
    data = listing(*(detail(job=f"job-{index}") for index in range(64)))
    data["has_more"] = True
    parsed = PlanList.parse(data, 100)
    assert len(parsed.plans) == 64 and parsed.has_more
    controller = PlanController(lambda: 100)
    operation = controller.refresh()
    assert operation is not None
    assert controller.accept(PlanOutcome(operation, listing=parsed))
    assert "up to 64" in controller.message and "more are available" in controller.message


def test_refresh_preserves_selected_hash_without_approving_previous_review() -> None:
    controller, api, _ = inspected()
    selected = controller.selected
    operation = controller.refresh()
    assert operation is not None and not controller.can_approve()
    controller.accept(perform_plan(api, operation))
    assert controller.selected == selected
    assert controller.review is not None
    assert not controller.can_approve()
    assert controller.can_inspect()
