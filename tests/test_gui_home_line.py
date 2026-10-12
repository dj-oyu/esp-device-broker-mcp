"""Synthetic local-only diagnostic tests. No device, filesystem output, or network."""

from __future__ import annotations

import copy
import threading
import time
from dataclasses import replace
from typing import Any

import pytest
from test_gui_timeline import (
    Clock,
    FakeAPI,
    card,
    display_review,
    image,
    image_row,
    panel_fixture,
    preview,
    refresh,
    snapshot,
)

from cardputer_mcp.gui_approvals import APPROVAL_UNCERTAIN, ATTESTATIONS
from cardputer_mcp.gui_home_line import (
    HOME_LINE_EXPIRED,
    HOME_LINE_FAILED,
    HOME_LINE_OPT_IN,
    HomeLinePreview,
    HomeLineRow,
)
from cardputer_mcp.gui_timeline import (
    INVALID_TIMELINE,
    TimelineController,
    TimelineOperation,
    TimelineSnapshot,
    TimelineWorker,
    perform_timeline,
    review_key,
)


def local_card(job: str = "job-a", **changes: Any) -> dict[str, Any]:
    result = card(job, backend="serial") | {"review": None, "state": "running"}
    result["local_home_line"] = {
        "job_id": job,
        "plan_hash": result["plan_hash"],
        "broker_epoch": "e" * 32,
        "principal_id": "key:" + "a" * 32,
        "diagnostic_id": "b" * 32,
        "state": "available",
        "remaining_seconds": 100.0,
        "wire_bytes": 20,
        "limit_bytes": 1024,
        "shape": None,
        **changes,
    }
    return result


class LocalAPI(FakeAPI):
    def __init__(self, *cards: dict[str, Any]) -> None:
        super().__init__(*cards)
        self.lines = {
            row["job_id"]: {
                "ok": True,
                **row["local_home_line"],
                "escaped_line": r"HOME_READY\x20\t\r\n",
            }
            for row in cards
            if row.get("local_home_line")
        }
        self.approval_changes: dict[str, Any] = {}

    def local_home_line(self, job: str, digest: str, diagnostic: str) -> dict[str, Any]:
        self.called("local_home_line", job, digest, diagnostic)
        return copy.deepcopy(self.lines[job])

    def approve(
        self, digest: str, revision: str, attestation: str, retain_local_home_line: bool = False
    ) -> dict[str, Any]:
        self.called("approve", digest, revision, attestation, retain_local_home_line)
        return {
            "ok": True,
            "approved": True,
            "plan_hash": digest,
            "already_approved": False,
            **({"local_home_line_opt_in": True} if retain_local_home_line else {}),
            **self.approval_changes,
        }


def loaded(*rows: dict[str, Any]) -> tuple[TimelineController, LocalAPI, Clock]:
    clock, api = Clock(), LocalAPI(*(rows or (local_card(),)))
    model = TimelineController(clock)
    refresh(model, api, clock)
    return model, api, clock


def fetch(model: TimelineController, api: LocalAPI, clock: Clock) -> HomeLinePreview:
    operation = model.next_home_line()
    assert operation is not None
    assert model.accept(perform_timeline(api, operation, clock))
    assert model.home_line_preview is not None
    return model.home_line_preview


@pytest.mark.parametrize("checked", [False, True])
def test_only_explicit_checked_serial_capture_approval_arms_retention(checked: bool) -> None:
    model, api, clock = loaded(card(backend="serial"))
    review = display_review(model)
    operation = model.approve(
        "job-a", review_key(review), ATTESTATIONS[review.attestation], checked
    )
    assert operation is not None and operation.retain_local_home_line is checked
    assert model.accept(perform_timeline(api, operation, clock))
    assert api.calls[-1] == (
        "approve",
        review.plan_hash,
        review.review_revision,
        ATTESTATIONS[review.attestation],
        checked,
    )


