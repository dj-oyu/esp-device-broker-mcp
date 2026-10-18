"""Deterministic live event sequences, without workers, clocks, Tk or devices."""

import copy
from dataclasses import FrozenInstanceError
from typing import Any

import pytest
from test_gui_images import detail as image_detail
from test_gui_images import row as image_row
from test_gui_retention_review import observed_card

from cardputer_mcp.gui_live_state import (
    RESYNC_REQUIRED,
    LiveState,
    apply,
    begin,
    close,
    disconnect,
    select,
)

EPOCH = "e" * 32


def cursor(sequence: int = 0, epoch: str = EPOCH) -> dict[str, Any]:
    return {"epoch": epoch, "sequence": sequence}


def card(job: str = "job_image_1", **changes: Any) -> dict[str, Any]:
    return {
        "job_id": job,
        "plan_hash": "a" * 64,
        "test": "firmware_build",
        "backend": "registered_project",
        "state": "running",
        "stage": "idf_build",
        "result": None,
        "created_utc": "2026-10-18T10:00:00+00:00",
        "finished_utc": None,
        "cancel_available": True,
        **changes,
    }


def queue_state(**changes: Any) -> dict[str, Any]:
    return {
        "ok": True,
        "backend": "serial",
        "queue_revision": "revision-1",
        "sessions": [],
        "waiters": [],
        "owner": None,
        "active_job": "job_image_1",
        "needs_review": False,
        "ownership_uncertain": False,
        **changes,
    }


def snapshot(
    rows: list[dict[str, Any]] | None = None,
    *,
    sequence: int = 0,
    epoch: str = EPOCH,
    kind: str = "snapshot",
    **changes: Any,
) -> dict[str, Any]:
    return {
        "ok": True,
        "schema_version": 1,
        "kind": kind,
        "cursor": cursor(sequence, epoch),
        "state": queue_state(),
        "timeline": {
            "ok": True,
            "broker_epoch": epoch,
            "cards": [card()] if rows is None else rows,
            "has_more": False,
        },
        **changes,
    }


def delta(sequence: int = 1, base: int = 0, **changes: Any) -> dict[str, Any]:
    return {
        "ok": True,
        "schema_version": 1,
        "kind": "delta",
        "cursor": cursor(sequence),
        "base_cursor": cursor(base),
        "upsert": [],
        "remove": [],
        "order": ["job_image_1"],
        "has_more": False,
        "logs_changed": False,
        **changes,
    }


def ready(payload: dict[str, Any] | None = None) -> LiveState:
    state = begin(LiveState())
    result = apply(state, snapshot() if payload is None else payload, state.generation, 1000)
    assert result.connection == "live"
    return result


def test_snapshot_is_validated_frozen_and_input_is_not_mutated() -> None:
    payload = snapshot()
    original = copy.deepcopy(payload)
    initial = begin(LiveState())
    state = apply(initial, payload, initial.generation, 1000)
    assert payload == original
    assert initial.snapshot is None
    assert state.snapshot is not None and state.snapshot.active_job == "job_image_1"
    assert state.timeline is not None and state.timeline.cards[0].state == "running"
    assert state.cursor == cursor()
    payload["cursor"]["sequence"] = 900
    assert state.cursor == cursor()
    with pytest.raises(FrozenInstanceError):
        state.connection = "closed"  # type: ignore[misc]


def test_keyed_delta_changes_only_upserts_and_follows_explicit_server_order() -> None:
    state = select(ready(snapshot([card("old"), card("running")])), "running")
    previous = state.timeline
    payload = delta(
        5,
        upsert=[card("running", stage="idf_diagnostics"), card("new")],
        order=["running", "new", "old"],
        state=queue_state(queue_revision="revision-2", needs_review=True),
        has_more=True,
    )
    updated = apply(state, payload, state.generation, 1001)
    assert updated.connection == "live" and updated.cursor == cursor(5)
    assert updated.timeline is not None and previous is not None
    assert [row.job_id for row in updated.timeline.cards] == ["running", "new", "old"]
    assert updated.timeline.cards[-1] is previous.cards[0]
    assert updated.selected_card is not None and updated.selected_card.stage == "idf_diagnostics"
    assert updated.snapshot is not None and updated.snapshot.needs_review
    assert updated.timeline.has_more
    assert state.timeline is previous and previous.cards[1].stage == "idf_build"


def test_duplicate_current_cursor_is_idempotent_and_does_not_reparse_retention() -> None:
    state = ready()
    payload = delta(upsert=[card(stage="idf_diagnostics")])
    updated = apply(state, payload, state.generation, 1001)
    assert apply(updated, payload, updated.generation, 2000) is updated
    assert apply(updated, snapshot(sequence=1), updated.generation, 2000) is updated


