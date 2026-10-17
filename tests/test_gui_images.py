"""Headless image-review integrity and lifecycle tests; no hardware or cloud calls."""

from __future__ import annotations

import base64
import copy
import hashlib
import queue
import struct
import sys
import threading
import time
import zlib
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from cardputer_mcp.capture import mock_image
from cardputer_mcp.gui_images import (
    CLOSING,
    DECISION_FAILED,
    EMPTY_IMAGES,
    EXPIRED,
    INVALID_IMAGE,
    MAX_PNG_BYTES,
    REQUEST_FAILED,
    SELECTION_CHANGED,
    SOURCES,
    ImageController,
    ImageOperation,
    ImageOutcome,
    ImagePanel,
    ImagePreview,
    ImagesSnapshot,
    ImageWorker,
    perform_image,
    validate_png,
)
from cardputer_mcp.operator_client import OperatorClientError


class Clock:
    def __init__(self) -> None:
        self.value = 1000.0

    def __call__(self) -> float:
        return self.value


def detail(**changes: Any) -> dict[str, Any]:
    image = mock_image()
    return {
        "ok": True,
        "job_id": "job_image_1",
        "sha256": image.sha256,
        "metadata": image.metadata(),
        "image_base64": base64.b64encode(image.png).decode("ascii"),
        "recipient_principal": "key:" + "a" * 32,
        "born_utc": "2026-10-03T13:30:00+00:00",
        "remaining_seconds": 200.0,
        "delivery_revision": "b" * 64,
        "delivery_state": "pending",
        **changes,
    }


def row(data: dict[str, Any]) -> dict[str, Any]:
    return {
        key: data[key]
        for key in (
            "job_id",
            "sha256",
            "recipient_principal",
            "born_utc",
            "remaining_seconds",
            "delivery_state",
        )
    } | {"source": data["metadata"]["source"], "policy_allowed": data.get("policy_allowed", False)}


def listing(data: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"ok": True, "broker_epoch": "e" * 32, "images": [row(data or detail())]}


class FakeAPI:
    def __init__(self, data: dict[str, Any] | None = None) -> None:
        self.data = data or detail()
        self.calls: list[tuple[Any, ...]] = []
        self.threads: list[int] = []
        self.failure: Exception | None = None
        self.decision_response: dict[str, Any] | None = None

    def _called(self, *args: Any) -> None:
        self.calls.append(args)
        self.threads.append(threading.get_ident())
        if self.failure is not None:
            raise self.failure

    def images(self) -> dict[str, Any]:
        self._called("images")
        return copy.deepcopy(listing(self.data))

    def image(self, job_id: str) -> dict[str, Any]:
        self._called("image", job_id)
        return copy.deepcopy(self.data)

    def decide_image(
        self, job_id: str, sha256: str, delivery_revision: str, decision: str
    ) -> dict[str, Any]:
        self._called("decide_image", job_id, sha256, delivery_revision, decision)
        if self.decision_response is not None:
            return self.decision_response
        self.data["delivery_state"] = "approved" if decision == "approve" else "rejected"
        return {"ok": True, "job_id": job_id, "delivery_state": self.data["delivery_state"]}


def loaded(*, displayed: bool = True) -> tuple[ImageController, FakeAPI, Clock]:
    clock = Clock()
    model, api = ImageController(clock), FakeAPI()
    refresh = model.refresh()
    assert refresh is not None
    assert model.accept(perform_image(api, refresh, clock))
    model.select(api.data["job_id"])
    preview = model.load_preview()
    assert preview is not None
    assert model.accept(perform_image(api, preview, clock))
    assert model.preview is not None
    if displayed:
        assert model.mark_displayed(model.preview)
    return model, api, clock


def chunk(kind: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + kind
        + payload
        + struct.pack(">I", zlib.crc32(kind + payload))
    )


def png_bytes(*, width: int = 240, height: int = 135, raster: bytes | None = None) -> bytes:
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raster if raster is not None else bytes(135 * 721)))
        + chunk(b"IEND", b"")
    )