def test_unchecked_approval_retains_three_argument_api_compatibility() -> None:
    clock, api = Clock(), FakeAPI(card(backend="serial"))
    model = TimelineController(clock)
    refresh(model, api, clock)
    review = display_review(model)
    operation = model.approve("job-a", review_key(review), ATTESTATIONS[review.attestation])
    assert operation is not None
    outcome = perform_timeline(api, operation, clock)
    assert outcome.approved and not outcome.error
    assert len(api.calls[-1]) == 4


@pytest.mark.parametrize(
    "backend,test", [("mock", "capture_home"), ("serial", "hello"), ("serial", "passive_observe")]
)
def test_other_approvals_cannot_arm_retention(backend: str, test: str) -> None:
    model, api, clock = loaded(card(backend=backend, test=test))
    review = display_review(model)
    phrase = ATTESTATIONS[review.attestation]
    assert model.approve("job-a", review_key(review), phrase, True) is None
    operation = model.approve("job-a", review_key(review), phrase)
    assert operation is not None
    assert perform_timeline(api, replace(operation, retain_local_home_line=True), clock).error
    assert api.calls == [("timeline",)]


@pytest.mark.parametrize(
    "change",
    [
        {"plan_hash": "f" * 64},
        {"approved": 1},
        {"already_approved": 1},
        {"local_home_line_opt_in": False},
        {"local_home_line_opt_in": 1},
        {"ok": False},
    ],
)
def test_checked_approval_requires_confirmed_exact_hash_opt_in(change: dict[str, Any]) -> None:
    model, api, clock = loaded(card(backend="serial"))
    review = display_review(model)
    api.approval_changes = change
    operation = model.approve("job-a", review_key(review), ATTESTATIONS[review.attestation], True)
    assert operation is not None
    outcome = perform_timeline(api, operation, clock)
    assert not outcome.approved and outcome.error == APPROVAL_UNCERTAIN


@pytest.mark.parametrize(
    "change",
    [
        {"remaining_seconds": True},
        {"remaining_seconds": float("nan")},
        {"remaining_seconds": 301},
        {"remaining_seconds": -1},
        {"wire_bytes": True},
        {"wire_bytes": 65537},
        {"limit_bytes": 1025},
        {"state": "private injection"},
        {"diagnostic_id": "g" * 32},
        {"principal_id": "bad\nprincipal"},
        {"principal_id": "a" * 129},
        {"job_id": "bad\x1b"},
        {"escaped_line": "private unexpected raw"},
        {"shape": {"private": "secret"}},
    ],
)
def test_metadata_rejects_untrusted_fields_without_echo(change: dict[str, Any]) -> None:
    data = snapshot(local_card(**change))
    with pytest.raises(ValueError) as exc:
        TimelineSnapshot.parse(data, 1000)
    assert str(exc.value) == INVALID_TIMELINE


@pytest.mark.parametrize(
    "change",
    [
        {"job_id": "different"},
        {"plan_hash": "f" * 64},
        {"broker_epoch": "f" * 32},
    ],
)
def test_metadata_is_bound_to_exact_card_and_epoch(change: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match=INVALID_TIMELINE):
        TimelineSnapshot.parse(snapshot(local_card(**change)), 1000)


def test_shape_is_fixed_numeric_boolean_schema() -> None:
    shape = {
        "standard_prefix_count": 1,
        "prefix_count_capped": False,
        "body_bytes": 12,
        "home_token_count": 1,
        "token_count_capped": False,
        "token_offset": 0,
        "before_bytes": 0,
        "after_bytes": 2,
        "before_spaces": 0,
        "before_tabs": 0,
        "after_spaces": 1,
        "after_tabs": 1,
        "before_horizontal_only": True,
        "after_horizontal_only": True,
    }
    row = HomeLineRow.parse(local_card(shape=shape)["local_home_line"], 1000)
    assert dict(row.shape or ()) == shape
    for field, bad in (
        ("home_token_count", 9),
        ("standard_prefix_count", 5),
        ("body_bytes", True),
        ("after_horizontal_only", 1),
        ("before_tabs", -1),
    ):
        with pytest.raises(ValueError, match=HOME_LINE_FAILED):
            HomeLineRow.parse(local_card(shape=shape | {field: bad})["local_home_line"], 1000)


