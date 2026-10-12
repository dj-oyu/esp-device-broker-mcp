"""Offline flash admission contract. Never opens USB or executes a command."""

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, NoReturn


class FlashAdmissionError(ValueError):
    """Fixed public diagnostic, with no paths, native output or credentials."""


@dataclass(frozen=True)
class FlashSegment:
    offset: int
    name: str
    size: int
    sha256: str


@dataclass(frozen=True)
class FlashBundle:
    manifest_sha256: str
    layout_sha256: str
    segments: tuple[FlashSegment, ...]
    flash_mode: str
    flash_size: str
    flash_freq: str


def _fail(code: str) -> NoReturn:
    raise FlashAdmissionError(code)


def _digest(value: Any) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[a-f0-9]{64}", value) is None:
        _fail("invalid_flash_receipt")
    return value


def _file(root: Path, receipt: dict[str, Any], limit: int) -> bytes:
    name = receipt.get("name")
    if not isinstance(name, str) or "\\" in name or ":" in name:
        _fail("unsafe_flash_artifact")
    relative = PurePosixPath(name)
    if relative.is_absolute() or any(x in ("", ".", "..") for x in name.split("/")):
        _fail("unsafe_flash_artifact")
    path = root
    for part in relative.parts:
        path = path / part
        if path.is_symlink():
            _fail("linked_flash_artifact")
    if not path.resolve().is_relative_to(root.resolve()) or not path.is_file():
        _fail("flash_artifact_missing")
    size = receipt.get("bytes")
    if type(size) is not int or not 0 < size <= limit or path.stat().st_size != size:
        _fail("flash_artifact_size_mismatch")
    with path.open("rb") as handle:
        data = handle.read(limit + 1)
    if len(data) != size or hashlib.sha256(data).hexdigest() != _digest(receipt.get("sha256")):
        _fail("flash_artifact_hash_mismatch")
    return data


def _unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            _fail("duplicate_flash_layout_key")
        result[key] = value
    return result


def verify_bundle(root: Path, manifest: dict[str, Any], *, flash_bytes: int) -> FlashBundle:
    """Consume only a broker-admitted success manifest and local build directory.

    This is not a public MCP parser. Callers must establish build principal/job
    authority and device identity/security separately, then obtain flash approval.
    A build manifest never grants flash permission. Legacy layouts fail closed.
    """
    if (
        type(manifest.get("schema_version")) is not int
        or manifest.get("schema_version") != 1
        or manifest.get("idf_version") != "6.0.1"
        or manifest.get("target") != "esp32s3"
        or manifest.get("flash_authorized") is not False
    ):
        _fail("build_manifest_not_flash_eligible")
    _digest(manifest.get("fingerprint"))
    _digest(manifest.get("environment_fingerprint"))
    if type(flash_bytes) is not int or not 0 < flash_bytes <= 32 * 1024 * 1024:
        _fail("verified_flash_capacity_required")
    receipt = manifest.get("flash_layout")
    if not isinstance(receipt, dict) or receipt.get("name") != "flasher_args.json":
        _fail("build_flash_layout_receipt_required")
    raw = _file(root, receipt, 65536)
    try:
        layout = json.loads(raw, object_pairs_hook=_unique)
    except (UnicodeError, json.JSONDecodeError):
        _fail("invalid_flash_layout")
    if not isinstance(layout, dict):
        _fail("invalid_flash_layout")
    extra = layout.get("extra_esptool_args")
    if not isinstance(extra, dict) or extra.get("chip") != "esp32s3":
        _fail("flash_layout_target_mismatch")
    settings = layout.get("flash_settings")
    if not isinstance(settings, dict) or (
        settings.get("flash_mode") not in ("dio", "dout", "qio", "qout")
        or settings.get("flash_size") not in ("2MB", "4MB", "8MB", "16MB", "32MB")
        or settings.get("flash_freq") not in ("20m", "26m", "40m", "80m")
    ):
        _fail("unsupported_flash_settings")
    if int(settings["flash_size"][:-2]) * 1024 * 1024 != flash_bytes:
        _fail("flash_capacity_mismatch")
    # Never replay generated argv; build a separate fixed command at dispatch.
    expected_args = [
        "--flash-mode",
        settings["flash_mode"],
        "--flash-size",
        settings["flash_size"],
        "--flash-freq",
        settings["flash_freq"],
    ]
    if layout.get("write_flash_args") != expected_args:
        _fail("unsupported_flash_arguments")
    files = layout.get("flash_files")
    artifacts = manifest.get("artifacts")
    if not isinstance(files, dict) or not 1 <= len(files) <= 16 or not isinstance(artifacts, list):
        _fail("invalid_flash_artifact_map")
    indexed: dict[str, dict[str, Any]] = {}
    for artifact in artifacts:
        if (
            not isinstance(artifact, dict)
            or not isinstance(artifact.get("name"), str)
            or artifact["name"] in indexed
        ):
            _fail("invalid_flash_artifact_map")
        indexed[artifact["name"]] = artifact
    segments = []
    for offset, name in files.items():
        if not isinstance(offset, str) or re.fullmatch(r"0x[0-9a-fA-F]{1,8}", offset) is None:
            _fail("invalid_flash_offset")
        if not isinstance(name, str) or not name.endswith(".bin") or name not in indexed:
            _fail("unattested_flash_artifact")
        artifact = indexed[name]
        data = _file(root, artifact, 32 * 1024 * 1024)
        start = int(offset, 16)
        if start % 4096 or start + len(data) > flash_bytes:
            _fail("unsafe_flash_range")
        segments.append(FlashSegment(start, name, len(data), artifact["sha256"]))
    segments.sort(key=lambda segment: segment.offset)
    for left, right in zip(segments, segments[1:], strict=False):
        if ((left.offset + left.size + 4095) // 4096) * 4096 > right.offset:
            _fail("overlapping_flash_erase_sectors")
    for value in layout.values():
        if (
            isinstance(value, dict)
            and "encrypted" in value
            and value["encrypted"] not in (False, "false")
        ):
            _fail("encrypted_flash_not_authorized")
    canonical = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    return FlashBundle(
        hashlib.sha256(canonical).hexdigest(),
        receipt["sha256"],
        tuple(segments),
        settings["flash_mode"],
        settings["flash_size"],
        settings["flash_freq"],
    )