def parsed_png(data: bytes) -> bytes:
    return validate_png(
        base64.b64encode(data).decode(), len(data), hashlib.sha256(data).hexdigest()
    )


def test_valid_preview_contains_exact_fixture_bytes_and_safe_source_label() -> None:
    data = detail()
    snapshot = ImagesSnapshot.parse(listing(data), 1000)
    preview = ImagePreview.parse(data, snapshot.rows[0], 1002)
    assert preview.png == mock_image().png
    assert SOURCES[preview.row.source] == "TEST / NOT DEVICE"
    assert preview.row.expires_at == 1200
    assert preview.delivery_revision == data["delivery_revision"]
    assert data["image_base64"] not in repr(preview)
    assert "PNG" not in repr(preview)


def test_device_render_buffer_label_does_not_claim_lcd_or_live_view() -> None:
    data = detail()
    data["metadata"].update(
        source="render_buffer",
        label="Device render buffer / LCD unverified",
        protocol="pocketjs-legacy-capture-v1",
        is_device_capture=True,
    )
    snapshot = ImagesSnapshot.parse(listing(data), 1000)
    preview = ImagePreview.parse(data, snapshot.rows[0], 1000)
    assert SOURCES[preview.row.source] == "Device render buffer / LCD unverified"


@pytest.mark.parametrize(
    "encoded",
    ["not base64!", "", True, None, "a" * 266669],
    ids=["malformed", "empty", "boolean", "missing", "oversized"],
)
def test_invalid_or_oversized_base64_is_rejected(encoded: Any) -> None:
    with pytest.raises(ValueError, match=INVALID_IMAGE):
        validate_png(encoded, 100, "a" * 64)


@pytest.mark.parametrize("size", [True, 0, -1, MAX_PNG_BYTES + 1, "100", None])
def test_invalid_size_metadata_is_rejected(size: Any) -> None:
    with pytest.raises(ValueError, match=INVALID_IMAGE):
        validate_png(detail()["image_base64"], size, detail()["sha256"])


def test_hash_mismatch_and_exact_size_mismatch_are_rejected() -> None:
    image = mock_image()
    with pytest.raises(ValueError, match=INVALID_IMAGE):
        validate_png(base64.b64encode(image.png).decode(), len(image.png), "a" * 64)
    with pytest.raises(ValueError, match=INVALID_IMAGE):
        validate_png(base64.b64encode(image.png).decode(), len(image.png) - 1, image.sha256)


@pytest.mark.parametrize(
    "data",
    [
        b"GIF89a" + bytes(64),
        b"\x89PNG\r\n\x1a\n",
        png_bytes(width=239),
        png_bytes(height=134),
        png_bytes(width=2**31 - 1),
        png_bytes()[:-1],
        png_bytes() + b"private trailing output",
        png_bytes(raster=b""),
        png_bytes(raster=bytes(135 * 721 + 1)),
        png_bytes(raster=b"\xff" + bytes(135 * 721 - 1)),
        png_bytes(raster=bytes(2_000_000)),
        png_bytes()[:33] + chunk(b"IDAT", b"bad zlib") + chunk(b"IEND", b""),
        png_bytes()[:33] + chunk(b"IEND", b""),
    ],
)
def test_invalid_png_signature_dimensions_structure_and_raster_are_rejected(data: bytes) -> None:
    with pytest.raises(ValueError, match=INVALID_IMAGE):
        parsed_png(data)


def test_png_crc_is_validated_even_when_sha_and_size_match() -> None:
    data = bytearray(png_bytes())
    data[29] ^= 1
    with pytest.raises(ValueError, match=INVALID_IMAGE):
        parsed_png(bytes(data))


