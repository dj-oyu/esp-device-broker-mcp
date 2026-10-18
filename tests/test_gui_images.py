"""Retained image integrity without a delivery approval or device access."""
import base64
import hashlib
import struct
import zlib
from typing import Any
import pytest
from cardputer_mcp.capture import mock_image
from cardputer_mcp.gui_images import (
    INVALID_IMAGE, MAX_PNG_BYTES, SOURCES, ImagePreview, ImagesSnapshot, validate_png,
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



def test_preview_needs_no_delivery_revision_and_cannot_extend_ttl():
    data = detail()
    selected = ImagesSnapshot.parse(listing(data), 1000).rows[0]
    data["remaining_seconds"] = 250
    preview = ImagePreview.parse(data, selected, 1100)
    assert preview.row.expires_at == 1200
    assert preview.row.delivery_state == "available"