@pytest.mark.parametrize(
    "field,value",
    [
        ("job_id", "different"),
        ("plan_hash", "f" * 64),
        ("broker_epoch", "f" * 32),
        ("principal_id", "key:other"),
        ("diagnostic_id", "f" * 32),
    ],
)
def test_response_must_match_every_binding(field: str, value: str) -> None:
    model, api, clock = loaded()
    api.lines["job-a"][field] = value
    operation = model.next_home_line()
    assert operation is not None
    outcome = perform_timeline(api, operation, clock)
    assert outcome.error == HOME_LINE_FAILED and outcome.home_line_preview is None
    assert model.accept(outcome) and model.home_line_preview is None


@pytest.mark.parametrize(
    "line",
    [
        "HOME_READY\n",
        "HOME_READY\r",
        "HOME_READY\t",
        "\x1b[31mHOME_READY",
        "HOME_READY\x7f",
        "HOME_READY\u202e",
        "x" * 4097,
        "",
        None,
    ],
)
def test_raw_controls_and_oversized_text_never_reach_gui(line: Any) -> None:
    model, api, clock = loaded()
    api.lines["job-a"]["escaped_line"] = line
    operation = model.next_home_line()
    assert operation is not None
    outcome = perform_timeline(api, operation, clock)
    assert outcome.error == HOME_LINE_FAILED and outcome.home_line_preview is None


@pytest.mark.parametrize(
    "change", [{"unknown": "private"}, {"wire_bytes": 1025}, {"state": "expired"}]
)
def test_response_cannot_smuggle_extra_or_expired_text(change: dict[str, Any]) -> None:
    model, api, clock = loaded()
    api.lines["job-a"].update(change)
    operation = model.next_home_line()
    assert operation is not None
    outcome = perform_timeline(api, operation, clock)
    assert outcome.error == HOME_LINE_FAILED and outcome.home_line_preview is None


def test_escaped_value_is_unchanged_hidden_from_repr_and_absent_from_timeline() -> None:
    model, api, clock = loaded()
    retained = fetch(model, api, clock)
    assert retained.escaped_line == r"HOME_READY\x20\t\r\n"
    assert retained.escaped_line not in repr(retained)
    assert retained.escaped_line not in repr(model.snapshot)
    assert api.calls[-1] == (
        "local_home_line",
        "job-a",
        retained.row.plan_hash,
        retained.row.diagnostic_id,
    )
    assert model.next_home_line() is None
    refresh(model, api, clock, retry=True)
    assert model.next_home_line() is None


def test_cache_is_single_preview_and_evicted_before_next_fetch() -> None:
    model, api, clock = loaded(local_card(), local_card("job-b", diagnostic_id="c" * 32))
    first = fetch(model, api, clock)
    operation = model.next_home_line()
    assert operation is not None and operation.job_id == "job-b"
    assert model.home_line_preview is None
    assert model.accept(perform_timeline(api, operation, clock))
    assert model.home_line_preview is not None and model.home_line_preview is not first
    assert model.home_line_preview.row.job_id == "job-b"
    assert model.next_home_line() is None