def test_png_unknown_chunks_and_multiple_streams_are_rejected() -> None:
    header = png_bytes()[:33]
    bad = [
        header + chunk(b"acTL", bytes(8)) + png_bytes()[33:],
        header + chunk(b"IDAT", zlib.compress(bytes(135 * 721)) * 2) + chunk(b"IEND", b""),
        header
        + chunk(b"IDAT", zlib.compress(bytes(135 * 721)))
        + chunk(b"tEXt", b"note\0text")
        + chunk(b"IDAT", b"")
        + chunk(b"IEND", b""),
        header + chunk(b"tEXt", b"bad text chunk") + png_bytes()[33:],
        header + chunk(b"tEXt", b"a\0b") * 33 + png_bytes()[33:],
    ]
    for data in bad:
        with pytest.raises(ValueError, match=INVALID_IMAGE):
            parsed_png(data)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("width", 241),
        ("width", True),
        ("height", 136),
        ("mime_type", "image/gif"),
        ("is_device_capture", True),
        ("label", "private arbitrary claim"),
        ("coherent_frame", True),
        ("lcd_transfer_ack", True),
        ("capture_complete", False),
        ("protocol", "untrusted-protocol"),
        ("sha256", "a" * 64),
        ("source", "camera"),
        ("size_bytes", MAX_PNG_BYTES + 1),
    ],
)
def test_untrusted_metadata_is_rejected_with_fixed_message(field: str, value: Any) -> None:
    data = detail()
    expected = ImagesSnapshot.parse(listing(data), 1000).rows[0]
    data["metadata"][field] = value
    with pytest.raises(ValueError) as caught:
        ImagePreview.parse(data, expected, 1000)
    assert str(caught.value) == INVALID_IMAGE
    assert "private arbitrary claim" not in str(caught.value)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("job_id", "different_job"),
        ("sha256", "a" * 64),
        ("recipient_principal", "key:" + "c" * 32),
        ("born_utc", "2026-10-03T13:31:00+00:00"),
        ("delivery_state", "approved"),
        ("policy_allowed", True),
    ],
)
def test_preview_must_match_exact_selected_image_and_recipient(field: str, value: Any) -> None:
    data = detail()
    expected = ImagesSnapshot.parse(listing(data), 1000).rows[0]
    data[field] = value
    with pytest.raises(ValueError, match=INVALID_IMAGE):
        ImagePreview.parse(data, expected, 1000)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("job_id", "../private"),
        ("job_id", "a" * 65),
        ("sha256", "z" * 64),
        ("recipient_principal", "secret\nheader"),
        ("recipient_principal", "а"),
        ("recipient_principal", "a" * 129),
        ("recipient_principal", " trailing "),
        ("remaining_seconds", float("nan")),
        ("remaining_seconds", float("inf")),
        ("remaining_seconds", -1),
        ("remaining_seconds", 301),
        ("remaining_seconds", True),
        ("born_utc", "2026-10-03"),
        ("born_utc", "2026-10-03T13:31:00+01:00"),
        ("delivery_state", "sent"),
        ("policy_allowed", "yes"),
    ],
)
def test_list_rejects_unbounded_or_misleading_fields(field: str, value: Any) -> None:
    data = listing()
    data["images"][0][field] = value
    with pytest.raises(ValueError, match=INVALID_IMAGE):
        ImagesSnapshot.parse(data, 1000)


def test_list_rejects_duplicate_ids_excess_entries_and_invalid_epoch() -> None:
    data = listing()
    for bad in (
        {**data, "images": data["images"] * 2},
        {**data, "images": data["images"] * 9},
        {**data, "broker_epoch": "bad"},
        {**data, "images": {}},
        {**data, "ok": False},
        {"private": "secret"},
    ):
        with pytest.raises(ValueError, match=INVALID_IMAGE):
            ImagesSnapshot.parse(bad, 1000)


def test_approval_requires_successfully_displayed_current_preview() -> None:
    model, api, clock = loaded(displayed=False)
    assert not model.can_decide()
    assert model.decide("approve") is None
    assert model.preview is not None
    assert not model.mark_displayed(replace(model.preview, delivery_revision="c" * 64))
    assert model.mark_displayed(model.preview)
    assert model.can_decide()
    clock.value = 1200
    assert not model.can_decide() and model.decide("approve") is None
    assert len(api.calls) == 2