def test_heartbeat_keeps_all_projection_and_selection_objects() -> None:
    state = select(ready(), "job_image_1")
    payload = {"ok": True, "schema_version": 1, "kind": "heartbeat", "cursor": cursor()}
    assert apply(state, payload, state.generation, 1001) is state


@pytest.mark.parametrize(
    "payload",
    [
        delta(2, base=1),
        delta(2, base=7),
        delta(2, base_cursor=cursor(0, "f" * 32)),
        delta(2, cursor=cursor(2, "f" * 32)),
        {"ok": True, "schema_version": 1, "kind": "heartbeat", "cursor": cursor(1)},
        {"ok": True, "schema_version": 1, "kind": "heartbeat", "cursor": cursor(), "state": {}},
    ],
)
def test_gaps_and_changed_heartbeat_require_resnapshot_without_lifecycle_change(payload) -> None:
    state = select(ready(), "job_image_1")
    failed = apply(state, payload, state.generation, 1001)
    assert failed.resync_required and failed.message == RESYNC_REQUIRED
    assert failed.snapshot is state.snapshot and failed.timeline is state.timeline
    assert failed.cursor is state.cursor and failed.selected_job_id == "job_image_1"
    assert failed.selected_card is not None and failed.selected_card.state == "running"
    assert apply(failed, delta(), failed.generation, 1002) is failed


def test_out_of_order_response_after_newer_delta_requires_resnapshot() -> None:
    state = ready()
    state = apply(state, delta(4), state.generation, 1001)
    reversed_response = apply(state, delta(2), state.generation, 1002)
    assert reversed_response.resync_required
    assert reversed_response.cursor == cursor(4)


def test_reconnect_fences_stale_generation_and_retains_last_validated_projection() -> None:
    live = select(ready(), "job_image_1")
    disconnected = disconnect(live, live.generation)
    assert disconnected.connection == "disconnected"
    assert disconnected.timeline is live.timeline
    assert apply(disconnected, delta(), live.generation, 1001) is disconnected
    connecting = begin(disconnected)
    assert connecting.generation == live.generation + 1
    assert apply(connecting, snapshot(sequence=20), live.generation, 1002) is connecting
    assert disconnect(connecting, live.generation) is connecting
    recovered = apply(connecting, snapshot(sequence=4), connecting.generation, 1003)
    assert recovered.connection == "live" and recovered.cursor == cursor(4)
    assert recovered.selected_job_id == "job_image_1"


def test_same_cursor_reconnect_snapshot_establishes_new_generation_as_live() -> None:
    state = begin(ready())
    recovered = apply(state, snapshot(), state.generation, 1001)
    assert recovered.connection == "live"


@pytest.mark.parametrize("kind", ["snapshot", "reset"])
def test_epoch_change_clears_selection_but_retention_reset_keeps_missing_tombstone(kind) -> None:
    state = select(ready(), "job_image_1")
    reset = apply(state, snapshot([], sequence=2, kind=kind), state.generation, 1001)
    assert reset.connection == "live" and reset.selection_missing
    assert reset.selected_job_id == "job_image_1" and reset.selected_card is None
    restarted = apply(reset, snapshot(epoch="f" * 32, kind=kind), reset.generation, 1002)
    assert restarted.connection == "live"
    assert restarted.selected_job_id == "" and not restarted.selection_missing


def test_selected_row_removal_keeps_missing_detail_and_can_reappear() -> None:
    state = select(ready(), "job_image_1")
    removed = apply(state, delta(remove=["job_image_1"], order=[]), state.generation, 1001)
    assert removed.selection_missing and removed.selected_card is None
    returned = apply(removed, delta(2, 1, upsert=[card()]), removed.generation, 1002)
    assert returned.selected_job_id == "job_image_1" and not returned.selection_missing
    assert select(returned, "").selected_card is None


def test_close_fences_all_late_completions_and_cannot_reopen_or_select() -> None:
    state = ready()
    closed = close(state)
    assert closed.connection == "closed" and closed.generation == state.generation + 1
    assert closed.timeline is state.timeline
    assert apply(closed, snapshot(sequence=3), state.generation, 1001) is closed
    assert apply(closed, snapshot(sequence=3), closed.generation, 1001) is closed
    assert disconnect(closed, closed.generation) is closed
    assert begin(closed) is closed and close(closed) is closed
    assert select(closed, "other") is closed


@pytest.mark.parametrize(
    "changes",
    [
        {"ok": False},
        {"schema_version": True},
        {"schema_version": 2},
        {"kind": "unknown"},
        {"cursor": {"epoch": EPOCH, "sequence": True}},
        {"cursor": {"epoch": EPOCH, "sequence": -1}},
        {"cursor": {"epoch": EPOCH.upper(), "sequence": 1}},
        {"cursor": {"epoch": EPOCH, "sequence": 1, "extra": "private"}},
        {"state": queue_state(needs_review="private")},
        {"timeline": {"ok": True, "broker_epoch": "f" * 32, "cards": [], "has_more": False}},
    ],
)
def test_invalid_snapshot_is_atomic_and_uses_safe_fixed_message(changes) -> None:
    state = ready()
    result = apply(state, snapshot(sequence=2, **changes), state.generation, 1001)
    assert result.resync_required and result.message == RESYNC_REQUIRED
    assert result.timeline is state.timeline and result.snapshot is state.snapshot
    assert "private" not in result.message


