"""Submitted exact-plan GUI flows with synthetic HTTP, clocks and mock devices."""

from __future__ import annotations

import copy
from typing import Any

import pytest
from test_gui_approvals import detail, listing
from test_gui_device_observation import observed_row
from test_gui_timeline import (
    Clock,
    FakeAPI,
    card,
    display_review,
    loaded,
    panel_fixture,
    refresh,
    snapshot,
)
from test_operator_timeline import prepare, running

from cardputer_mcp.gui_approvals import (
    APPROVAL_NOTICE,
    APPROVAL_UNCERTAIN,
    ATTESTATIONS,
    EXECUTION_NOTICE,
    EXECUTION_QUEUED,
    PlanList,
    PlanReview,
    approval_button_text,
    review_warning,
)
from cardputer_mcp.gui_plan_badge import PlanBadge
from cardputer_mcp.gui_timeline import (
    EXPIRED_REVIEW,
    INVALID_TIMELINE,
    TimelineController,
    TimelineSnapshot,
    perform_timeline,
    review_key,
)
from cardputer_mcp.models import CancelRequest, ExecuteRequest
from cardputer_mcp.operator_timeline import observation_summary


def submitted_card(**kwargs: Any) -> dict[str, Any]:
    result = card(**kwargs)
    result["state"] = "awaiting_approval"
    result["review"].update(execution_submitted=True, intent_remaining_seconds=20)
    return result


class SubmittedAPI(FakeAPI):
    def __init__(self, *cards: dict[str, Any]) -> None:
        super().__init__(*cards)
        self.approval_changes: dict[str, Any] = {}

    def approve(
        self,
        plan_hash: str,
        revision: str,
        attestation: str,
        retain_local_home_line: bool = False,
    ) -> dict[str, Any]:
        self.called("approve", plan_hash, revision, attestation, retain_local_home_line)
        for row in self.data["cards"]:
            if row["plan_hash"] == plan_hash:
                row.update(review=None, state="queued")
        return {
            "ok": True,
            "approved": True,
            "plan_hash": plan_hash,
            "already_approved": False,
            "execution_submitted": True,
            "job_state": "queued",
            **({"local_home_line_opt_in": True} if retain_local_home_line else {}),
            **self.approval_changes,
        }


def test_old_detail_keeps_prepare_only_copy_and_action():
    review = PlanReview.parse(detail(), 100)
    assert review.execution_submitted is False
    assert review.expires_at == 130
    assert approval_button_text(review) == "Approve plan"
    assert review_warning(review) == APPROVAL_NOTICE
    assert PlanList.parse(listing(detail()), 100).plans[0].execution_submitted is False


def test_submitted_detail_clamps_authority_to_intent_and_changes_review_identity():
    data = detail()
    old = PlanReview.parse(data, 100)
    review = PlanReview.parse(
        data | {"execution_submitted": True, "intent_remaining_seconds": 4.5}, 100
    )
    assert review.execution_submitted is True and review.expires_at == 104.5
    assert review_key(old) != review_key(review)
    assert approval_button_text(review) == "Approve & start"
    assert review_warning(review) == EXECUTION_NOTICE
    assert APPROVAL_NOTICE not in review_warning(review)


@pytest.mark.parametrize(
    "changes",
    [{"execution_submitted": value} for value in (0, 1, "true", None, [])]
    + [
        {"execution_submitted": True, "intent_remaining_seconds": value}
        for value in (None, False, True, "2", 0, -1, 31, 301, float("nan"), float("inf"))
    ]
    + [
        {"execution_submitted": False, "intent_remaining_seconds": 10},
        {"intent_remaining_seconds": 10},
    ],
)
def test_review_and_list_reject_malformed_execution_context(changes):
    data = detail() | changes
    with pytest.raises(ValueError, match="invalid_plan_response"):
        PlanReview.parse(data, 100)
    rows = listing(data)
    rows["plans"][0].update(changes)
    with pytest.raises(ValueError, match="invalid_plan_response"):
        PlanList.parse(rows, 100)