@pytest.mark.parametrize("decision", ["approve", "reject"])
@pytest.mark.parametrize("uncertain", [False, True])
def test_image_decision_recovers_local_text_only_after_fresh_snapshot(
    decision: Any, uncertain: bool
) -> None:
    row, image_data = local_card(), image()
    # Synthetic PNG bytes exercise the serial-card UI; no device is involved.
    image_data["metadata"].update(
        source="render_buffer",
        label="Device render buffer / LCD unverified",
        is_device_capture=True,
        protocol="pocketjs-legacy-capture-v1",
    )
    row.update(state="succeeded", image=image_row(image_data))
    model, api, clock = loaded(row)
    api.images["job-a"] = image_data
    retained = fetch(model, api, clock)
    displayed = preview(model, api, clock)
    if uncertain:
        api.fail.add("decide_image")
    operation = model.decide("job-a", displayed, decision)
    assert operation is not None
    assert model.accept(perform_timeline(api, operation, clock))
    assert not model.fresh and model.home_line_preview is None
    assert retained.row.key not in model.home_line_attempts
    assert model.next_home_line() is None
    api.fail.clear()
    refresh(model, api, clock)
    assert model.home_line_preview is None
    recovered = fetch(model, api, clock)
    assert recovered is not retained and recovered.escaped_line == retained.escaped_line
    refresh(model, api, clock, retry=True)
    assert model.next_home_line() is None
    assert [call[0] for call in api.calls] == [
        "timeline",
        "local_home_line",
        "image",
        "decide_image",
        "timeline",
        "local_home_line",
        "timeline",
    ]


@pytest.mark.parametrize("failure", ["request", "invalid_metadata"])
def test_successful_local_text_can_recover_from_failed_refresh(failure: str) -> None:
    model, api, clock = loaded()
    retained = fetch(model, api, clock)
    if failure == "request":
        api.fail.add("timeline")
    else:
        api.data["cards"][0]["local_home_line"]["principal_id"] = "invalid\nprincipal"
    refresh(model, api, clock)
    assert not model.fresh and model.home_line_preview is None
    assert retained.row.key not in model.home_line_attempts
    assert model.next_home_line() is None
    api.fail.clear()
    api.data["cards"][0] = local_card()
    refresh(model, api, clock)
    assert model.home_line_preview is None
    assert fetch(model, api, clock).row.key == retained.row.key
    refresh(model, api, clock, retry=True)
    assert model.next_home_line() is None
    assert [call[0] for call in api.calls] == [
        "timeline",
        "local_home_line",
        "timeline",
        "timeline",
        "local_home_line",
        "timeline",
    ]


@pytest.mark.parametrize("retry", [False, True])
def test_failed_local_read_cannot_loop_after_refresh(retry: bool) -> None:
    model, api, clock = loaded()
    if retry:
        fetch(model, api, clock)
        api.fail.add("timeline")
        refresh(model, api, clock)
        api.fail.clear()
        refresh(model, api, clock)
    api.fail.add("local_home_line")
    operation = model.next_home_line()
    assert operation is not None
    outcome = perform_timeline(api, operation, clock)
    assert outcome.error == HOME_LINE_FAILED
    assert model.accept(outcome) and model.home_line_preview is None
    api.fail.clear()
    refresh(model, api, clock, retry=True)
    assert model.next_home_line() is None
    api.fail.add("timeline")
    refresh(model, api, clock)
    api.fail.clear()
    refresh(model, api, clock)
    assert model.next_home_line() is None
    assert sum(call[0] == "local_home_line" for call in api.calls) == 1 + retry


def test_recovery_releases_only_current_successful_preview_attempt() -> None:
    model, api, clock = loaded(local_card(), local_card("job-b", diagnostic_id="c" * 32))
    first = fetch(model, api, clock)
    second = fetch(model, api, clock)
    api.fail.add("timeline")
    refresh(model, api, clock)
    assert first.row.key in model.home_line_attempts
    assert second.row.key not in model.home_line_attempts
    api.fail.clear()
    refresh(model, api, clock)
    assert fetch(model, api, clock).row.key == second.row.key
    refresh(model, api, clock, retry=True)
    assert model.next_home_line() is None
    assert [call[1] for call in api.calls if call[0] == "local_home_line"] == [
        "job-a",
        "job-b",
        "job-b",
    ]