@pytest.mark.parametrize("decision", ["approve", "reject"])
def test_one_explicit_decision_is_bound_to_hash_revision_and_job(decision: Any) -> None:
    model, api, clock = loaded()
    operation = model.decide(decision)
    assert operation is not None
    assert not model.can_decide()
    assert model.decide(decision) is None
    assert model.refresh() is None and model.load_preview() is None
    result = perform_image(api, operation, clock)
    assert model.accept(result)
    assert api.calls[-1] == (
        "decide_image",
        api.data["job_id"],
        api.data["sha256"],
        api.data["delivery_revision"],
        decision,
    )
    assert result.delivery_state == ("approved" if decision == "approve" else "rejected")
    assert not model.can_decide()
    assert model.decide("reject" if decision == "approve" else "approve") is None
    assert len(api.calls) == 3


def test_expiration_in_queue_prevents_any_decision_http_request() -> None:
    model, api, clock = loaded()
    operation = model.decide("approve")
    assert operation is not None
    clock.value = 1200
    result = perform_image(api, operation, clock)
    assert result.error == EXPIRED
    assert len(api.calls) == 2
    assert model.accept(result) and not model.can_decide()


def test_expiration_during_preview_request_prevents_display_and_approval() -> None:
    model, api, clock = loaded()
    operation = model.load_preview()
    assert operation is not None
    result = perform_image(api, operation, clock)
    clock.value = 1200
    assert model.accept(result)
    assert model.preview is None and not model.can_decide()


def test_later_preview_response_cannot_extend_original_expiry() -> None:
    model, api, clock = loaded()
    original_deadline = model.preview.row.expires_at if model.preview else None
    clock.value += 100
    api.data["remaining_seconds"] = 200
    operation = model.load_preview()
    assert operation is not None
    assert model.accept(perform_image(api, operation, clock))
    assert model.preview is not None and model.preview.row.expires_at == original_deadline


def test_new_selection_invalidates_preview_and_ignores_late_worker_result() -> None:
    model, api, clock = loaded()
    operation = model.load_preview()
    assert operation is not None
    result = perform_image(api, operation, clock)
    model.select("different_job")
    assert not model.can_decide() and model.preview is None
    assert model.accept(result)
    assert model.message == SELECTION_CHANGED
    assert model.preview is None and not model.busy
    assert not model.accept(result)


def test_wrong_operation_result_does_not_release_busy_guard() -> None:
    model, api, clock = loaded()
    operation = model.decide("approve")
    assert operation is not None
    result = perform_image(api, operation, clock)
    for wrong in (
        replace(operation, sequence=operation.sequence - 1),
        replace(operation, action="reject"),
    ):
        assert not model.accept(replace(result, operation=wrong))
        assert model.busy
    assert model.accept(result)


def test_refresh_or_changed_recipient_never_reuses_displayed_approval() -> None:
    model, api, clock = loaded()
    previous = model.preview
    operation = model.refresh()
    assert operation is not None and previous is not None
    api.data["recipient_principal"] = "ephemeral:remote_mcp"
    assert model.accept(perform_image(api, operation, clock))
    assert model.preview is None and not model.mark_displayed(previous)
    model.select(api.data["job_id"])
    assert model.decide("approve") is None


def test_broker_restart_refresh_clears_all_previous_review_state() -> None:
    model, api, clock = loaded()
    old_preview = model.preview
    operation = model.refresh()
    assert operation is not None
    snapshot = ImagesSnapshot.parse({**listing(), "broker_epoch": "d" * 32}, clock())
    assert model.accept(ImageOutcome(operation, snapshot=snapshot))
    assert model.preview is None and model.selected_id == ""
    assert old_preview is not None and not model.mark_displayed(old_preview)
    assert model.decide("approve") is None
    assert len(api.calls) == 2


@pytest.mark.parametrize(
    "failure", [RuntimeError("PRIVATE TOKEN"), OperatorClientError("PRIVATE TOKEN")]
)
def test_transport_errors_are_redacted_and_not_retried(failure: Exception) -> None:
    model, api, clock = loaded()
    operation = model.decide("approve")
    assert operation is not None
    api.failure = failure
    outcome = perform_image(api, operation, clock)
    assert outcome.error == DECISION_FAILED
    assert "PRIVATE TOKEN" not in repr(outcome)
    assert model.accept(outcome)
    assert model.preview is None and model.snapshot is None
    assert model.decide("approve") is None
    assert len(api.calls) == 3