def test_pending_badge_counts_submitted_plan_and_expires_at_intent():
    rows = listing(detail())
    rows["plans"][0].update(execution_submitted=True, intent_remaining_seconds=1)
    badge = PlanBadge()
    badge.update(PlanList.parse(rows, 100), 100, 100)
    assert badge.view(100).label == "Pending (1) NEW"
    assert badge.view(101).label == "Pending (?)"


@pytest.mark.parametrize(
    "state,submitted", [("awaiting_approval", False), ("approval_required", True)]
)
def test_card_rejects_review_with_wrong_execution_state(state, submitted):
    row = submitted_card()
    row["state"] = state
    row["review"].update(
        execution_submitted=submitted, intent_remaining_seconds=20 if submitted else None
    )
    with pytest.raises(ValueError, match=INVALID_TIMELINE):
        TimelineSnapshot.parse(snapshot(row), 100)


@pytest.mark.parametrize("retain", [False, True])
def test_single_inline_click_queues_exact_submission_with_serial_opt_in(monkeypatch, retain):
    row = submitted_card(backend="serial")
    panel, _old_api, clock, _ = panel_fixture(monkeypatch, row)
    api = SubmittedAPI(row)
    view = panel.views["job-a"]
    assert view.expanded and view.approve_button.config["text"] == "Approve & start"
    assert "Submitted" in view.state_var.get() and "waiting for approval" in view.state_var.get()
    assert EXECUTION_NOTICE in view.summary_var.get()
    assert "Esc" in view.summary_var.get() and "unsaved" in view.summary_var.get()
    assert APPROVAL_NOTICE not in view.summary_var.get()
    assert not view.home_line_opt_in.hidden
    assert view.approve_button.config["state"] == "disabled"
    view.attestation_var.set(ATTESTATIONS["device_home_firmware"])
    view.home_line_opt_in_var.set("1" if retain else "")
    view._arm_plan()
    view._approve()
    view._approve()
    assert panel.worker.commands.qsize() == 1
    operation = panel.worker.commands.get_nowait()
    assert operation.retain_local_home_line is retain
    assert operation.review.execution_submitted
    assert "already submitted exact plan" in view.message_var.get()
    outcome = perform_timeline(api, operation, clock)
    assert outcome.approved and outcome.execution_submitted and not outcome.error
    panel.controller.accept(outcome)
    assert panel.controller.card_messages["job-a"] == EXECUTION_QUEUED
    assert api.calls == [
        (
            "approve",
            operation.review.plan_hash,
            "d" * 64,
            ATTESTATIONS["device_home_firmware"],
            retain,
        )
    ]
    refresh(panel.controller, api, clock)
    panel._render()
    assert len(panel.views) == 1 and panel.views["job-a"] is view
    assert view.plan_actions.hidden and not panel.controller.can_approve("job-a")
    assert "waiting for client" not in view.state_var.get().lower()


@pytest.mark.parametrize(
    "changes",
    [
        {"execution_submitted": False},
        {"execution_submitted": 1},
        {"execution_submitted": None},
        {"job_state": None},
        {"job_state": "running"},
        {"job_state": "awaiting_approval"},
        {"job_state": "injected\nstate"},
    ],
)
@pytest.mark.parametrize("retain", [False, True])
def test_uncertain_response_never_claims_queued_or_retries(changes, retain):
    clock, api = Clock(), SubmittedAPI(submitted_card(backend="serial"))
    model = TimelineController(clock)
    refresh(model, api, clock)
    review = display_review(model)
    api.approval_changes = changes
    operation = model.approve("job-a", review_key(review), ATTESTATIONS[review.attestation], retain)
    assert operation is not None
    outcome = perform_timeline(api, operation, clock)
    assert not outcome.approved and outcome.error == APPROVAL_UNCERTAIN
    model.accept(outcome)
    assert not model.can_approve("job-a", ATTESTATIONS[review.attestation])
    assert len(api.calls) == 2


