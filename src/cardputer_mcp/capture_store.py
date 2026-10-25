"""Durable, owner-bound copies of completed captures under ``<state_dir>/captures/``.

Layout (schema_version 1), one directory per job:
``<job_id>/capture.png`` holds the exact PNG bytes the job delivered and
``<job_id>/capture.json`` holds ``{"schema_version":1,"job_id","principal_id",
"origin","mime_type","size_bytes","sha256","width","height","source",
"captured_utc"}``. Both files are written to a temporary name and replaced
atomically; the metadata is written last, so a directory without readable
metadata is never served. Reads re-verify size and sha256.

Saving never raises into the job. Retention is bounded by count and total
bytes; the oldest captures are evicted first and the one being saved stays.
"""

import hashlib
import json
import os
import stat
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .capture import CapturedImage

SCHEMA_VERSION = 1
MAX_CAPTURES = 200
MAX_TOTAL_BYTES = 64 * 1024 * 1024
MAX_IMAGE_BYTES = 200_000
_PNG = "capture.png"
_META = "capture.json"
_REPARSE = 0x400
_SOURCES = frozenset({"render_buffer", "synthetic_fixture"})


def _identifier(value: object) -> bool:
    return (
        type(value) is str
        and 1 <= len(value) <= 64
        and all(c.isascii() and (c.isalnum() or c in "_-") for c in value)
    )


def _linked(path: Path) -> bool:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & _REPARSE)


def _write(path: Path, data: bytes) -> None:
    temporary = path.with_name("." + path.name + ".tmp")
    flags = (
        os.O_CREAT
        | os.O_WRONLY
        | os.O_TRUNC
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = os.open(temporary, flags, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _read(path: Path, limit: int) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    with os.fdopen(descriptor, "rb") as handle:
        return handle.read(limit + 1)


class CaptureStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self._lock = threading.Lock()

    def _directory(self, job_id: str) -> Path:
        if not _identifier(job_id):
            raise ValueError("capture_identifier_invalid")
        if _linked(self.root):
            raise ValueError("capture_linked_path_not_supported")
        directory = self.root / job_id
        if _linked(directory):
            raise ValueError("capture_linked_path_not_supported")
        return directory

    def save(self, job_id: str, principal_id: str, origin: str, image: CapturedImage) -> bool:
        """Persist one completed capture; returns False instead of raising."""
        try:
            if (
                not principal_id
                or type(principal_id) is not str
                or image.source not in _SOURCES
                or not 0 < len(image.png) <= MAX_IMAGE_BYTES
            ):
                return False
            with self._lock:
                self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
                directory = self._directory(job_id)
                self.prune(keep={job_id}, reserve=len(image.png))
                directory.mkdir(mode=0o700, exist_ok=True)
                if _linked(directory / _PNG) or _linked(directory / _META):
                    return False
                metadata = {
                    "schema_version": SCHEMA_VERSION,
                    "job_id": job_id,
                    "principal_id": principal_id,
                    "origin": str(origin),
                    "mime_type": "image/png",
                    "size_bytes": len(image.png),
                    "sha256": image.sha256,
                    "width": image.width,
                    "height": image.height,
                    "source": image.source,
                    "captured_utc": datetime.now(UTC).isoformat(),
                }
                _write(directory / _PNG, image.png)
                _write(
                    directory / _META,
                    json.dumps(metadata, sort_keys=True, separators=(",", ":")).encode(),
                )
            return True
        except Exception:
            return False

    def load(self, job_id: str, principal_id: str) -> tuple[CapturedImage, dict[str, Any]] | None:
        """The owner's verified capture and its metadata, or None."""
        try:
            directory = self._directory(job_id)
            if _linked(directory / _PNG) or _linked(directory / _META):
                return None
            metadata = json.loads(_read(directory / _META, 4096))
            if (
                type(metadata) is not dict
                or metadata.get("schema_version") != SCHEMA_VERSION
                or metadata.get("job_id") != job_id
                or metadata.get("principal_id") != principal_id
                or metadata.get("mime_type") != "image/png"
                or metadata.get("source") not in _SOURCES
            ):
                return None
            png = _read(directory / _PNG, MAX_IMAGE_BYTES)
            if (
                len(png) != metadata.get("size_bytes")
                or len(png) > MAX_IMAGE_BYTES
                or hashlib.sha256(png).hexdigest() != metadata.get("sha256")
            ):
                return None
            image = CapturedImage(
                png, metadata["source"], int(metadata["width"]), int(metadata["height"])
            )
            return image, metadata
        except Exception:
            return None

    def prune(self, keep: set[str], reserve: int = 0) -> None:
        """Evict the oldest capture directories beyond the count/byte caps."""
        try:
            names = os.listdir(self.root)
        except FileNotFoundError:
            return
        entries: list[tuple[int, int, Path]] = []
        for name in names:
            if not _identifier(name):
                continue
            directory = self.root / name
            if _linked(directory) or not directory.is_dir():
                continue
            size, newest = 0, 0
            try:
                for child in directory.iterdir():
                    info = child.lstat()
                    if stat.S_ISREG(info.st_mode):
                        size += info.st_size
                        newest = max(newest, info.st_mtime_ns)
            except OSError:
                continue
            entries.append((newest, size, directory))
        count = len(entries) + (0 if any(item[2].name in keep for item in entries) else 1)
        total = sum(size for _, size, _ in entries) + reserve
        for _, size, directory in sorted(entries, key=lambda item: item[0]):
            if count <= MAX_CAPTURES and total <= MAX_TOTAL_BYTES:
                break
            if directory.name in keep:
                continue
            try:
                for child in directory.iterdir():
                    if not _linked(child):
                        child.unlink()
                directory.rmdir()
            except OSError:
                continue
            count -= 1
            total -= size
