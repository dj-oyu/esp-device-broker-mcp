"""Read-only retained-image models and bounded PNG decoding before Tk display."""

from __future__ import annotations

import base64
import binascii
import hashlib
import math
import re
import struct
import zlib
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any, Literal, cast

MAX_PNG_BYTES = 200_000
MAX_IMAGES = 8
MAX_TTL = 300
WIDTH, HEIGHT = 240, 135
INVALID_IMAGE = "The broker returned an invalid image preview. Refresh the image list."
REQUEST_FAILED = "Image request failed. Refresh to check the retained image; no retry was sent."
EXPIRED = "This retained image has expired. This request was not sent; refresh images."
EMPTY_IMAGES = (
    "No retained images available. The RAM copy expires 5 minutes after completion; "
    "a saved copy is shown on the job card while it is kept. This does not mean "
    "capture never ran. Refresh does not recapture."
)
SAVED = "saved"
SOURCES = {
    "synthetic_fixture": "TEST / NOT DEVICE",
    "render_buffer": "Device render buffer / LCD unverified",
}
Source = Literal["render_buffer", "synthetic_fixture"]


def _text(value: Any, pattern: str) -> str:
    if not isinstance(value, str) or re.fullmatch(pattern, value) is None:
        raise ValueError(INVALID_IMAGE)
    return value


def _recipient(value: Any) -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 128
        or not value.isascii()
        or not value.isprintable()
        or value != value.strip()
    ):
        raise ValueError(INVALID_IMAGE)
    return value


def _seconds(value: Any) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or not 0 <= value <= MAX_TTL
    ):
        raise ValueError(INVALID_IMAGE)
    return float(value)


def _born(value: Any) -> str:
    if not isinstance(value, str) or len(value) > 40:
        raise ValueError(INVALID_IMAGE)
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() != UTC.utcoffset(None):
        raise ValueError(INVALID_IMAGE)
    return parsed.astimezone(UTC).isoformat()


@dataclass(frozen=True)
class ImageRow:
    job_id: str
    sha256: str
    source: Source
    recipient_principal: str
    born_utc: str
    expires_at: float
    delivery_state: str = "available"
    saved: bool = False

    @classmethod
    def parse(cls, data: dict[str, Any], started_at: float) -> ImageRow:
        source = data["source"]
        if source not in SOURCES:
            raise ValueError(INVALID_IMAGE)
        retention = data.get("retention")
        if retention is not None:
            # A saved copy has no timer; only the broker's caps can prune it.
            if retention != SAVED or data["remaining_seconds"] is not None:
                raise ValueError(INVALID_IMAGE)
            return cls(
                _text(data["job_id"], r"[a-zA-Z0-9_-]{1,64}"),
                _text(data["sha256"], r"[0-9a-f]{64}"),
                cast(Source, source),
                _recipient(data["recipient_principal"]),
                _born(data["born_utc"]),
                math.inf,
                saved=True,
            )
        return cls(
            _text(data["job_id"], r"[a-zA-Z0-9_-]{1,64}"),
            _text(data["sha256"], r"[0-9a-f]{64}"),
            cast(Source, source),
            _recipient(data["recipient_principal"]),
            _born(data["born_utc"]),
            started_at + _seconds(data["remaining_seconds"]),
        )

    def same_image(self, other: ImageRow) -> bool:
        return (
            self.job_id,
            self.sha256,
            self.source,
            self.recipient_principal,
            self.born_utc,
        ) == (
            other.job_id,
            other.sha256,
            other.source,
            other.recipient_principal,
            other.born_utc,
        )


@dataclass(frozen=True)
class ImagesSnapshot:
    broker_epoch: str
    rows: tuple[ImageRow, ...]

    @classmethod
    def parse(cls, data: dict[str, Any], started_at: float) -> ImagesSnapshot:
        try:
            if data["ok"] is not True or not isinstance(data["images"], list):
                raise ValueError(INVALID_IMAGE)
            if len(data["images"]) > MAX_IMAGES:
                raise ValueError(INVALID_IMAGE)
            rows = tuple(ImageRow.parse(row, started_at) for row in data["images"])
            if len({row.job_id for row in rows}) != len(rows):
                raise ValueError(INVALID_IMAGE)
            return cls(_text(data["broker_epoch"], r"[0-9a-f]{32}"), rows)
        except (KeyError, TypeError, ValueError, OverflowError):
            raise ValueError(INVALID_IMAGE) from None


