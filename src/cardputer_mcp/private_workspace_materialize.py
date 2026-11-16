"""POSIX staging for trusted plans; not a build sandbox or public MCP API.

The parent directory must already be broker-owned, protected from other writers,
non-symlinked through all ancestors, and on a local filesystem. This helper does
not establish these deployment properties. Windows requires a native adapter.
"""

import hashlib
import os
import shutil
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

from .private_input_contract import InputFile, _hash, _inventory
from .private_workspace_plan import WorkspacePlan


def _supported() -> bool:
    return (
        os.name == "posix"
        and hasattr(os, "O_NOFOLLOW")
        and os.open in os.supports_dir_fd
        and os.mkdir in os.supports_dir_fd
        and shutil.rmtree.avoids_symlink_attacks
    )


def _check_cancel(cancelled: Callable[[], bool]) -> None:
    if cancelled():
        raise ValueError("private_workspace_cancelled")


def _write(root_fd: int, path: str, data: bytes) -> None:
    parent = os.dup(root_fd)
    try:
        parts = path.split("/")
        for part in parts[:-1]:
            try:
                os.mkdir(part, mode=0o700, dir_fd=parent)
            except FileExistsError:
                pass
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            os.close(parent)
            parent = child
        fd = os.open(
            parts[-1],
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=parent,
        )
        with os.fdopen(fd, "wb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent)
        with os.fdopen(fd, "rb") as check:
            if hashlib.file_digest(check, "sha256").digest() != hashlib.sha256(data).digest():
                raise ValueError("private_workspace_write_mismatch")
    finally:
        os.close(parent)


@contextmanager
def materialize_workspace(
    plan: WorkspacePlan,
    *,
    protected_parent: Path,
    cancelled: Callable[[], bool] = lambda: False,
) -> Iterator[Path]:
    """Yield only complete staging, then remove it, also on build/cancel failure.

    Caller must authenticate receipts and prepare the plan in-process. A forged
    WorkspacePlan is not an authorization token. The yielded directory is mutable
    build scratch, never a persistent cache or a durable success receipt. Caller
    must stop/join every build process before leaving this context. No arbitrary
    command execution or automatic artifact publishing is provided here.
    """
    if not _supported():
        raise ValueError("private_workspace_platform_unsupported")
    _hash(plan.identity)
    for _, payload in plan.files:
        if type(payload) is not bytes:
            raise ValueError("private_workspace_bytes_required")
    _inventory(tuple(InputFile(p, len(b), hashlib.sha256(b).hexdigest()) for p, b in plan.files))
    _check_cancel(cancelled)
    stage: Path | None = None
    try:
        try:
            if not protected_parent.is_absolute() or protected_parent.is_symlink():
                raise ValueError("private_workspace_parent_invalid")
            stage = Path(tempfile.mkdtemp(prefix="private-work-", dir=protected_parent))
            root_fd = os.open(stage, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                for path, data in plan.files:
                    _check_cancel(cancelled)
                    _write(root_fd, path, data)
                _check_cancel(cancelled)
            finally:
                os.close(root_fd)
        except OSError:
            raise ValueError("private_workspace_io_failed") from None
        yield stage
    finally:
        if stage is not None:
            try:
                shutil.rmtree(stage)
            except OSError:
                raise ValueError("private_workspace_cleanup_failed") from None