def test_read_parse_errors_are_redacted_and_not_retried() -> None:
    clock, api = Clock(), FakeAPI()
    api.data["recipient_principal"] = "PRIVATE\nTOKEN"
    model = ImageController(clock)
    operation = model.refresh()
    assert operation is not None
    result = perform_image(api, operation, clock)
    assert result.error == REQUEST_FAILED
    assert "PRIVATE" not in repr(result) and len(api.calls) == 1


@pytest.mark.parametrize(
    "response",
    [
        {"ok": False},
        {"ok": True, "job_id": "other", "delivery_state": "approved"},
        {"ok": True, "job_id": "job_image_1", "delivery_state": "rejected"},
    ],
)
def test_uncertain_or_wrong_decision_ack_requires_manual_refresh(response: Any) -> None:
    model, api, clock = loaded()
    api.decision_response = response
    operation = model.decide("approve")
    assert operation is not None
    result = perform_image(api, operation, clock)
    assert result.error == DECISION_FAILED
    assert model.accept(result) and not model.can_decide()
    assert model.preview is None


def test_hide_invalidates_display_but_does_not_cancel_submitted_decision() -> None:
    model, api, clock = loaded()
    operation = model.decide("approve")
    assert operation is not None
    model.hide()
    assert model.busy and not model.closed
    assert model.preview is None and model.selected_id == ""
    result = perform_image(api, operation, clock)
    assert model.accept(result)
    assert api.data["delivery_state"] == "approved"
    assert model.preview is None and not model.can_decide()
    assert "cancel" not in model.message.lower()


def test_close_during_pending_request_waits_and_cannot_issue_more_requests() -> None:
    model, api, clock = loaded()
    operation = model.decide("reject")
    assert operation is not None
    assert not model.request_close()
    assert model.closing and not model.closed and model.message == CLOSING
    assert model.refresh() is None and model.load_preview() is None
    assert model.decide("approve") is None
    outcome = perform_image(api, operation, clock)
    assert model.accept(outcome) and model.closed and not model.busy
    assert model.preview is None and model.snapshot is None
    assert not model.accept(outcome)
    assert api.data["delivery_state"] == "rejected"


def test_idle_close_clears_bytes_and_has_no_network_side_effects() -> None:
    model, api, _clock = loaded()
    assert model.request_close()
    assert model.closed and model.preview is None and model.displayed is None
    assert model.snapshot is None and model.refresh() is None
    assert len(api.calls) == 2


def test_optional_key_policy_is_distinct_from_manual_approval() -> None:
    data = detail(policy_allowed=True)
    snapshot = ImagesSnapshot.parse(listing(data), 1000)
    preview = ImagePreview.parse(data, snapshot.rows[0], 1000)
    assert preview.row.policy_allowed and preview.row.delivery_state == "pending"
    assert not ImagesSnapshot.parse(listing(), 1000).rows[0].policy_allowed


def test_background_worker_makes_no_startup_request_and_uses_one_non_tk_thread() -> None:
    api = FakeAPI()
    worker = ImageWorker(api)
    try:
        assert api.calls == []
        worker.commands.put(ImageOperation(1, 1, "list", time.monotonic()))
        result = worker.results.get(timeout=2)
        assert result.snapshot is not None
        assert api.calls == [("images",)]
        assert api.threads == [worker.thread.ident]
        assert worker.thread.ident != threading.get_ident()
    finally:
        worker.stop()
        worker.thread.join(timeout=2)
    assert not worker.thread.is_alive()
    assert worker.results.empty()


class Root:
    def __init__(self) -> None:
        self.callbacks: list[tuple[int, Any]] = []

    def after(self, milliseconds: int, callback: Any) -> None:
        self.callbacks.append((milliseconds, callback))


class Widget:
    def __init__(self) -> None:
        self.value = ""
        self.config: dict[str, Any] = {}

    def set(self, value: str) -> None:
        self.value = value

    def configure(self, **values: Any) -> None:
        self.config.update(values)