def test_recovery_cannot_extend_shorter_successful_response_deadline() -> None:
    model, api, clock = loaded()
    api.lines["job-a"]["remaining_seconds"] = 30
    retained = fetch(model, api, clock)
    assert retained.row.expires_at == 1030
    clock.value += 20
    api.fail.add("timeline")
    refresh(model, api, clock)
    assert model.card("job-a").local_home_line.expires_at == 1030
    api.fail.clear()
    api.lines["job-a"]["remaining_seconds"] = 100
    refresh(model, api, clock)
    assert model.card("job-a").local_home_line.expires_at == 1030
    fetch(model, api, clock)
    clock.value = 1030
    api.fail.add("timeline")
    refresh(model, api, clock)
    api.fail.clear()
    refresh(model, api, clock)
    assert model.home_line_preview is None and model.next_home_line() is None
    assert model.card("job-a").local_home_line.expires_at == 1030
    assert sum(call[0] == "local_home_line" for call in api.calls) == 2


@pytest.mark.parametrize(
    "change", ["expiry", "revoked", "job", "hash", "principal", "diagnostic", "epoch"]
)
def test_recovery_cannot_restore_text_after_expiry_or_identity_change(change: str) -> None:
    model, api, clock = loaded()
    retained = fetch(model, api, clock)
    api.fail.add("timeline")
    refresh(model, api, clock)
    api.fail.clear()
    row = api.data["cards"][0]
    if change == "expiry":
        clock.value += 100
    elif change == "revoked":
        row["local_home_line"].update(state="cleared", remaining_seconds=0)
    elif change == "job":
        api.data["cards"] = []
    elif change == "hash":
        row["plan_hash"] = row["local_home_line"]["plan_hash"] = "f" * 64
    elif change == "epoch":
        api.data["broker_epoch"] = row["local_home_line"]["broker_epoch"] = "f" * 32
    elif change == "principal":
        row["local_home_line"]["principal_id"] = "key:other"
    else:
        row["local_home_line"]["diagnostic_id"] = "f" * 32
    refresh(model, api, clock)
    assert model.home_line_preview is None
    operation = model.next_home_line()
    if change in ("expiry", "revoked", "job"):
        assert operation is None
    else:
        assert operation is not None and operation.home_line_row is not None
        assert operation.home_line_row.key != retained.row.key
        # The fake endpoint still returns the old value; it must be rejected.
        outcome = perform_timeline(api, operation, clock)
        assert outcome.error == HOME_LINE_FAILED
        assert model.accept(outcome) and model.home_line_preview is None
    refresh(model, api, clock, retry=True)
    assert model.home_line_preview is None and model.next_home_line() is None


def test_ttl_is_capped_and_refresh_cannot_extend_cached_text() -> None:
    model, api, clock = loaded(local_card(remaining_seconds=300))
    retained = fetch(model, api, clock)
    assert retained.row.expires_at == clock.value + 120
    clock.value += 60
    refresh(model, api, clock)
    assert model.card("job-a").local_home_line.expires_at == 1120
    clock.value = 1120
    model.expire()
    assert model.home_line_preview is None and model.next_home_line() is None


def test_capture_ttl_starts_after_armed_approval_lease() -> None:
    model, api, clock = loaded(local_card(state="armed", remaining_seconds=15))
    assert model.next_home_line() is None
    clock.value += 10
    api.data["cards"][0]["local_home_line"].update(state="capturing", remaining_seconds=120)
    refresh(model, api, clock)
    assert model.card("job-a").local_home_line.expires_at == 1130
    clock.value += 1
    api.data["cards"][0]["local_home_line"].update(state="available", remaining_seconds=120)
    refresh(model, api, clock)
    assert model.card("job-a").local_home_line.expires_at == 1130


@pytest.mark.parametrize("change", ["epoch", "job", "hash", "principal", "diagnostic", "state"])
def test_identity_changes_and_terminal_state_clear_text(change: str) -> None:
    model, api, clock = loaded()
    fetch(model, api, clock)
    row = api.data["cards"][0]
    if change == "epoch":
        api.data["broker_epoch"] = row["local_home_line"]["broker_epoch"] = "f" * 32
    elif change == "job":
        api.data["cards"] = []
    elif change == "hash":
        row["plan_hash"] = row["local_home_line"]["plan_hash"] = "f" * 64
    elif change == "principal":
        row["local_home_line"]["principal_id"] = "key:other"
    elif change == "diagnostic":
        row["local_home_line"]["diagnostic_id"] = "f" * 32
    else:
        row["local_home_line"]["state"] = "cleared"
    refresh(model, api, clock)
    assert model.home_line_preview is None


