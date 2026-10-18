"""Bounded, offline decoding of PocketJS's legacy 240x135 diagnostic capture.

The pinned firmware 22f6d54 emits ``CAPTURE_BEGIN 240 135``, then ``PIX y``
and 240 four-digit, lowercase RGB565 values, then ``CAPTURE_END``. See
main/hal/board.c:129-132,419-439 and tools/serial_control.py:50-73 in
dj-oyu/cardputer-adv-pocketjs. These values are sampled before byte swapping
and LCD transfer: a complete frame is not proof of LCD acknowledgement or
snapshot coherence. This module never opens a port, executes a command, logs
input, or keeps unrelated device output. The caller owns timeouts and approval.
"""

import hashlib
import re
import struct
import zlib
from dataclasses import dataclass, field
from typing import Literal

WIDTH = 240
HEIGHT = 135
MAX_WIRE_BYTES = 256 * 1024
MAX_LINE_BYTES = 2048
_ROW = re.compile(rb"PIX (0|[1-9][0-9]{0,2}) ([0-9a-f]{960})")
_BEGIN = b"CAPTURE_BEGIN 240 135"


class CaptureError(ValueError):
    """A fixed category only; never include device bytes in an error."""


@dataclass(frozen=True)
class CapturedImage:
    png: bytes = field(repr=False)
    source: Literal["render_buffer", "synthetic_fixture"]
    width: int = WIDTH
    height: int = HEIGHT

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.png).hexdigest()

    def metadata(self) -> dict[str, object]:
        synthetic = self.source == "synthetic_fixture"
        return {
            "source": self.source,
            "label": "TEST / NOT DEVICE" if synthetic else "Device render buffer / LCD unverified",
            "is_device_capture": not synthetic,
            "mime_type": "image/png",
            "width": self.width,
            "height": self.height,
            "size_bytes": len(self.png),
            "sha256": self.sha256,
            "capture_complete": True,
            "coherent_frame": "unknown",
            "lcd_transfer_ack": "unknown",
            "protocol": "synthetic-image-v1" if synthetic else "pocketjs-legacy-capture-v1",
        }


def _chunk(kind: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + kind
        + payload
        + struct.pack(">I", zlib.crc32(kind + payload))
    )


def _png(rgb: bytes, description: str) -> bytes:
    if len(rgb) != WIDTH * HEIGHT * 3:
        raise CaptureError("capture_raster_size")
    stride = WIDTH * 3
    raster = b"".join(b"\0" + rgb[y * stride : (y + 1) * stride] for y in range(HEIGHT))
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", struct.pack(">IIBBBBB", WIDTH, HEIGHT, 8, 2, 0, 0, 0))
        + _chunk(b"tEXt", b"Description\0" + description.encode("ascii"))
        + _chunk(b"IDAT", zlib.compress(raster))
        + _chunk(b"IEND", b"")
    )


