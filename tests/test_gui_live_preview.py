"""Automatic retained-image integrity, deadline and view-ownership regressions."""

import base64
from unittest.mock import Mock

import pytest
from test_gui_images import detail, row
from test_gui_live_presentation import drain, live_window
from test_gui_live_state import ready, snapshot
from test_gui_timeline import card

from cardputer_mcp.gui_images import ImageRow
from cardputer_mcp.gui_live_preview import EXPIRED, FAILED, PreviewState
from cardputer_mcp.gui_live_state import select
from cardputer_mcp.gui_live_transport import (
    TransportEvent,
)

EPOCH = "e" * 32


def available(ttl=200):
    return PreviewState().reconcile(
        1, EPOCH, ImageRow.parse(row(detail(remaining_seconds=ttl)), 1000), True, 1000
    )


def result(**changes):
    return detail(broker_epoch=EPOCH, **changes)


def accepted():
    return available().started(8).accept(1, "job_image_1", 8, result(), 1001, 1002)


@pytest.mark.parametrize(
    "changes",
    [
        {"broker_epoch": "f" * 32},
        {"recipient_principal": "key:other"},
        {"sha256": "f" * 64},
        {"born_utc": "2026-10-04T13:30:00+00:00"},
        {"image_base64": "unvalidated PNG"},
        {"ok": False},
    ],
)
def test_invalid_preview_never_reaches_presentation(changes):
    response = result() | changes
    state = available().started(8).accept(1, "job_image_1", 8, response, 1001, 1002)
    assert state.preview is None and state.message == FAILED and not state.pending


def test_expired_click_and_delayed_response_have_no_preview_and_no_renewal():
    state = available(ttl=1)
    assert not state.can_read(1001)
    expired = state.started(8).accept(1, "job_image_1", 8, result(), 1000, 1001)
    assert expired.preview is None and expired.message == EXPIRED
    displayed = accepted().reconcile(1, EPOCH, accepted().row, True, 1200)
    assert displayed.preview is None and not displayed.can_read(1200)


@pytest.fixture
def window(monkeypatch):
    window = live_window()
    window.state = select(ready(snapshot([card(image=row(detail()))])), "job_image_1")
    window.runner.read_image.side_effect = range(20, 100)
    window.preview_button = Mock()
    clock = [1000.0]
    monkeypatch.setattr("cardputer_mcp.gui_live.time.monotonic", lambda: clock[0])
    photo = Mock()
    monkeypatch.setattr("tkinter.PhotoImage", photo)
    return window, clock, photo


def image_event(window, request_id, response=None):
    return TransportEvent(
        "image",
        window.state.generation,
        {
            "job_id": "job_image_1",
            "request_id": request_id,
            "response": result() if response is None else response,
        },
        1000,
    )


def test_selection_loads_once_and_only_validated_png_reaches_tk(window):
    window, clock, photo = window
    drain(window)
    window.runner.read_image.assert_called_once_with(1, "job_image_1", expires_at=1200)
    request_id = window.preview_state.request_id
    drain(window, image_event(window, request_id))
    drain(window)
    window.runner.read_image.assert_called_once_with(1, "job_image_1", expires_at=1200)
    assert window.preview_photo is photo.return_value
    assert window.preview_state.preview is not None
    assert base64.b64decode(photo.call_args.kwargs["data"]) == window.preview_state.preview.png
    assert photo.call_args.kwargs["format"] == "png"
    assert window.api.mock_calls == [] and not window.action_pending


def test_failed_automatic_image_is_not_retried_until_selection_changes(window):
    window, clock, photo = window
    drain(window)
    request_id = window.preview_state.request_id
    drain(window, image_event(window, request_id, {"ok": False, "error": "not_authorized"}))
    assert "denied" in window.preview_state.message
    for _ in range(5):
        drain(window)
    assert window.runner.read_image.call_count == 1
    photo.assert_not_called()
    window.job_tree.selection.return_value = ("other",)
    window._select()
    window.job_tree.selection.return_value = ("job_image_1",)
    window._select()
    assert window.runner.read_image.call_count == 2


def saved_row(**changes):
    return row(detail(remaining_seconds=None)) | {"retention": "saved"} | changes


def saved_result(**changes):
    return result(remaining_seconds=None, retention="saved", **changes)


def test_saved_copy_has_no_timer_and_reads_after_any_delay():
    parsed = ImageRow.parse(saved_row(), 1000)
    assert parsed.saved and parsed.expires_at == float("inf")
    state = PreviewState().reconcile(1, EPOCH, parsed, True, 10**9)
    assert state.can_read(10**9) and "Saved" in state.message
    shown = state.started(8).accept(1, "job_image_1", 8, saved_result(), 10**9, 10**9 + 1)
    assert shown.preview is not None and shown.message.endswith("(saved copy)")
    assert shown.reconcile(1, EPOCH, parsed, True, 10**10).preview is not None


@pytest.mark.parametrize(
    "changes",
    [
        {"remaining_seconds": 200.0},
        {"retention": "forever"},
        {"retention": ""},
    ],
)
def test_saved_row_requires_exact_retention_without_timer(changes):
    with pytest.raises(ValueError):
        ImageRow.parse(saved_row(**changes), 1000)


def test_saved_and_ram_responses_never_substitute_for_each_other():
    saved = PreviewState().reconcile(1, EPOCH, ImageRow.parse(saved_row(), 1000), True, 1000)
    assert saved.started(8).accept(1, "job_image_1", 8, result(), 1000, 1001).message == FAILED
    ram = available()
    assert ram.started(8).accept(1, "job_image_1", 8, saved_result(), 1000, 1001).message == FAILED


def test_saved_selection_reads_without_a_deadline(monkeypatch):
    window = live_window()
    window.state = select(ready(snapshot([card(image=saved_row())])), "job_image_1")
    window.runner.read_image.side_effect = range(20, 100)
    window.preview_button = Mock()
    monkeypatch.setattr("cardputer_mcp.gui_live.time.monotonic", lambda: 10.0**9)
    photo = Mock()
    monkeypatch.setattr("tkinter.PhotoImage", photo)
    drain(window)
    window.runner.read_image.assert_called_once_with(1, "job_image_1", expires_at=None)
    drain(window, image_event(window, window.preview_state.request_id, saved_result()))
    assert window.preview_state.preview is not None and window.preview_state.preview.row.saved
    assert window.preview_photo is photo.return_value