@pytest.mark.parametrize("stage", ["before_request", "during_request", "before_accept", "closed"])
def test_late_results_cannot_restore_text(stage: str) -> None:
    model, api, clock = loaded()
    operation = model.next_home_line()
    assert operation is not None
    if stage == "before_request":
        clock.value += 101
    if stage == "during_request":
        original = api.local_home_line

        def delayed(*args: Any) -> dict[str, Any]:
            result = original(*args)
            clock.value += 101
            return result

        api.local_home_line = delayed
    outcome = perform_timeline(api, operation, clock)
    if stage == "before_accept":
        clock.value += 101
    if stage == "closed":
        assert model.stop()
        assert not model.accept(outcome)
    else:
        assert model.accept(outcome)
    assert model.home_line_preview is None
    if stage == "before_request":
        assert api.calls == [("timeline",)] and outcome.error == HOME_LINE_EXPIRED


def test_parser_and_http_execute_only_on_worker_thread(monkeypatch: Any) -> None:
    api = LocalAPI(local_card())
    start = time.monotonic()
    row = HomeLineRow.parse(api.data["cards"][0]["local_home_line"], start)
    threads = []
    original = HomeLinePreview.parse

    def parsed(*args: Any) -> HomeLinePreview:
        threads.append(threading.get_ident())
        return original(*args)

    monkeypatch.setattr(HomeLinePreview, "parse", parsed)
    worker = TimelineWorker(api)
    try:
        worker.commands.put(
            TimelineOperation(1, 0, "home_line", start, "e" * 32, "job-a", home_line_row=row)
        )
        outcome = worker.results.get(timeout=2)
        assert outcome.home_line_preview is not None
        assert threads == api.threads == [worker.thread.ident]
        assert "HOME_READY" not in repr(outcome)
    finally:
        worker.stop()
        worker.thread.join(timeout=2)


def test_stopped_worker_drops_late_local_text() -> None:
    entered, release = threading.Event(), threading.Event()

    class BlockingAPI(LocalAPI):
        def local_home_line(self, *args: Any) -> dict[str, Any]:
            entered.set()
            assert release.wait(2)
            return super().local_home_line(*args)

    api = BlockingAPI(local_card())
    started = time.monotonic()
    row = HomeLineRow.parse(api.data["cards"][0]["local_home_line"], started)
    worker = TimelineWorker(api)
    worker.commands.put(
        TimelineOperation(1, 0, "home_line", started, "e" * 32, "job-a", home_line_row=row)
    )
    try:
        assert entered.wait(2)
        worker.stop()
        release.set()
        worker.thread.join(timeout=2)
        assert not worker.thread.is_alive() and worker.results.empty()
    finally:
        release.set()
        worker.stop()


@pytest.mark.parametrize(
    "backend,test,visible",
    [
        ("serial", "capture_home", True),
        ("mock", "capture_home", False),
        ("serial", "hello", False),
        ("serial", "passive_observe", False),
    ],
)
def test_opt_in_is_unchecked_and_only_in_manual_serial_capture_review(
    monkeypatch: Any, backend: str, test: str, visible: bool
) -> None:
    panel, _api, _clock, _ = panel_fixture(monkeypatch, card(backend=backend, test=test))
    view = panel.views["job-a"]
    assert view.home_line_opt_in_var.get() == ""
    assert view.home_line_opt_in.hidden is not visible
    assert view.home_line_opt_in.config["text"] == HOME_LINE_OPT_IN


