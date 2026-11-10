"""One fixed source-bound Kconfig selector; never accept arbitrary build arguments."""

import hashlib
import os
import stat
from pathlib import Path
from typing import Any

MODE = "native_diagnostic_mode"
SYMBOL = "CONFIG_KSN_DEVICE_PROBE"
LIMIT = 1024 * 1024
ERROR = "native_diagnostic_config_invalid"


def mode(value: Any) -> str:
    if type(value) is not str or value not in ("ON", "OFF"):
        raise ValueError(ERROR)
    return value


def kwargs(plan: dict[str, Any]) -> dict[str, str]:
    return {MODE: mode(plan[MODE])} if MODE in plan else {}


def _read(path: Path) -> bytes:
    try:
        for part in (path, *path.parents):
            info = part.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise ValueError(ERROR)
        descriptor = os.open(
            path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
        )
        with os.fdopen(descriptor, "rb") as source:
            info = os.fstat(source.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or not 0 < info.st_size <= LIMIT
            ):
                raise ValueError(ERROR)
            raw = source.read(LIMIT + 1)
            if len(raw) != info.st_size:
                raise ValueError(ERROR)
            return raw
    except OSError:
        raise ValueError(ERROR) from None


def verify(root: Path, selected: str) -> dict[str, Any]:
    """Bind the actual post-IDF sdkconfig, including OFF presence, not only intent."""
    selected = mode(selected)
    raw = _read(root / "sdkconfig")
    try:
        lines = raw.decode("utf-8").splitlines()
    except UnicodeError:
        raise ValueError(ERROR) from None
    expected = SYMBOL + "=y" if selected == "ON" else "# " + SYMBOL + " is not set"
    declarations = [
        s for s in lines if s.startswith(SYMBOL + "=") or s == "# " + SYMBOL + " is not set"
    ]
    if declarations != [expected]:
        raise ValueError(ERROR)
    return {"name": "sdkconfig", "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}


def _seed(root: Path, selected: str) -> None:
    """Seed only this isolated output's config; a reused conflicting config is refused."""
    selected = mode(selected)
    path = root / "sdkconfig"
    if path.exists() or path.is_symlink():
        verify(root, selected)
        return
    # claim_build_directory has verified the directory; still refuse link ancestors.
    for part in (root, *root.parents):
        info = part.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError(ERROR)
    raw = (SYMBOL + "=y\n" if selected == "ON" else "# " + SYMBOL + " is not set\n").encode()
    descriptor = os.open(
        path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600
    )
    with os.fdopen(descriptor, "wb") as output:
        output.write(raw)
        output.flush()
        os.fsync(output.fileno())


def seed(root: Path, selected: str) -> None:
    try:
        _seed(root, selected)
    except OSError:
        raise ValueError(ERROR) from None