def test_old_server_approval_response_cannot_confirm_submitted_execution():
    model, api, clock = loaded(submitted_card())
    review = display_review(model)
    operation = model.approve("job-a", review_key(review), "")
    assert operation is not None
    outcome = perform_timeline(api, operation, clock)
    assert not outcome.approved and outcome.error == APPROVAL_UNCERTAIN


def test_intent_expiry_disables_click_and_worker_before_network():
    model, api, clock = loaded(submitted_card())
    review = display_review(model)
    operation = model.approve("job-a", review_key(review), "")
    assert operation is not None
    clock.value = review.expires_at
    outcome = perform_timeline(api, operation, clock)
    assert outcome.error == EXPIRED_REVIEW
    assert api.calls == [("timeline",)]
    model.accept(outcome)
    assert not model.can_approve("job-a")


@pytest.mark.parametrize("change", ["revision", "submission", "stale", "expiry"])
def test_pressed_button_never_approves_changed_or_stale_review(monkeypatch, change):
    row = card() if change == "submission" else submitted_card()
    panel, api, clock, _ = panel_fixture(monkeypatch, row)
    view = panel.views["job-a"]
    view._arm_plan()
    if change == "revision":
        api.data["cards"][0]["review"]["review_revision"] = "f" * 64
    elif change == "submission":
        api.data["cards"][0] = submitted_card()
    elif change == "stale":
        api.fail.add("timeline")
    else:
        clock.value += 20
    if change != "expiry":
        refresh(panel.controller, api, clock)
    panel._render()
    view._approve()
    assert panel.controller.pending is None and panel.worker.commands.empty()
    assert not any(call[0] == "approve" for call in api.calls)


@pytest.mark.parametrize(
    "result", ["execution_intent_expired", "client_cancelled_awaiting_approval"]
)
def test_terminal_submitted_card_removes_approval_and_waiting_copy(monkeypatch, result):
    panel, api, clock, _ = panel_fixture(monkeypatch, submitted_card())
    view = panel.views["job-a"]
    api.data["cards"][0].update(state="cancelled", result=result, review=None)
    refresh(panel.controller, api, clock)
    panel._render()
    assert panel.views["job-a"] is view
    assert "Cancelled" in view.state_var.get()
    assert view.plan_actions.hidden and not panel.controller.can_approve("job-a")
    assert EXECUTION_NOTICE not in view.detail_var.get()
    assert "no image was acquired" in view.image_var.get()


async def test_operator_timeline_and_badge_keep_one_active_submitted_job(tmp_path):
    async with running(tmp_path) as (broker, plans, timeline, _clock):
        prepared = prepare(broker)
        broker.execute(
            ExecuteRequest(
                session=broker.session.capability,
                plan_hash=prepared["plan_hash"],
                wait_for_approval=True,
            )
        )
        result = timeline.snapshot()
        assert len(result["cards"]) == 1
        row = result["cards"][0]
        assert row["state"] == "awaiting_approval" and row["created_utc"] is not None
        assert row["review"]["execution_submitted"] is True
        parsed = TimelineSnapshot.parse(result | {"ok": True}, 100)
        assert parsed.cards[0].review.execution_submitted
        pending = PlanList.parse(plans.list_pending() | {"ok": True}, 100)
        assert len(pending.plans) == 1 and pending.plans[0].job_id == "one"
        assert broker.backend.calls == 0
        approved = plans.approve(prepared["plan_hash"], row["review"]["review_revision"], "")
        assert approved["execution_submitted"] and approved["job_state"] == "queued"
        next_row = timeline.snapshot()["cards"][0]
        assert next_row["state"] == "queued" and next_row["review"] is None
        assert next_row["job_id"] == row["job_id"] and next_row["plan_hash"] == row["plan_hash"]
        await broker.queue.join()


