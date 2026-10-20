"""Non-waiting OS lock, automatically released on process death."""

import hashlib
import importlib
import os
import stat
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

LOCK_NAME = ".source-cache.lock"


class CacheLock:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.local = threading.local()

    @contextmanager
    def hold(self, root: Path, state: Path, *, write: bool) -> Iterator[None]:
        if not self.lock.acquire(blocking=False):
            raise ValueError("source_fetch_busy")
        descriptor: int | None = None
        depth = getattr(self.local, "depth", 0)
        try:
            self.local.depth = depth + 1
            path = root / LOCK_NAME
            if depth == 0 and (write or path.exists()):
                try:
                    info = path.lstat()
                except FileNotFoundError:
                    pass
                else:
                    if (
                        not stat.S_ISREG(info.st_mode)
                        or info.st_nlink != 1
                        or getattr(info, "st_file_attributes", 0) & 0x400
                    ):
                        raise ValueError("source_linked_path_rejected")
                descriptor = os.open(
                    path,
                    os.O_RDWR
                    | (os.O_CREAT if write else 0)
                    | getattr(os, "O_NOFOLLOW", 0)
                    | getattr(os, "O_BINARY", 0),
                    0o600,
                )
                try:
                    if os.name == "nt":
                        importlib.import_module("msvcrt").locking(
                            descriptor, importlib.import_module("msvcrt").LK_NBLCK, 1
                        )
                    else:
                        module = importlib.import_module("fcntl")
                        module.flock(descriptor, module.LOCK_EX | module.LOCK_NB)
                except OSError:
                    raise ValueError("source_fetch_busy") from None
                owner = hashlib.sha256(os.path.normcase(str(state.resolve())).encode()).hexdigest()
                info = os.fstat(descriptor)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise ValueError("source_linked_path_rejected")
                if info.st_size == 0 and write:
                    os.write(descriptor, owner.encode("ascii"))
                    os.fsync(descriptor)
                elif info.st_size == 0:
                    pass  # Interrupted first claim: read-only preflight may inspect it.
                else:
                    if info.st_size != 64 or os.read(descriptor, 65) != owner.encode("ascii"):
                        raise ValueError("source_cache_owner_conflict")
            yield
        finally:
            # Closing the descriptor releases flock / Windows byte-range locks.
            if descriptor is not None:
                os.close(descriptor)
            self.local.depth = depth
            self.lock.release()