def rendered_panel(monkeypatch: Any) -> tuple[ImagePanel, list[dict[str, Any]], FakeAPI, Clock]:
    model, api, clock = loaded(displayed=False)
    panel = ImagePanel.__new__(ImagePanel)
    panel.controller = model
    panel.dialog = SimpleNamespace()  # type: ignore[assignment]
    panel.rows = model.snapshot.rows if model.snapshot else ()
    panel.rendered = panel.photo = None
    panel.image_label = Widget()  # type: ignore[assignment]
    panel.source_var = Widget()  # type: ignore[assignment]
    panel.details_var = Widget()  # type: ignore[assignment]
    panel.status_var = Widget()  # type: ignore[assignment]
    panel.status_label = Widget()  # type: ignore[assignment]
    panel.refresh_button = Widget()  # type: ignore[assignment]
    panel.preview_button = Widget()  # type: ignore[assignment]
    panel.approve_button = Widget()  # type: ignore[assignment]
    panel.reject_button = Widget()  # type: ignore[assignment]
    photos: list[dict[str, Any]] = []

    def photo(**kwargs: Any) -> object:
        photos.append(kwargs)
        return object()

    monkeypatch.setitem(
        sys.modules, "tkinter", SimpleNamespace(PhotoImage=photo, TclError=ValueError)
    )
    return panel, photos, api, clock


def test_panel_passes_exact_validated_png_to_photoimage_before_enabling_approval(
    monkeypatch: Any,
) -> None:
    panel, photos, _api, clock = rendered_panel(monkeypatch)
    assert not panel.controller.can_decide()
    panel._render()
    assert len(photos) == 1
    assert base64.b64decode(photos[0]["data"]) == mock_image().png
    assert panel.controller.can_decide()
    assert panel.approve_button.config["state"] == "normal"  # type: ignore[attr-defined]
    assert panel.source_var.value == "TEST / NOT DEVICE"  # type: ignore[attr-defined]
    assert "host clock" in panel.details_var.value  # type: ignore[attr-defined]
    assert panel.controller.preview is not None
    assert panel.controller.preview.row.recipient_principal in panel.details_var.value  # type: ignore[attr-defined]
    panel._render()
    assert len(photos) == 1
    clock.value = 1200
    panel._render()
    assert panel.preview_button.config["state"] == "disabled"  # type: ignore[attr-defined]
    assert panel.approve_button.config["state"] == "disabled"  # type: ignore[attr-defined]
    assert panel.reject_button.config["state"] == "disabled"  # type: ignore[attr-defined]
    assert panel.controller.preview is None and panel.photo is None
    assert panel.details_var.value == ""  # type: ignore[attr-defined]
    assert "expired" in panel.controller.message


@pytest.mark.parametrize("previous_preview", [False, True])
def test_empty_panel_explains_retention_without_inferring_capture_history(
    monkeypatch: Any, previous_preview: bool
) -> None:
    panel, photos, api, clock = rendered_panel(monkeypatch)
    if previous_preview:
        panel._render()
        clock.value = 1200
    else:
        panel.controller = ImageController(clock)
    panel.rows = ()

    def empty_images() -> dict[str, Any]:
        api._called("images")
        return {"ok": True, "broker_epoch": "e" * 32, "images": []}

    monkeypatch.setattr(api, "images", empty_images)
    calls_before = len(api.calls)
    for _ in range(2):
        refresh = panel.controller.refresh()
        assert refresh is not None
        assert panel.controller.accept(perform_image(api, refresh, clock))
        panel._render()
        assert panel.status_var.value == EMPTY_IMAGES  # type: ignore[attr-defined]
        assert "5 minutes after completion" in panel.controller.message
        assert "does not mean capture never ran" in panel.controller.message
        assert "Refresh does not recapture" in panel.controller.message
        assert not panel.controller.failed
        assert panel.controller.preview is None and panel.photo is None
        assert panel.details_var.value == ""  # type: ignore[attr-defined]
        assert panel.refresh_button.config["state"] == "normal"  # type: ignore[attr-defined]
        for button in (panel.preview_button, panel.approve_button, panel.reject_button):
            assert button.config["state"] == "disabled"  # type: ignore[attr-defined]
    assert api.calls[calls_before:] == [("images",), ("images",)]
    assert len(photos) == int(previous_preview)


