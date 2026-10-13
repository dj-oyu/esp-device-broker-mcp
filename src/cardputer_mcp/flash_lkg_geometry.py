"""Strict same-layout firmware recovery fence; no device or storage operations."""

import hashlib
import struct
from dataclasses import dataclass

from .flash_lifecycle import FlashFailure


@dataclass(frozen=True)
class FactoryLayout:
    table_sha256: str
    app_offset: int
    app_bytes: int


def factory_layout(data: bytes, *, table_offset: int, flash_bytes: int) -> FactoryLayout:
    """Accept the narrow single-factory subset; unknown/OTA/encrypted layouts stop."""
    if (
        len(data) != 3072
        or table_offset < 4096
        or table_offset % 4096
        or flash_bytes not in (2097152, 4194304, 8388608, 16777216)
    ):
        raise FlashFailure("lkg_partition_layout_unsupported")
    rows = []
    app = None
    verified_md5 = False
    ended = False
    for position in range(0, len(data), 32):
        entry = data[position : position + 32]
        if entry == b"\xff" * 32:
            ended = True
            if any(byte != 255 for byte in data[position:]):
                raise FlashFailure("lkg_partition_layout_unsupported")
            break
        if entry[:16] == b"\xeb\xeb" + b"\xff" * 14:
            if verified_md5 or entry[16:] != hashlib.md5(data[:position]).digest():
                raise FlashFailure("lkg_partition_checksum_invalid")
            verified_md5 = True
            continue
        if verified_md5:
            raise FlashFailure("lkg_partition_layout_unsupported")
        magic, kind, subtype, offset, size, _, flags = struct.unpack("<2sBBLL16sL", entry)
        if (
            magic != b"\xaa\x50"
            or kind not in (0, 1)
            or flags != 0
            or offset < table_offset + 4096
            or offset % 4096
            or size == 0
            or size % 4096
            or offset + size > flash_bytes
            or (kind == 1 and subtype == 0)
        ):
            raise FlashFailure("lkg_partition_layout_unsupported")
        if kind == 0:
            if subtype != 0 or app is not None or offset % 65536:
                raise FlashFailure("lkg_partition_layout_unsupported")
            app = offset, size
        rows.append((offset, offset + size))
    if not ended or not verified_md5 or app is None:
        raise FlashFailure("lkg_partition_layout_unsupported")
    ordered = sorted(rows)
    if any(left[1] > right[0] for left, right in zip(ordered, ordered[1:])):
        raise FlashFailure("lkg_partition_overlap")
    return FactoryLayout(hashlib.sha256(data).hexdigest(), app[0], app[1])