class LegacyCaptureDecoder:
    """Consume exactly one fresh capture, preserving arbitrary USB read splits.

    Rows must be unique and in order. Recognized but malformed capture records
    fail closed; unrelated lines are discarded. A failure is sticky. ``finish``
    closes an incomplete decoder too, so a timed-out capture cannot later be
    revived by bytes from another operation. A successful finish is idempotent.
    The caller must establish a fresh serial boundary before sending its one
    requested capture command; this legacy protocol carries no capture nonce.
    """

    def __init__(self) -> None:
        self._pending = bytearray()
        self._pixels = bytearray()
        self._bytes_read = 0
        self._rows = 0
        self._started = False
        self._ended = False
        self._failure: str | None = None
        self._image: CapturedImage | None = None

    @property
    def complete(self) -> bool:
        return self._ended and self._failure is None

    @property
    def received_bytes(self) -> int:
        return self._bytes_read

    @property
    def received_rows(self) -> int:
        return self._rows

    def _fail(self, code: str) -> None:
        self._failure = code
        self._pending.clear()
        self._pixels.clear()
        raise CaptureError(code)

    def feed(self, incoming: bytes) -> None:
        if self._failure is not None:
            raise CaptureError(self._failure)
        if self._ended or self._image is not None:
            self._fail("capture_closed")
        self._bytes_read += len(incoming)
        if self._bytes_read > MAX_WIRE_BYTES:
            self._fail("capture_wire_limit")
        at = 0
        while at < len(incoming):
            newline = incoming.find(b"\n", at)
            stop = newline if newline >= 0 else len(incoming)
            if len(self._pending) + stop - at > MAX_LINE_BYTES:
                self._fail("capture_line_limit")
            self._pending.extend(incoming[at:stop])
            if newline < 0:
                break
            line = bytes(self._pending).removesuffix(b"\r")
            self._pending.clear()
            self._line(line)
            at = newline + 1

    def _line(self, line: bytes) -> None:
        if line.startswith(b"CAPTURE_BEGIN"):
            if self._started or self._ended:
                self._fail("capture_multiple_frames")
            if line != _BEGIN:
                self._fail("capture_header_invalid")
            self._started = True
        elif line.startswith(b"PIX"):
            if not self._started or self._ended:
                self._fail("capture_row_outside_frame")
            match = _ROW.fullmatch(line)
            if match is None:
                self._fail("capture_row_invalid")
            assert match is not None
            row = int(match[1])
            if row >= HEIGHT:
                self._fail("capture_row_range")
            if row != self._rows:
                self._fail("capture_row_sequence")
            # Each four-digit token is the numeric RGB565 value before the
            # firmware's SPI byte swap, independent of host native endianness.
            self._pixels.extend(bytes.fromhex(match[2].decode("ascii")))
            self._rows += 1
        elif line.startswith(b"CAPTURE_END"):
            if not self._started or self._ended:
                self._fail("capture_end_outside_frame")
            if line != b"CAPTURE_END":
                self._fail("capture_end_invalid")
            if self._rows != HEIGHT:
                self._fail("capture_incomplete")
            self._ended = True
        elif line.startswith(b"CAPTURE_"):
            self._fail("capture_record_invalid")
        # Logs, including private text, never leave this method or get saved.

    def finish(self) -> CapturedImage:
        if self._failure is not None:
            raise CaptureError(self._failure)
        if self._image is not None:
            return self._image
        if not self._ended or self._rows != HEIGHT:
            self._fail("capture_incomplete")
        if self._pending.startswith((b"CAPTURE_", b"PIX")):
            self._fail("capture_trailing_record")
        rgb = bytearray(WIDTH * HEIGHT * 3)
        for index, (pixel,) in enumerate(struct.iter_unpack(">H", self._pixels)):
            at = index * 3
            rgb[at] = ((pixel >> 11) & 31) * 255 // 31
            rgb[at + 1] = ((pixel >> 5) & 63) * 255 // 63
            rgb[at + 2] = (pixel & 31) * 255 // 31
        self._image = CapturedImage(
            _png(bytes(rgb), "DEVICE RENDER BUFFER - LCD TRANSFER AND COHERENCE UNVERIFIED"),
            source="render_buffer",
        )
        self._pixels.clear()
        self._pending.clear()
        return self._image


# Small deterministic glyphs keep this transport fixture independent of fonts,
# GUI libraries, firmware, Pillow and real screenshots. Every image says what it is.
_FONT = {
    "T": (31, 4, 4, 4, 4, 4, 4),
    "E": (31, 16, 16, 30, 16, 16, 31),
    "S": (15, 16, 16, 14, 1, 1, 30),
    "N": (17, 25, 25, 21, 19, 19, 17),
    "O": (14, 17, 17, 17, 17, 17, 14),
    "D": (30, 17, 17, 17, 17, 17, 30),
    "V": (17, 17, 17, 17, 17, 10, 4),
    "I": (31, 4, 4, 4, 4, 4, 31),
    "C": (15, 16, 16, 16, 16, 16, 15),
    " ": (0, 0, 0, 0, 0, 0, 0),
}


def mock_image() -> CapturedImage:
    """Return a visibly labelled, deterministic synthetic transport fixture."""
    rgb = bytearray(WIDTH * HEIGHT * 3)
    for y in range(HEIGHT):
        for x in range(WIDTH):
            color = (24, 47, 62) if ((x // 16) + (y // 16)) % 2 else (36, 70, 86)
            if y < 6 or y >= HEIGHT - 6:
                color = (255, 187, 24)
            at = (y * WIDTH + x) * 3
            rgb[at : at + 3] = bytes(color)
    scale = 3
    for text, top in (("TEST", 37), ("NOT DEVICE", 76)):
        left = (WIDTH - (len(text) * 6 - 1) * scale) // 2
        for index, letter in enumerate(text):
            for row, mask in enumerate(_FONT[letter]):
                for col in range(5):
                    if mask & (1 << (4 - col)):
                        for dy in range(scale):
                            for dx in range(scale):
                                x = left + (index * 6 + col) * scale + dx
                                y = top + row * scale + dy
                                at = (y * WIDTH + x) * 3
                                rgb[at : at + 3] = b"\xff\xff\xff"
    return CapturedImage(
        _png(bytes(rgb), "SYNTHETIC TEST IMAGE - NOT A DEVICE CAPTURE"),
        source="synthetic_fixture",
    )
