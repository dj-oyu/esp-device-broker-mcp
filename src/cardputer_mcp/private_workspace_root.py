"""Create disposable POSIX private build roots without changing existing modes.

For local POSIX filesystems and a dedicated broker identity only. Same-identity
hostile processes and privileged administrators are outside this boundary.
Windows and network-filesystem guarantees require separate deployment adapters.
"""

import os
import secrets
import shutil
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from .private_workspace_materialize import _supported


def _check_directory(info: os.stat_result, *, private: bool = False) -> None:
    if not stat.S_ISDIR(info.st_mode):
        raise ValueError("private_root_not_directory")
    if info.st_uid not in ({os.geteuid()} if private else {0, os.geteuid()}):
        raise ValueError("private_root_wrong_owner")
    if info.st_mode & (0o077 if private else 0o022):
        raise ValueError("private_root_unsafe_mode")


def _open_verified_parent(path: Path) -> int:
    if not _supported():
        raise ValueError("private_root_platform_unsupported")
    if not path.is_absolute() or ".." in path.parts or path.anchor != "/":
        raise ValueError("private_root_invalid_path")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = os.open("/", flags)
    try:
        _check_directory(os.fstat(fd))
        for part in path.parts[1:]:
            child = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = child
            _check_directory(os.fstat(fd))
        return fd
    except BaseException:
        os.close(fd)
        raise


@contextmanager
def private_workspace_root(container: Path) -> Iterator[Path]:
    """Reserve owned mode-0700 scratch under a verified ancestry.

    container is a trusted broker deployment value, never a remote request path.
    Every ancestor must be root/current-UID owned and not group/world writable;
    even sticky writable ancestors are rejected. Existing modes are never fixed
    automatically. Callers must join all owned child processes before exit.
    """
    parent_fd: int | None = None
    name: str | None = None
    try:
        try:
            parent_fd = _open_verified_parent(container)
            candidate = "private-root-" + secrets.token_hex(16)
            os.mkdir(candidate, mode=0o700, dir_fd=parent_fd)
            name = candidate
            child = os.open(
                candidate, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd
            )
            try:
                _check_directory(os.fstat(child), private=True)
            finally:
                os.close(child)
        except OSError:
            raise ValueError("private_root_io_failed") from None
        yield container / name
    finally:
        try:
            if parent_fd is not None and name is not None:
                try:
                    shutil.rmtree(name, dir_fd=parent_fd)
                except OSError:
                    raise ValueError("private_root_cleanup_failed") from None
        finally:
            if parent_fd is not None:
                os.close(parent_fd)