@pytest.mark.parametrize("change", ["checked", "unchecked", "revision", "epoch"])
def test_press_release_cannot_arm_changed_or_unseen_checkbox(monkeypatch: Any, change: str) -> None:
    panel, api, clock, _ = panel_fixture(monkeypatch, card(backend="serial"))
    view = panel.views["job-a"]
    view.attestation_var.set(ATTESTATIONS["device_home_firmware"])
    view.home_line_opt_in_var.set("1" if change == "unchecked" else "")
    view._arm_plan()
    if change in ("checked", "unchecked"):
        view.home_line_opt_in_var.set("1" if change == "checked" else "")
    elif change == "revision":
        api.data["cards"][0]["review"]["review_revision"] = "f" * 64
        refresh(panel.controller, api, clock)
        panel._render()
        assert view.home_line_opt_in_var.get() == ""
    else:
        api.data["broker_epoch"] = "f" * 32
        api.data["cards"] = []
        refresh(panel.controller, api, clock)
        panel._render()
    view._approve()
    assert panel.controller.pending is None and panel.worker.commands.empty()


def test_inline_text_is_read_only_bounded_and_cleared_at_ttl_close(monkeypatch: Any) -> None:
    panel, _api, clock, _ = panel_fixture(monkeypatch, local_card())
    api = LocalAPI(local_card())
    view = panel.views["job-a"]
    assert view.expanded
    view.open_override = True
    retained = fetch(panel.controller, api, clock)
    panel._render()
    assert view.home_line_text.content == retained.escaped_line
    assert view.home_line_text.config["state"] == "disabled"
    assert view.home_line_text.config["height"] == 4
    assert view.home_line_text.config["exportselection"] is False
    assert "Never sent to AI" in view.home_line_var.get()
    assert "100 s remaining" in view.home_line_var.get()
    clock.value += 100
    panel._render()
    assert not view.home_line_text.content and view.rendered_home_line is None
    assert panel.controller.home_line_preview is None
    assert "Expired" in view.home_line_var.get()
    assert panel.stop()
    assert not view.home_line_text.content and view.frame.destroyed


def test_unchanged_mock_review_render_does_not_disarm_normal_approval(monkeypatch: Any) -> None:
    panel, _api, _clock, _ = panel_fixture(monkeypatch, card())
    view = panel.views["job-a"]
    view._arm_plan()
    panel._render()
    view._approve()
    assert panel.controller.pending is not None
    assert panel.controller.pending.action == "approve_plan"
    assert panel.controller.pending.retain_local_home_line is False


@pytest.mark.parametrize("failure", ["request", "invalid_metadata"])
def test_refresh_failure_clears_private_cache_and_display(monkeypatch: Any, failure: str) -> None:
    panel, _api, clock, _ = panel_fixture(monkeypatch, local_card())
    api = LocalAPI(local_card())
    retained = fetch(panel.controller, api, clock)
    panel._render()
    view = panel.views["job-a"]
    assert view.home_line_text.content == retained.escaped_line
    if failure == "request":
        api.fail.add("timeline")
    else:
        api.data["cards"][0]["local_home_line"]["principal_id"] = "private\ninvalid"
    refresh(panel.controller, api, clock)
    panel._render()
    assert not panel.controller.fresh
    assert panel.controller.home_line_preview is None
    assert view.rendered_home_line is None and not view.home_line_text.content
    assert "private" not in panel.controller.message
    # Even if an old caller holds the value, stale authority cannot render it.
    panel.controller.home_line_preview = retained
    panel._render()
    assert not view.home_line_text.content


def test_auto_policy_row_has_no_path_to_diagnostic_opt_in(monkeypatch: Any) -> None:
    row = card(backend="serial") | {"review": None, "state": "ready"}
    panel, api, _clock, _ = panel_fixture(monkeypatch, row)
    view = panel.views["job-a"]
    assert view.home_line_opt_in.hidden and view.home_line_opt_in_var.get() == ""
    view.home_line_opt_in_var.set("1")
    view._arm_plan()
    view._approve()
    panel._drain()
    assert panel.worker.commands.empty() and api.calls == [("timeline",)]
