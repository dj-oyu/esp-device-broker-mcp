"""Retained image integrity without a delivery approval or device access."""

import base64
import hashlib
import struct
import zlib
from typing import Any

import pytest

from cardputer_mcp.capture import mock_image
from cardputer_mcp.gui_images import (
    INVALID_IMAGE,
    MAX_PNG_BYTES,
    SOURCES,
    ImagePreview,
    ImagesSnapshot,
    validate_png,
)


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
        "delivery_state": "available",
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


def test_hash_mismatch_and_exact_size_mismatch_are_rejected() -> None:
    image = mock_image()
    with pytest.raises(ValueError, match=INVALID_IMAGE):
        validate_png(base64.b64encode(image.png).decode(), len(image.png), "a" * 64)
    with pytest.raises(ValueError, match=INVALID_IMAGE):
        validate_png(base64.b64encode(image.png).decode(), len(image.png) - 1, image.sha256)


def test_png_crc_is_validated_even_when_sha_and_size_match() -> None:
    data = bytearray(png_bytes())
    data[29] ^= 1
    with pytest.raises(ValueError, match=INVALID_IMAGE):
        parsed_png(bytes(data))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("width", 241),
        ("label", "private arbitrary claim"),
        ("lcd_transfer_ack", True),
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
    ],
)
def test_preview_must_match_exact_selected_image_and_recipient(field: str, value: Any) -> None:
    data = detail()
    expected = ImagesSnapshot.parse(listing(data), 1000).rows[0]
    data[field] = value
    with pytest.raises(ValueError, match=INVALID_IMAGE):
        ImagePreview.parse(data, expected, 1000)


def test_preview_needs_no_delivery_revision_and_cannot_extend_ttl():
    data = detail()
    selected = ImagesSnapshot.parse(listing(data), 1000).rows[0]
    data["remaining_seconds"] = 250
    preview = ImagePreview.parse(data, selected, 1100)
    assert preview.row.expires_at == 1200
    assert preview.row.delivery_state == "available"
