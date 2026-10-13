"""Same-layout firmware-only snapshot receipts; data partitions are never targets."""

import hashlib
import json
from dataclasses import asdict
from typing import Any

from .flash_lifecycle import FlashFailure
from .flash_lkg_geometry import factory_layout
from .flash_manifest import FlashBundle, FlashSegment
from .flash_security import validate_build_security


def compatibility(
    manifest: dict[str, Any], bundle: FlashBundle, files: dict[str, bytes], capacity: int
) -> dict[str, Any]:
    layout = json.loads(files[manifest["flash_layout"]["name"]])
    security = json.loads(files[manifest["flash_security"]["name"]])
    validate_build_security(security)
    segments = {item.name: item for item in bundle.segments}
    roles = {}
    for role in ("bootloader", "partition_table", "app"):
        value = layout.get(role)
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
        boot.offset != 0 or ((boot.size + 4095) // 4096) * 4096 > table.offset
        or app.offset != geometry.app_offset
        or ((app.size + 4095) // 4096) * 4096 > geometry.app_bytes
    ):
        raise FlashFailure("lkg_firmware_range_not_allowed")
    protected = {key: value for key, value in security.items() if key.startswith(
        ("SECURE_", "FLASH_ENC", "BOOT_ROM_LOG", "BOOTLOADER_APP_", "ESP_SECURE_")
    )}
    return {
        **asdict(geometry), "table_offset": table.offset,
        "security_sha256": hashlib.sha256(json.dumps(protected, sort_keys=True).encode()).hexdigest(),
        "mode": bundle.flash_mode, "frequency": bundle.flash_freq, "size": bundle.flash_size,
    }


def same_layout(old: dict[str, Any], new: dict[str, Any]) -> None:
    if old != new:
        raise FlashFailure("lkg_partition_or_security_change_not_allowed")


def baseline_layout(backup: bytes, compat: dict[str, Any], table: bytes) -> None:
    start = compat["table_offset"]
    if backup[start:start + len(table)] != table:
        raise FlashFailure("lkg_initial_partition_change_not_allowed")


def bundle_record(bundle: FlashBundle) -> dict[str, Any]:
    return asdict(bundle)


def bundle_from_record(value: dict[str, Any]) -> FlashBundle:
    return FlashBundle(
        value["manifest_sha256"], value["layout_sha256"],
        tuple(FlashSegment(**item) for item in value["segments"]),
        value["flash_mode"], value["flash_size"], value["flash_freq"],
    )