async def test_operator_timeline_rejects_detail_extensions_without_hiding_waiting_job(tmp_path):
    async with running(tmp_path) as (broker, plans, timeline, _clock):
        prepared = prepare(broker)
        broker.execute(
            ExecuteRequest(
                session=broker.session.capability,
                plan_hash=prepared["plan_hash"],
                wait_for_approval=True,
            )
        )
        original = plans.detail
        plans.detail = lambda digest: original(digest) | {"secret_extension": "private"}
        result = timeline.snapshot()
        assert result["plans_available"] is False
        assert result["cards"][0]["state"] == "awaiting_approval"
        assert result["cards"][0]["review"] is None
        assert "secret_extension" not in repr(result) and "private" not in repr(result)


@pytest.mark.parametrize("cancel", [False, True])
async def test_operator_timeline_reports_terminal_waiting_job(tmp_path, cancel):
    async with running(tmp_path) as (broker, _plans, timeline, clock):
        prepared = prepare(broker)
        broker.execute(
            ExecuteRequest(
                session=broker.session.capability,
                plan_hash=prepared["plan_hash"],
                wait_for_approval=True,
            )
        )
        if cancel:
            broker.cancel(CancelRequest(session=broker.session.capability, job_id="one"))
        else:
            clock.now += 301
        result = timeline.snapshot()
        row = result["cards"][0]
        assert row["state"] == "cancelled" and row["review"] is None
        assert row["finished_utc"] is not None and broker.backend.calls == 0
        TimelineSnapshot.parse(result | {"ok": True}, 100)


async def test_intent_expiring_during_snapshot_is_terminal_on_the_same_card(tmp_path):
    async with running(tmp_path) as (broker, plans, timeline, clock):
        prepared = prepare(broker)
        broker.execute(
            ExecuteRequest(
                session=broker.session.capability,
                plan_hash=prepared["plan_hash"],
                wait_for_approval=True,
            )
        )
        original = plans.detail

        def expiring_detail(digest):
            result = original(digest)
            clock.now += 301
            return result

        plans.detail = expiring_detail
        result = timeline.snapshot()
        assert result["plans_available"] is False
        row = result["cards"][0]
        assert row["job_id"] == "one" and row["state"] == "cancelled"
        assert row["review"] is None and row["finished_utc"] is not None
        assert broker.backend.calls == 0


async def test_submitted_job_stays_ahead_of_newer_terminal_history(tmp_path):
    async with running(tmp_path) as (broker, _plans, timeline, _clock):
        prepared = prepare(broker)
        broker.execute(
            ExecuteRequest(
                session=broker.session.capability,
                plan_hash=prepared["plan_hash"],
                wait_for_approval=True,
            )
        )
        for number in range(40):
            job_id = f"history-{number}"
            broker._store().add_job(job_id, "f" * 64)
            broker._store().set_job(job_id, "succeeded", "mock_pass")
        result = timeline.snapshot()
        assert result["has_more"] and len(result["cards"]) == 32
        row = result["cards"][0]
        assert row["job_id"] == "one" and row["state"] == "awaiting_approval"
        assert row["review"]["execution_submitted"]
        assert sum(item["job_id"] == "one" for item in result["cards"]) == 1


@pytest.mark.parametrize(
    "length,valid", [(7, False), (8, True), (9, True), (16, True), (17, False)]
)
def test_gui_metadata_accepts_only_supported_elf_prefix_lengths(length, valid):
    data = copy.deepcopy(observed_row())
    data["metadata"]["firmware"]["elf_sha256_prefix"] = "a" * length
    if valid:
        parsed = observation_summary(data, "job-a")
        firmware = parsed["metadata"]["firmware"]
        assert firmware["elf_sha256_prefix"] == "a" * length
        assert firmware["binary_sha256_verified"] is False
    else:
        with pytest.raises(ValueError, match="invalid_device_observation"):
            observation_summary(data, "job-a")
