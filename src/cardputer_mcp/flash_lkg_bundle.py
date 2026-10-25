"""Same-layout firmware-only snapshot receipts; data partitions are never targets.

The strict same-layout recovery fence (``factory_layout``) performs no device or
storage operations.
"""

import hashlib
import json
import struct
from dataclasses import asdict, dataclass
from typing import Any

from .flash_lifecycle import FlashFailure
from .flash_manifest import FlashBundle, FlashSegment
from .flash_security import validate_build_security


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
    labels = set()
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
        magic, kind, subtype, offset, size, label, flags = struct.unpack("<2sBBLL16sL", entry)
        name = label.split(b"\x00", 1)[0]
        if not name or name in labels:
            raise FlashFailure("lkg_partition_label_invalid")
        labels.add(name)
        if (
            magic != b"\xaa\x50"
            or kind not in (0, 1)
            or (flags != 0 if kind == 0 else flags not in (0, 2))
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
    if any(left[1] > right[0] for left, right in zip(ordered, ordered[1:], strict=False)):
        raise FlashFailure("lkg_partition_overlap")
    return FactoryLayout(hashlib.sha256(data).hexdigest(), app[0], app[1])


def compatibility(
    manifest: dict[str, Any], bundle: FlashBundle, files: dict[str, bytes], capacity: int
) -> dict[str, Any]:
    layout = json.loads(files[manifest["flash_layout"]["name"]])
    security = json.loads(files[manifest["flash_security"]["name"]])
    validate_build_security(security)
    segments = {item.name: item for item in bundle.segments}
    roles = {}
    for role in ("bootloader", "partition_table", "app"):
        value = layout.get("partition-table") if role == "partition_table" else layout.get(role)
        if value is None and role == "partition_table":
            value = layout.get("partition_table")
        if not isinstance(value, dict) or value.get("file") not in segments:
            raise FlashFailure("lkg_firmware_roles_required")
        item = segments[value["file"]]
        if int(value["offset"], 0) != item.offset:
            raise FlashFailure("lkg_firmware_role_changed")
        roles[role] = item
    if len(segments) != 3 or len({item.name for item in roles.values()}) != 3:
        raise FlashFailure("lkg_data_or_unknown_write_not_allowed")
    boot, table, app = (roles[name] for name in ("bootloader", "partition_table", "app"))
    geometry = factory_layout(files[table.name], table_offset=table.offset, flash_bytes=capacity)
    if (
        boot.offset != 0
        or ((boot.size + 4095) // 4096) * 4096 > table.offset
        or app.offset != geometry.app_offset
        or ((app.size + 4095) // 4096) * 4096 > geometry.app_bytes
    ):
        raise FlashFailure("lkg_firmware_range_not_allowed")
    protected = {
        key: value
        for key, value in security.items()
        if key.startswith(
            ("SECURE_", "FLASH_ENC", "BOOT_ROM_LOG", "BOOTLOADER_APP_", "ESP_SECURE_")
        )
    }
    return {
        **asdict(geometry),
        "table_offset": table.offset,
        "security_sha256": hashlib.sha256(
            json.dumps(protected, sort_keys=True).encode()
        ).hexdigest(),
        "mode": bundle.flash_mode,
        "frequency": bundle.flash_freq,
        "size": bundle.flash_size,
    }


def same_layout(old: dict[str, Any], new: dict[str, Any]) -> None:
    if old != new:
        raise FlashFailure("lkg_partition_or_security_change_not_allowed")


def baseline_layout(backup: bytes, compat: dict[str, Any], table: bytes) -> None:
    start = compat["table_offset"]
    if backup[start : start + len(table)] != table:
        raise FlashFailure("lkg_initial_partition_change_not_allowed")


def bundle_record(bundle: FlashBundle) -> dict[str, Any]:
    return asdict(bundle)


def bundle_from_record(value: dict[str, Any]) -> FlashBundle:
    return FlashBundle(
        value["manifest_sha256"],
        value["layout_sha256"],
        tuple(FlashSegment(**item) for item in value["segments"]),
        value["flash_mode"],
        value["flash_size"],
        value["flash_freq"],
    )