@pytest.mark.parametrize("elapsed, remaining", [(0, 200), (0.1, 200), (199, 1), (199.9, 1)])
def test_panel_remaining_time_counts_down_without_extending_retention(
    monkeypatch: Any, elapsed: float, remaining: int
) -> None:
    panel, photos, api, clock = rendered_panel(monkeypatch)
    panel._render()
    clock.value += elapsed
    panel._render()
    assert f"Expires in: {remaining} s" in panel.details_var.value  # type: ignore[attr-defined]
    assert panel.controller.preview is not None
    assert panel.controller.preview.row.expires_at == 1200
    assert panel.preview_button.config["state"] == "normal"  # type: ignore[attr-defined]
    assert panel.approve_button.config["state"] == "normal"  # type: ignore[attr-defined]
    assert len(photos) == 1 and len(api.calls) == 2


def test_expired_selected_cached_row_disables_preview_and_sends_no_request(
    monkeypatch: Any,
) -> None:
    panel, photos, api, clock = rendered_panel(monkeypatch)
    panel.controller.preview = panel.controller.displayed = None
    clock.value = 1200
    panel._render()
    assert panel.controller.selected_row() is not None
    assert "expired" in panel.status_var.value  # type: ignore[attr-defined]
    for button in (panel.preview_button, panel.approve_button, panel.reject_button):
        assert button.config["state"] == "disabled"  # type: ignore[attr-defined]
    assert panel.controller.load_preview() is None
    assert panel.controller.message == EXPIRED
    assert panel.controller.decide("approve") is None
    assert panel.controller.decide("reject") is None
    assert not photos and len(api.calls) == 2


def test_failed_tk_decode_never_enables_approval_or_exposes_exception(monkeypatch: Any) -> None:
    panel, _photos, _api, _clock = rendered_panel(monkeypatch)

    def failed_photo(**_kwargs: Any) -> object:
        raise ValueError("private PNG content")

    monkeypatch.setitem(
        sys.modules, "tkinter", SimpleNamespace(PhotoImage=failed_photo, TclError=ValueError)
    )
    panel._render()
    assert panel.controller.preview is None
    assert not panel.controller.can_decide()
    assert "private PNG" not in panel.controller.message
    assert panel.approve_button.config["state"] == "disabled"  # type: ignore[attr-defined]


def test_panel_parent_close_guard_does_not_shutdown_if_later_confirmation_is_declined() -> None:
    root, api = Root(), FakeAPI()
    panel = ImagePanel(root, api)  # type: ignore[arg-type]
    try:
        assert panel.can_close_window()
        assert not panel.controller.closed and not panel.worker.stopped.is_set()
        assert api.calls == []
        operation = panel.controller.refresh()
        assert operation is not None
        assert not panel.can_close_window()
        assert not panel.stop()
        assert panel.controller.message == CLOSING
        assert not panel.controller.closed and not panel.worker.stopped.is_set()
        assert panel.controller.accept(perform_image(api, operation))
        assert panel.stop()
        assert panel.controller.closed and panel.worker.stopped.is_set()
    finally:
        panel.worker.stop()
        panel.worker.thread.join(timeout=2)


def test_panel_close_callback_waits_for_one_result_and_runs_once() -> None:
    panel = ImagePanel(Root(), FakeAPI())  # type: ignore[arg-type]
    calls: list[str] = []
    try:
        operation = panel.controller.refresh()
        assert operation is not None
        panel.request_close(lambda: calls.append("closed"))
        panel.request_close(lambda: calls.append("second"))
        assert calls == [] and not panel.worker.stopped.is_set()
        result = perform_image(panel.worker.api, operation)
        panel.worker.results.put(result)
        panel._drain()
        assert calls == ["closed"] and panel.controller.closed
        panel._drain()
        assert calls == ["closed"]
    finally:
        panel.worker.stop()
        panel.worker.thread.join(timeout=2)


