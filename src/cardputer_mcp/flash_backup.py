"""Private broker-owned backup receipts; never deliver raw flash bytes to MCP."""

import hashlib
import json
import os
import re
import stat
from dataclasses import asdict, dataclass
from pathlib import Path

from .flash_lifecycle import BackupReceipt, FlashFailure


@dataclass(frozen=True)
class RetainedBackup:
    job_id: str
    principal: str
    device_sha256: str
    device_fingerprint: str
    flash_bytes: int
    sha256: str


def _not_link(path: Path) -> None:
    for candidate in (path, *path.parents):
        info = candidate.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise FlashFailure("backup_linked_path_not_supported")


class BackupStore:
    def __init__(self, directory: Path, *, windows_private_acl_confirmed: bool = False) -> None:
        if not directory.is_absolute() or ".." in directory.parts:
            raise FlashFailure("registered_private_backup_directory_required")
        if os.name == "nt" and windows_private_acl_confirmed is not True:
            raise FlashFailure("private_backup_acl_confirmation_required")
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        _not_link(directory)
        info = directory.stat()
        if os.name == "posix" and (
            info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700
        ):
            raise FlashFailure("private_backup_directory_required")
        self.directory = directory

    @staticmethod
    def _job(job_id: str) -> None:
        if re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", job_id) is None:
            raise FlashFailure("invalid_backup_job")

    def save(self, job_id: str, principal: str, receipt: BackupReceipt, data: bytes) -> RetainedBackup:
        self._job(job_id)
        if not principal or len(data) != receipt.flash_bytes or hashlib.sha256(data).hexdigest() != receipt.sha256:
            raise FlashFailure("backup_receipt_mismatch")
        retained = RetainedBackup(job_id, principal, receipt.device_sha256,
                                  receipt.device_fingerprint, receipt.flash_bytes, receipt.sha256)
        binary = self.directory / (job_id + ".bin")
        metadata = self.directory / (job_id + ".json")
        _not_link(self.directory)
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(binary, flags, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            encoded = json.dumps(asdict(retained), sort_keys=True).encode()
            descriptor = os.open(metadata, flags, 0o600)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
        except Exception:
            # Partial backup files remain inert; never create a success receipt.
            raise FlashFailure("backup_persistence_failed_needs_review") from None
        return retained

    def load(self, job_id: str, principal: str) -> tuple[RetainedBackup, bytes]:
        self._job(job_id)
        try:
            binary = self.directory / (job_id + ".bin")
            metadata = self.directory / (job_id + ".json")
            _not_link(binary)
            _not_link(metadata)
            if metadata.stat().st_size > 4096:
                raise ValueError
            record = RetainedBackup(**json.loads(metadata.read_text("utf-8")))
            if record.job_id != job_id or record.principal != principal:
                raise FlashFailure("backup_owner_mismatch")
            if type(record.flash_bytes) is not int or not 0 < record.flash_bytes <= 32 * 1024 * 1024:
                raise ValueError
            info = binary.stat()
            if info.st_nlink != 1 or info.st_size != record.flash_bytes:
                raise ValueError
            if os.name == "posix" and (info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600):
                raise ValueError
            flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(binary, flags)
            with os.fdopen(descriptor, "rb") as handle:
                opened = os.fstat(handle.fileno())
                if (opened.st_dev, opened.st_ino, opened.st_size) != (info.st_dev, info.st_ino, info.st_size):
                    raise ValueError
                data = handle.read(record.flash_bytes + 1)
                after = os.fstat(handle.fileno())
                if after.st_mtime_ns != opened.st_mtime_ns or after.st_size != opened.st_size:
                    raise ValueError
            if len(data) != record.flash_bytes or hashlib.sha256(data).hexdigest() != record.sha256:
                raise ValueError
            return record, data
        except FlashFailure:
            raise
        except Exception:
            raise FlashFailure("backup_missing_or_changed") from None