def validate_png(encoded: Any, size: Any, sha256: str) -> bytes:
    """Check bounded RGB8 PNG structure, checksums and raster size before Tk sees it."""
    try:
        if type(size) is not int or not 1 <= size <= MAX_PNG_BYTES:
            raise ValueError(INVALID_IMAGE)
        if not isinstance(encoded, str) or len(encoded) > 4 * ((MAX_PNG_BYTES + 2) // 3):
            raise ValueError(INVALID_IMAGE)
        png = base64.b64decode(encoded, validate=True)
        if (
            len(png) != size
            or hashlib.sha256(png).hexdigest() != sha256
            or not png.startswith(b"\x89PNG\r\n\x1a\n")
        ):
            raise ValueError(INVALID_IMAGE)
        offset, count, finished = 8, 0, False
        compressed = bytearray()
        idat_seen, idat_ended = False, False
        while offset < len(png):
            count += 1
            if count > 32 or len(png) - offset < 12:
                raise ValueError(INVALID_IMAGE)
            length = struct.unpack_from(">I", png, offset)[0]
            end = offset + 12 + length
            if end > len(png):
                raise ValueError(INVALID_IMAGE)
            kind = png[offset + 4 : offset + 8]
            payload = png[offset + 8 : end - 4]
            checksum = struct.unpack_from(">I", png, end - 4)[0]
            if checksum != zlib.crc32(kind + payload):
                raise ValueError(INVALID_IMAGE)
            if count == 1:
                if kind != b"IHDR" or payload != struct.pack(
                    ">IIBBBBB", WIDTH, HEIGHT, 8, 2, 0, 0, 0
                ):
                    raise ValueError(INVALID_IMAGE)
            elif kind == b"IDAT":
                if idat_ended:
                    raise ValueError(INVALID_IMAGE)
                idat_seen = True
                compressed.extend(payload)
            elif kind == b"IEND":
                if payload or not idat_seen or end != len(png):
                    raise ValueError(INVALID_IMAGE)
                finished = True
            elif kind == b"tEXt":
                if b"\0" not in payload:
                    raise ValueError(INVALID_IMAGE)
                idat_ended = idat_seen
            else:
                raise ValueError(INVALID_IMAGE)
            offset = end
        if not finished:
            raise ValueError(INVALID_IMAGE)
        expected_size = HEIGHT * (1 + WIDTH * 3)
        decoder = zlib.decompressobj()
        raster = decoder.decompress(bytes(compressed), expected_size + 1)
        if (
            len(raster) != expected_size
            or not decoder.eof
            or decoder.unused_data
            or decoder.unconsumed_tail
            or any(raster[y * (1 + WIDTH * 3)] > 4 for y in range(HEIGHT))
        ):
            raise ValueError(INVALID_IMAGE)
        return png
    except (ValueError, TypeError, binascii.Error, struct.error, zlib.error, OverflowError):
        raise ValueError(INVALID_IMAGE) from None


@dataclass(frozen=True)
class ImagePreview:
    row: ImageRow
    png: bytes = field(repr=False)

    @classmethod
    def parse(cls, data: dict[str, Any], expected: ImageRow, started_at: float) -> ImagePreview:
        try:
            if data["ok"] is not True:
                raise ValueError(INVALID_IMAGE)
            metadata = data["metadata"]
            source = metadata["source"]
            row = ImageRow.parse({**data, "source": source}, started_at)
            synthetic = source == "synthetic_fixture"
            if (
                not row.same_image(expected)
                or row.saved is not expected.saved
                or data.get("source", source) != source
                or metadata["label"] != SOURCES[source]
                or metadata["is_device_capture"] is not (not synthetic)
                or metadata["mime_type"] != "image/png"
                or type(metadata["width"]) is not int
                or metadata["width"] != WIDTH
                or type(metadata["height"]) is not int
                or metadata["height"] != HEIGHT
                or metadata["sha256"] != row.sha256
                or metadata["capture_complete"] is not True
                or metadata["coherent_frame"] != "unknown"
                or metadata["lcd_transfer_ack"] != "unknown"
                or metadata["protocol"]
                != ("synthetic-image-v1" if synthetic else "pocketjs-legacy-capture-v1")
            ):
                raise ValueError(INVALID_IMAGE)
            png = validate_png(data["image_base64"], metadata["size_bytes"], row.sha256)
            # Never extend a list snapshot's deadline using a later response.
            row = replace(row, expires_at=min(expected.expires_at, row.expires_at))
            return cls(row, png)
        except (KeyError, TypeError, ValueError, OverflowError):
            raise ValueError(INVALID_IMAGE) from None