def test_stopped_worker_does_not_process_later_queued_commands() -> None:
    api = FakeAPI()
    worker = ImageWorker(api)
    worker.stop()
    worker.thread.join(timeout=2)
    worker.commands.put(ImageOperation(1, 1, "list", time.monotonic()))
    assert api.calls == []
    with pytest.raises(queue.Empty):
        worker.results.get_nowait()


def test_rejection_removes_automatic_permission_in_local_view() -> None:
    model, api, clock = loaded()
    api.data["policy_allowed"] = True
    refresh = model.refresh()
    assert refresh is not None
    assert model.accept(perform_image(api, refresh, clock))
    model.select(api.data["job_id"])
    preview = model.load_preview()
    assert preview is not None
    assert model.accept(perform_image(api, preview, clock))
    assert model.preview is not None and model.preview.row.policy_allowed
    assert model.mark_displayed(model.preview)
    decision = model.decide("reject")
    assert decision is not None
    assert model.accept(perform_image(api, decision, clock))
    assert model.preview is not None and not model.preview.row.policy_allowed
    assert model.preview.row.delivery_state == "rejected"


def test_conflicting_detail_and_metadata_source_is_rejected() -> None:
    data = detail(source="render_buffer")
    expected = ImagesSnapshot.parse(listing(data), 1000).rows[0]
    with pytest.raises(ValueError, match=INVALID_IMAGE):
        ImagePreview.parse(data, expected, 1000)


def test_expiry_clears_retained_local_pixels() -> None:
    model, _api, clock = loaded()
    clock.value = 1200
    model.expire_preview()
    assert model.preview is None and model.displayed is None
    assert not model.can_decide()


@pytest.mark.parametrize("field", ["recipient_principal", "sha256", "source", "policy_allowed"])
def test_changed_selected_row_disables_old_displayed_approval(field: str) -> None:
    model, api, _clock = loaded()
    assert model.snapshot is not None
    changed = {
        "recipient_principal": "another-ai",
        "sha256": "f" * 64,
        "source": "render_buffer",
        "policy_allowed": True,
    }
    model.snapshot = replace(
        model.snapshot,
        rows=(replace(model.snapshot.rows[0], **{field: changed[field]}),),
    )
    assert not model.can_decide()
    assert model.decide("approve") is None
    assert len(api.calls) == 2


def test_all_image_http_methods_run_on_the_same_background_worker() -> None:
    model, api = ImageController(), FakeAPI()
    worker = ImageWorker(api)
    try:
        refresh = model.refresh()
        assert refresh is not None
        worker.commands.put(refresh)
        assert model.accept(worker.results.get(timeout=2))
        model.select(api.data["job_id"])
        preview = model.load_preview()
        assert preview is not None
        worker.commands.put(preview)
        assert model.accept(worker.results.get(timeout=2))
        assert model.preview is not None and model.mark_displayed(model.preview)
        decision = model.decide("approve")
        assert decision is not None
        worker.commands.put(decision)
        assert model.accept(worker.results.get(timeout=2))
        assert api.threads == [worker.thread.ident] * 3
        assert worker.thread.ident != threading.get_ident()
        assert not model.can_decide()
    finally:
        worker.stop()
        worker.thread.join(timeout=2)


async def test_parser_accepts_real_mock_broker_response_without_an_extra_capture(
    tmp_path: Path,
) -> None:
    from test_capture_broker import PRINCIPAL, acquire, enqueue, running

    from cardputer_mcp.broker import BrokerError

    async with running(tmp_path) as broker:
        session = acquire(broker)
        enqueue(broker, session, "gui_preview")
        await broker.queue.join()
        snapshot = ImagesSnapshot.parse({"ok": True, **broker.operator_images()}, 1000)
        preview = ImagePreview.parse(
            {"ok": True, **broker.operator_image("gui_preview")}, snapshot.rows[0], 1000
        )
        assert preview.row.recipient_principal == PRINCIPAL
        assert preview.png == mock_image().png
        assert preview.row.delivery_state == "pending"
        with pytest.raises(BrokerError, match="capture_delivery_approval_required"):
            broker.read_capture("gui_preview", PRINCIPAL)
        assert broker.backend.calls == 1
