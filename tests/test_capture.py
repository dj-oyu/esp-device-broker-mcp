"""Pure host tests; no serial import, enumeration, real device or screenshot."""

import hashlib
import random
import struct
import zlib

import pytest

from cardputer_mcp.capture import (
    HEIGHT,
    MAX_LINE_BYTES,
    MAX_WIRE_BYTES,
    WIDTH,
    CaptureError,
    LegacyCaptureDecoder,
    mock_image,
)


def row(y: int, color: str = "f800") -> bytes:
    return f"PIX {y} {color * WIDTH}\n".encode()


def frame() -> bytes:
    colors = ("f800", "07e0", "001f", "ffff", "0000", "8410")
    return (
        b"CAPTURE_BEGIN 240 135\n"
        + b"".join(row(y, colors[y % len(colors)]) for y in range(HEIGHT))
        + b"CAPTURE_END\n"
    )


def png_parts(png: bytes) -> tuple[dict[bytes, bytes], bytes]:
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    parts = {}
    at = 8
    while at < len(png):
        length = struct.unpack(">I", png[at : at + 4])[0]
        kind = png[at + 4 : at + 8]
        data = png[at + 8 : at + 8 + length]
        crc = struct.unpack(">I", png[at + 8 + length : at + 12 + length])[0]
        assert crc == zlib.crc32(kind + data)
        parts[kind] = data
        at += length + 12
    assert at == len(png)
    assert parts[b"IEND"] == b""
    assert struct.unpack(">IIBBBBB", parts[b"IHDR"]) == (WIDTH, HEIGHT, 8, 2, 0, 0, 0)
    return parts, zlib.decompress(parts[b"IDAT"])


@pytest.mark.parametrize("size", [1, 4096, 200000])
def test_complete_image_with_arbitrary_fragmentation_and_big_coalesced_reads(size):
    wire = frame()
    decoder = LegacyCaptureDecoder()
    for at in range(0, len(wire), size):
        decoder.feed(wire[at : at + size])
    assert decoder.complete
    assert decoder.received_rows == HEIGHT
    assert decoder.received_bytes == len(wire)
    image = decoder.finish()
    assert decoder.finish() is image
    _, raster = png_parts(image.png)
    assert len(raster) == HEIGHT * (WIDTH * 3 + 1)
    expected = ((255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 255), (0, 0, 0))
    for y, color in enumerate(expected):
        start = y * (WIDTH * 3 + 1)
        assert raster[start] == 0
        assert raster[start + 1 : start + 1 + WIDTH * 3] == bytes(color) * WIDTH
    start = 5 * (WIDTH * 3 + 1)
    assert raster[start + 1 : start + 4] == bytes((131, 129, 131))
    metadata = image.metadata()
    assert metadata["capture_complete"] is True
    assert metadata["source"] == "render_buffer"
    assert metadata["coherent_frame"] == metadata["lcd_transfer_ack"] == "unknown"
    assert metadata["sha256"] == hashlib.sha256(image.png).hexdigest()


def test_noise_crlf_and_random_fragmentation_are_bounded_and_private():
    secret = b"SYNTHETIC_SECRET_DO_NOT_LOG"
    wire = secret + b"\n" + frame().replace(b"\n", b"\r\nnoise " + secret + b"\r\n")
    decoder = LegacyCaptureDecoder()
    rng = random.Random(42)
    at = 0
    while at < len(wire):
        size = rng.randint(1, 997)
        # END closes the decoder: trailing ordinary logs from the same read
        # are discarded; the owner stops feeding on complete.
        decoder.feed(wire[at : at + size])
        at += size
        if decoder.complete:
            break
    image = decoder.finish()
    assert secret not in image.png
    assert secret.decode() not in repr(image)
    assert secret.decode() not in str(image.metadata())
    assert len(decoder._pixels) == len(decoder._pending) == 0


@pytest.mark.parametrize(
    ("wire", "code"),
    [
        (b"CAPTURE_BEGIN 239 135\n", "capture_header_invalid"),
        (b"CAPTURE_BEGIN 240 135\n" * 2, "capture_multiple_frames"),
        (row(0), "capture_row_outside_frame"),
        (b"CAPTURE_BEGIN 240 135\n" + row(0) * 2, "capture_row_sequence"),
        (b"CAPTURE_BEGIN 240 135\n" + row(0, "F800"), "capture_row_invalid"),
        (frame().replace(b"CAPTURE_END\n", b"CAPTURE_END garbage\n"), "capture_end_invalid"),
        (b"CAPTURE_OTHER\n", "capture_record_invalid"),
    ],
    ids=[
        "wrong-width",
        "duplicate-header",
        "row-before-header",
        "duplicate-row",
        "uppercase-pixels",
        "invalid-end",
        "unknown-record",
    ],
)
def test_corrupt_or_mixed_capture_records_fail_closed_and_never_recover(wire, code):
    decoder = LegacyCaptureDecoder()
    with pytest.raises(CaptureError, match=f"^{code}$"):
        decoder.feed(wire)
    assert not decoder.complete
    assert not decoder._pixels
    for operation in (lambda: decoder.feed(frame()), decoder.finish):
        with pytest.raises(CaptureError, match=f"^{code}$"):
            operation()


@pytest.mark.parametrize(
    "wire",
    [b"CAPTURE_BEGIN 240 135\n", frame()[:-12]],
    ids=["header-only", "missing-end"],
)
def test_finish_on_timeout_or_truncated_end_poisoned_decoder(wire):
    decoder = LegacyCaptureDecoder()
    decoder.feed(wire)
    with pytest.raises(CaptureError, match="^capture_incomplete$"):
        decoder.finish()
    with pytest.raises(CaptureError, match="^capture_incomplete$"):
        decoder.feed(frame())
    assert not decoder.complete


def test_total_wire_limit_counts_noise_and_fails_before_accepting_frame():
    decoder = LegacyCaptureDecoder()
    decoder.feed(b"\n" * MAX_WIRE_BYTES)
    with pytest.raises(CaptureError, match="^capture_wire_limit$"):
        decoder.feed(frame())
    assert not decoder.complete
    line = LegacyCaptureDecoder()
    line.feed(b"x" * MAX_LINE_BYTES)
    with pytest.raises(CaptureError, match="^capture_line_limit$"):
        line.feed(b"SYNTHETIC_SECRET")
    assert not line._pending


def test_partial_unrelated_log_after_end_is_not_part_of_the_image():
    decoder = LegacyCaptureDecoder()
    decoder.feed(frame() + b"unrelated SYNTHETIC_SECRET")
    image = decoder.finish()
    assert b"SYNTHETIC_SECRET" not in image.png
    assert not decoder._pending


def test_synthetic_image_is_deterministic_and_visibly_labeled():
    image = mock_image()
    assert mock_image().png == image.png
    metadata = image.metadata()
    assert metadata["source"] == "synthetic_fixture"
    assert metadata["is_device_capture"] is False
    assert metadata["label"] == "TEST / NOT DEVICE"
    assert metadata["lcd_transfer_ack"] == metadata["coherent_frame"] == "unknown"
    parts, raster = png_parts(image.png)
    assert b"NOT A DEVICE CAPTURE" in parts[b"tEXt"]
    # TEST's first T and NOT DEVICE's first N are real white raster glyphs,
    # not merely a description attached to an otherwise ambiguous image.
    stride = WIDTH * 3 + 1
    for x, y in ((85, 37), (31, 76)):
        at = y * stride + 1 + x * 3
        assert raster[at : at + 3] == b"\xff\xff\xff"
    assert len(image.png) < 8 * 1024
