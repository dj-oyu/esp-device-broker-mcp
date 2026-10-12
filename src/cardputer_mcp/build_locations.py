"""Private build slots. Full identities are verified, never truncated for path length."""

import hashlib
import json
import os
import re
import shutil
import stat
from pathlib import Path
from typing import Any

MAX_WINDOWS_BUILD_DIRECTORY = 200


class BuildLocationError(ValueError):
    """Fixed public codes; never expose registered output paths."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _digest(value: Any) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[a-f0-9]{64}", value) is None:
        raise BuildLocationError("build_output_identity_invalid")
    return value


def build_slot(fingerprint: str, environment_fingerprint: str) -> str:
    payload = {
        "domain": "esp-device-broker-mcp/build-slot/v2",
        "fingerprint": _digest(fingerprint),
        "environment_fingerprint": _digest(environment_fingerprint),
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def output_base(state_dir: Path, build_root: Path | None) -> Path:
    chosen = build_root if build_root is not None else state_dir / "firmware-builds"
    for component in (chosen, *chosen.parents):
        _plain(component)
    if chosen.exists() and not chosen.is_dir():
        raise BuildLocationError("build_output_root_invalid")
    return chosen.resolve()


def build_directory(
    state_dir: Path, build_root: Path | None, fingerprint: str, environment_fingerprint: str
) -> Path:
    base = output_base(state_dir, build_root)
    if build_root is not None:
        return base / build_slot(fingerprint, environment_fingerprint)
    return base / _digest(fingerprint) / _digest(environment_fingerprint)


def check_windows_path(path: Path, *, windows: bool | None = None) -> None:
    if windows is None:
        windows = os.name == "nt"
    if windows and len(str(path).encode("utf-16-le")) // 2 > MAX_WINDOWS_BUILD_DIRECTORY:
        raise BuildLocationError("build_output_path_too_long")


def build_location(
    fingerprint: str, environment_fingerprint: str, build_root: Path | None
) -> dict[str, Any]:
    if build_root is None:
        return {"layout_version": 1}
    return {"layout_version": 2, "slot": build_slot(fingerprint, environment_fingerprint)}


def _plain(path: Path) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
        raise BuildLocationError("build_output_linked")


def _json(path: Path) -> dict[str, Any]:
    _plain(path)
    if not path.is_file() or not 0 < path.stat().st_size <= 65536:
        raise BuildLocationError("build_output_identity_mismatch")
    with path.open("rb") as handle:
        raw = handle.read(65537)
    if len(raw) > 65536:
        raise BuildLocationError("build_output_identity_mismatch")

    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise BuildLocationError("build_output_identity_mismatch")
            result[key] = value
        return result

    try:
        value = json.loads(raw, object_pairs_hook=unique)
    except (UnicodeError, json.JSONDecodeError, RecursionError):
        raise BuildLocationError("build_output_identity_mismatch") from None
    if not isinstance(value, dict):
        raise BuildLocationError("build_output_identity_mismatch")
    return value


def identity_receipt(
    state_dir: Path, build_root: Path | None, fingerprint: str, environment_fingerprint: str
) -> dict[str, Any]:
    return {
        "receipt_version": 1,
        "location": build_location(fingerprint, environment_fingerprint, build_root),
        "fingerprint": _digest(fingerprint),
        "environment_fingerprint": _digest(environment_fingerprint),
        "root_id": hashlib.sha256(
            str(output_base(state_dir, build_root)).casefold().encode()
        ).hexdigest(),
    }


def claim_build_directory(
    state_dir: Path, build_root: Path | None, fingerprint: str, environment_fingerprint: str
) -> Path:
    """Caller must hold the corresponding slot lock through build and hashing."""
    destination = build_directory(state_dir, build_root, fingerprint, environment_fingerprint)
    check_windows_path(destination)
    _plain(destination.parent)
    _plain(destination)
    expected = identity_receipt(state_dir, build_root, fingerprint, environment_fingerprint)
    receipt = destination / "broker-build-identity.json"
    if destination.exists():
        if not receipt.is_file() or _json(receipt) != expected:
            raise BuildLocationError("build_output_identity_mismatch")
    else:
        destination.mkdir(parents=True, mode=0o700)
        with receipt.open("x", encoding="utf-8") as handle:
            json.dump(expected, handle, sort_keys=True)
    return destination


def storage_snapshot(
    state_dir: Path,
    build_root: Path | None,
    *,
    min_free_bytes: int = 512 * 1024 * 1024,
    max_files: int = 10000,
) -> dict[str, Any]:
    """Read-only bounded owned-slot inventory. Never cleans or adopts other data."""
    base = output_base(state_dir, build_root)
    existing = base
    while not existing.exists() and existing.parent != existing:
        existing = existing.parent
    try:
        disk = shutil.disk_usage(existing)
    except OSError:
        raise BuildLocationError("build_output_storage_unavailable") from None
    slots: list[Path] = []
    scanned = 0
    scan_complete = True
    if base.is_dir():
        for first in base.iterdir():
            scanned += 1
            if scanned > 2048:
                scan_complete = False
                break
            if (
                not re.fullmatch(r"[a-f0-9]{64}", first.name)
                or first.is_symlink()
                or not first.is_dir()
            ):
                continue
            try:
                _plain(first)
            except (BuildLocationError, OSError):
                continue
            candidates = iter([first]) if build_root is not None else first.iterdir()
            for slot in candidates:
                scanned += 1
                if scanned > 2048:
                    scan_complete = False
                    break
                receipt = slot / "broker-build-identity.json"
                if not receipt.is_file():
                    continue
                try:
                    _plain(slot)
                    value = _json(receipt)
                    fp = _digest(value.get("fingerprint"))
                    env = _digest(value.get("environment_fingerprint"))
                    if value != identity_receipt(state_dir, build_root, fp, env):
                        continue
                    if slot != build_directory(state_dir, build_root, fp, env):
                        continue
                except (BuildLocationError, OSError):
                    continue
                slots.append(slot)
                if len(slots) >= 256:
                    break
            if len(slots) >= 256 or not scan_complete:
                break
    used = files = entries = 0
    complete = len(slots) < 256 and scan_complete
    def walk_failed(error: OSError) -> None:
        nonlocal complete
        complete = False

    for slot in slots:
        for directory, directories, names in os.walk(slot, followlinks=False, onerror=walk_failed):
            entries += 1 + len(directories)
            if entries > max_files:
                complete = False
                break
            safe = []
            for name in directories:
                child = Path(directory) / name
                try:
                    _plain(child)
                except (BuildLocationError, OSError):
                    complete = False
                    continue
                safe.append(name)
            directories[:] = safe
            for name in names:
                files += 1
                if files > max_files:
                    complete = False
                    break
                path = Path(directory) / name
                try:
                    _plain(path)
                    info = path.lstat()
                    if stat.S_ISREG(info.st_mode):
                        used += info.st_size
                except (BuildLocationError, OSError):
                    complete = False
            if files > max_files:
                break
        if files > max_files or entries > max_files:
            break
    return {
        "free_bytes": disk.free,
        "total_bytes": disk.total,
        "owned_bytes": used,
        "owned_slots": len(slots),
        "usage_complete": complete,
        "min_free_bytes": min_free_bytes,
        "cleanup_enabled": False,
    }


def check_storage_before_write(
    state_dir: Path, build_root: Path | None, min_free_bytes: int = 512 * 1024 * 1024
) -> dict[str, Any]:
    value = storage_snapshot(state_dir, build_root, min_free_bytes=min_free_bytes)
    if value["free_bytes"] < min_free_bytes:
        raise BuildLocationError("build_output_insufficient_space")
    return value


def slot_lock_path(
    state_dir: Path, build_root: Path | None, fingerprint: str, environment_fingerprint: str
) -> Path:
    locks = output_base(state_dir, build_root) / ".locks"
    _plain(locks)
    return locks / (build_slot(fingerprint, environment_fingerprint) + ".lock")


def resolve_build_manifest(
    state_dir: Path, build_root: Path | None, manifest: dict[str, Any]
) -> Path:
    """Internal locator only. Flash still requires artifact hashes and separate approval.

    Old schema-v1 manifests without a build_location use the original two-hash
    layout. Compact manifests never silently fall back to another output root.
    """
    if type(manifest.get("schema_version")) is not int or manifest["schema_version"] != 1:
        raise BuildLocationError("build_output_identity_invalid")
    fingerprint = _digest(manifest.get("fingerprint"))
    environment = _digest(manifest.get("environment_fingerprint"))
    location = manifest.get("build_location", {"layout_version": 1})
    if not isinstance(location, dict) or type(location.get("layout_version")) is not int:
        raise BuildLocationError("build_output_identity_invalid")
    if location == {"layout_version": 1}:
        root = build_directory(state_dir, None, fingerprint, environment)
        _plain(root.parent)
        _plain(root)
        saved = _json(root / "broker-manifest.json")
        if (
            type(saved.get("schema_version")) is not int
            or saved.get("schema_version") != 1
            or saved.get("fingerprint") != fingerprint
            or saved.get("environment_fingerprint") != environment
            or saved != manifest
        ):
            raise BuildLocationError("build_output_identity_mismatch")
        return root
    if build_root is None or location != build_location(fingerprint, environment, build_root):
        raise BuildLocationError("build_output_identity_mismatch")
    root = build_directory(state_dir, build_root, fingerprint, environment)
    _plain(root)
    expected = identity_receipt(state_dir, build_root, fingerprint, environment)
    if _json(root / "broker-build-identity.json") != expected:
        raise BuildLocationError("build_output_identity_mismatch")
    saved = _json(root / "broker-manifest.json")
    if saved != manifest:
        raise BuildLocationError("build_output_identity_mismatch")
    return root
