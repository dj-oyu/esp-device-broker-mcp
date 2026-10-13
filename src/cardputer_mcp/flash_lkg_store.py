"""Private immutable content hashes, independently retained from build caches."""

import ctypes
import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

from .flash_backup import BackupStore, _not_link
from .flash_lifecycle import FlashFailure


class FirmwareSnapshots:
    def __init__(self, root: Path, *, windows_private_acl_confirmed: bool = False) -> None:
        self.root = BackupStore(
            root, windows_private_acl_confirmed=windows_private_acl_confirmed
        ).directory

    def _directory_barrier(self, directory: Path) -> None:
        if os.name == "posix":
            descriptor = os.open(directory, os.O_RDONLY | int(vars(os)["O_DIRECTORY"]))
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)

    def _publish(self, pending: Path, target: Path) -> None:
        if os.name == "nt":
            kernel = vars(ctypes)["WinDLL"]("kernel32", use_last_error=True)
            move = kernel.MoveFileExW
            move.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_ulong]
            move.restype = ctypes.c_int
            # MOVEFILE_WRITE_THROUGH, without REPLACE_EXISTING or cross-volume copy.
            if not move(str(pending), str(target), 0x8):
                raise FlashFailure("lkg_publication_failed")
        else:
            pending.rename(target)
            self._directory_barrier(self.root)
            self._directory_barrier(self.root.parent)

    def _path(self, digest: str) -> Path:
        if re.fullmatch(r"[a-f0-9]{64}", digest) is None:
            raise FlashFailure("lkg_receipt_invalid")
        return self.root / digest

    def put(self, data: bytes) -> str:
        digest = hashlib.sha256(data).hexdigest()
        path = self._path(digest)
        _not_link(self.root)
        if path.exists():
            if self.get(digest, len(data)) != data:
                raise FlashFailure("lkg_object_changed")
            return digest
        pending = self.root / (digest + ".pending")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(pending, flags, 0o600)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            # One broker owns the device/state lock; no overwrite/adoption of others.
            if path.exists():
                raise FlashFailure("lkg_object_conflict")
            self._publish(pending, path)
        except Exception:
            raise FlashFailure("lkg_persistence_failed_needs_review") from None
        finally:
            pending.unlink(missing_ok=True)
        return digest

    def get(self, digest: str, limit: int = 67108864) -> bytes:
        try:
            path = self._path(digest)
            _not_link(path)
            info = path.stat()
            if info.st_nlink != 1 or not 0 < info.st_size <= limit:
                raise ValueError
            flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(path, flags)
            with os.fdopen(descriptor, "rb") as handle:
                opened = os.fstat(handle.fileno())
                if (opened.st_dev, opened.st_ino, opened.st_size) != (
                    info.st_dev,
                    info.st_ino,
                    info.st_size,
                ):
                    raise ValueError
                data = handle.read(limit + 1)
            if len(data) > limit or hashlib.sha256(data).hexdigest() != digest:
                raise ValueError
            return data
        except Exception:
            raise FlashFailure("lkg_object_missing_or_changed") from None

    def stage(self, metadata: dict[str, Any], files: dict[str, bytes]) -> str:
        if (not files and metadata.get("kind") != "full_backup") or sum(
            map(len, files.values())
        ) > 100 * 1048576:
            raise FlashFailure("lkg_snapshot_size_invalid")
        receipts = {name: self.put(data) for name, data in files.items()}
        envelope = {"schema_version": 1, "metadata": metadata, "files": receipts}
        return self.put(json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode())

    def load(self, receipt: str) -> tuple[dict[str, Any], dict[str, bytes]]:
        try:
            value = json.loads(self.get(receipt, 1048576))
            if (
                set(value) != {"schema_version", "metadata", "files"}
                or value["schema_version"] != 1
            ):
                raise ValueError
            return value["metadata"], {
                name: self.get(digest) for name, digest in value["files"].items()
            }
        except Exception:
            raise FlashFailure("lkg_snapshot_missing_or_changed") from None