@pytest.mark.parametrize(
    "changes",
    [
        {"order": []},
        {"order": ["job_image_1", "job_image_1"]},
        {"order": ["job_image_1", "unknown"]},
        {"logs_changed": []},
        {"logs_changed": 1},
        {"has_more": 1},
        {"upsert": [card(), card()]},
        {"upsert": [card()], "remove": ["job_image_1"]},
        {"remove": ["invalid id"]},
        {"remove": ["job_image_1", "job_image_1"]},
        {"upsert": [card(stage="private invalid stage")]},
        {"state": queue_state(ok=False)},
        {"state": queue_state(ownership_uncertain=1)},
    ],
)
def test_invalid_delta_cannot_partially_update_state(changes) -> None:
    state = ready()
    result = apply(state, delta(**changes), state.generation, 1001)
    assert result.resync_required
    assert result.timeline is state.timeline and result.snapshot is state.snapshot
    assert result.cursor == cursor()


@pytest.mark.parametrize("started_at", [float("inf"), float("nan"), True, "private"])
def test_invalid_injected_clock_is_rejected(started_at) -> None:
    state = begin(LiveState())
    assert apply(state, snapshot(), state.generation, started_at).resync_required


@pytest.mark.parametrize("kind", ["delta", "heartbeat"])
def test_connecting_requires_snapshot_even_if_previous_projection_exists(kind) -> None:
    state = begin(ready())
    payload = delta(kind=kind)
    assert apply(state, payload, state.generation, 1001).resync_required


def test_state_axes_do_not_turn_historical_review_into_present_uncertainty() -> None:
    payload = snapshot([card(state="needs_review", reviewed=True, cancel_available=False)])
    state = ready(payload)
    assert state.timeline is not None and not state.timeline.cards[0].needs_device_review
    assert state.snapshot is not None and not state.snapshot.ownership_uncertain
    state = apply(state, delta(logs_changed=True), state.generation, 2000)
    assert state.connection == "live" and not state.snapshot.ownership_uncertain
    assert state.timeline.cards[0].state == "needs_review"
    assert disconnect(state, state.generation).timeline is state.timeline


@pytest.mark.parametrize("kind", ["image", "observation"])
def test_retention_survives_removal_reconnect_and_reset_without_deadline_renewal(kind) -> None:
    row = (
        observed_card()
        if kind == "observation"
        else card(
            test="capture_home",
            backend="mock",
            state="succeeded",
            stage=None,
            cancel_available=False,
            image=image_row(image_detail()),
        )
    )
    attribute = "device_observation" if kind == "observation" else "image"
    state = ready(snapshot([row]))
    assert state.timeline is not None
    deadline = getattr(state.timeline.cards[0], attribute).expires_at
    state = apply(state, delta(remove=["job_image_1"], order=[]), state.generation, 1050)
    state = begin(disconnect(state, state.generation))
    state = apply(state, snapshot([row], sequence=2, kind="reset"), state.generation, 1100)
    assert state.connection == "live"
    assert getattr(state.timeline.cards[0], attribute).expires_at == deadline
    state = apply(state, snapshot([row], epoch="f" * 32, kind="reset"), state.generation, 1150)
    assert getattr(state.timeline.cards[0], attribute).expires_at == deadline + 150


def test_changed_report_metadata_with_same_identity_is_rejected_and_detached_from_input() -> None:
    row = observed_card()
    state = ready(snapshot([row]))
    assert state.timeline is not None
    report = state.timeline.cards[0].device_observation
    assert report is not None
    original_bytes = report.metadata["bytes_received"]
    row["device_observation"]["metadata"]["bytes_received"] += 1
    assert report.metadata["bytes_received"] == original_bytes
    changed = apply(state, delta(upsert=[row]), state.generation, 1001)
    assert changed.resync_required and changed.timeline is state.timeline


def test_retention_capacity_fails_without_dropping_existing_identity(monkeypatch) -> None:
    monkeypatch.setattr("cardputer_mcp.gui_live_state.MAX_RETENTION_IDENTITIES", 1)
    state = ready(snapshot([observed_card()]))
    other = observed_card()
    other["job_id"] = other["device_observation"]["job_id"] = "second"
    failed = apply(
        state, delta(upsert=[other], order=["job_image_1", "second"]), state.generation, 1001
    )
    assert failed.resync_required and failed.timeline is state.timeline
