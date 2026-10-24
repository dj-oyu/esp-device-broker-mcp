"""Deterministic live event sequences, without workers, clocks, Tk or devices."""

import copy
from dataclasses import FrozenInstanceError
from typing import Any

import pytest

from cardputer_mcp.gui_live_state import (
    DISCONNECTED,
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


@pytest.mark.parametrize(
    "reason",
    [
        None,
        "authentication_failed token=SYNTHETIC_PRIVATE_TOKEN",
        "https://SYNTHETIC_PRIVATE_TOKEN@example.invalid/private/path",
        {"error": "SYNTHETIC_PRIVATE_TOKEN"},
    ],
)
def test_disconnect_unknown_or_raw_reason_never_exposes_transport_data(reason: Any) -> None:
    live = ready()
    disconnected = disconnect(live, live.generation, reason)
    assert disconnected.message == DISCONNECTED
    assert "SYNTHETIC_PRIVATE_TOKEN" not in repr(disconnected)
    assert "example.invalid" not in disconnected.message
    assert disconnect(live, live.generation).message == DISCONNECTED


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
