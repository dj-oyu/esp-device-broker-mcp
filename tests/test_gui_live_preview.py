"""Explicit retained-image preview integrity, deadline and view-ownership regressions."""

import base64
from dataclasses import FrozenInstanceError, replace
from unittest.mock import Mock

import pytest
from test_gui_images import detail, row
from test_gui_live_presentation import drain, live_window, message
from test_gui_live_state import delta, ready, snapshot
from test_gui_timeline import card

from cardputer_mcp.gui_images import ImageRow
from cardputer_mcp.gui_live_preview import EXPIRED, FAILED, PreviewState
from cardputer_mcp.gui_live_state import DISCONNECT_MESSAGES, DISCONNECTED, select
from cardputer_mcp.gui_live_transport import (
    AUTH_FAILED,
    DELIVERY_GAP,
    INVALID_RESPONSE,
    REJECTED,
    UNREACHABLE,
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


def test_preview_is_frozen_and_reads_preserve_the_original_deadline():
    state = accepted()
    assert state.preview is not None and state.row.expires_at == 1200
    assert state.preview.png == base64.b64decode(result()["image_base64"])
    assert "PNG" not in repr(state) and result()["image_base64"] not in repr(state)
    later = state.reconcile(1, EPOCH, replace(state.row, expires_at=1400), True, 1050)
    assert later.row.expires_at == 1200
    read = later.started(9).accept(1, "job_image_1", 9, result(), 1100, 1101)
    assert read.row.expires_at == 1200
    with pytest.raises(FrozenInstanceError):
        state.request_id = 3


@pytest.mark.parametrize(
    "change",
    [
        "recipient_principal",
        "sha256",
        "born_utc",
        "source",
        "missing",
        "epoch",
        "generation",
        "disconnect",
    ],
)
def test_metadata_or_authority_invalidation_clears_pixels_and_pending_read(change):
    state = accepted().started(9)
    image = accepted().row
    generation, epoch, live = 1, EPOCH, True
    if change == "missing":
        image = None
    elif change == "epoch":
        epoch = "f" * 32
    elif change == "generation":
        generation = 2
    elif change == "disconnect":
        live = False
    else:
        image = replace(
            image,
            **{
                change: {
                    "recipient_principal": "key:other",
                    "sha256": "f" * 64,
                    "born_utc": "2026-10-04T13:30:00+00:00",
                    "source": "render_buffer",
                }[change]
            },
        )
    cleared = state.reconcile(generation, epoch, image, live, 1005)
    assert cleared.preview is None and not cleared.pending
    assert cleared.accept(1, "job_image_1", 9, result(), 1001, 1005).preview is None


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


def show(window):
    window._preview()
    request_id = window.preview_state.request_id
    drain(window, image_event(window, request_id))
    return request_id


def test_button_is_explicit_and_only_validated_png_reaches_tk(window):
    window, clock, photo = window
    drain(window)
    window.runner.read_image.assert_not_called()
    show(window)
    window.runner.read_image.assert_called_once_with(1, "job_image_1", expires_at=1200)
    assert window.preview_photo is photo.return_value
    assert window.preview_state.preview is not None
    assert base64.b64decode(photo.call_args.kwargs["data"]) == window.preview_state.preview.png
    assert photo.call_args.kwargs["format"] == "png"
    assert window.api.mock_calls == [] and not window.action_pending


def test_expired_preview_dispatch_is_not_sent(window):
    window, clock, photo = window
    clock[0] = 1200
    window._preview()
    window.runner.read_image.assert_not_called()
    photo.assert_not_called()


def test_pending_preview_does_not_block_selection_a_b_a_and_late_response(window):
    window, clock, photo = window
    window._preview()
    previous = window.preview_state.request_id
    for job in ("other", "job_image_1"):
        window.job_tree.selection.return_value = (job,)
        window._select()
        window._render_preview()
    window._preview()
    current = window.preview_state.request_id
    drain(window, image_event(window, previous))
    assert window.preview_state.request_id == current and window.preview_photo is None
    photo.assert_not_called()
    drain(window, image_event(window, current))
    assert window.preview_photo is photo.return_value


def test_delayed_response_and_tk_decode_crossing_deadline_cannot_display(window):
    window, clock, photo = window
    window._preview()
    request_id = window.preview_state.request_id
    clock[0] = 1200
    drain(window, image_event(window, request_id))
    photo.assert_not_called()
    assert window.preview_photo is None
    clock[0] = 1000
    window._preview()
    request_id = window.preview_state.request_id
    photo.side_effect = lambda **kwargs: clock.__setitem__(0, 1200) or Mock()
    drain(window, image_event(window, request_id))
    assert window.preview_photo is None and window.preview_state.preview is None
    assert window.preview_image.configure.call_args.kwargs == {"image": ""}


def test_displayed_pixels_are_released_on_idle_expiry_and_metadata_removal(window):
    window, clock, photo = window
    show(window)
    clock[0] = 1200
    drain(window)
    assert window.preview_photo is None and window.displayed_preview is None
    assert window.preview_state.preview is None
    window.preview_area.grid_remove.assert_called()
    clock[0] = 1000
    show(window)
    drain(window, message(window, delta(upsert=[card(image=None)], order=["job_image_1"])))
    assert window.preview_photo is None and window.preview_state.preview is None


@pytest.mark.parametrize("operation", ["disconnect", "reconnect", "epoch", "close"])
def test_connection_generation_and_close_release_pixels_and_ignore_old_completion(
    window, operation
):
    window, clock, photo = window
    previous = show(window)
    window._preview()
    pending = window.preview_state.request_id
    if operation == "disconnect":
        drain(window, TransportEvent("disconnected", window.state.generation, {}, 1000))
    elif operation == "reconnect":
        window._reconnect()
        window._render_preview()
    elif operation == "epoch":
        drain(window, message(window, snapshot([card(image=row(detail()))], epoch="f" * 32)))
    else:
        window.timer = None
        window._finish_close()
    assert window.preview_photo is None and window.displayed_preview is None
    assert not window.preview_state.pending
    old = TransportEvent(
        "image", 1, {"job_id": "job_image_1", "request_id": pending, "response": result()}, 1000
    )
    drain(window, old)
    assert window.preview_photo is None and window.api.mock_calls == []
    assert previous != pending


@pytest.mark.parametrize(
    "error,code",
    [
        (AUTH_FAILED, "authentication_failed"),
        (DELIVERY_GAP, "delivery_gap"),
        (INVALID_RESPONSE, "invalid_response"),
        (UNREACHABLE, "unreachable"),
        (REJECTED, "rejected"),
        ("SYNTHETIC_SECRET", None),
        ({"secret": "SYNTHETIC_SECRET"}, None),
    ],
)
def test_disconnect_only_maps_fixed_transport_reasons(window, error, code):
    window, clock, photo = window
    drain(window, TransportEvent("disconnected", 1, {"error": error}, 1000))
    assert window.state.message == (DISCONNECT_MESSAGES[code] if code else DISCONNECTED)
    assert "SYNTHETIC_SECRET" not in window.state.message


def test_preview_pending_keeps_mutation_axis_ready_and_close_does_not_wait(window):
    window, clock, photo = window
    window._preview()
    assert window.preview_state.pending and window._actions_ready()
    assert not window.action_pending
    window.timer = "timer"
    window._close()
    assert window.state.connection == "closed" and not window.preview_state.pending
    window.runner.close.assert_called_once()
    assert window.api.mock_calls == []
