"""Source-bound build variant: one named Kconfig fragment inside the verified source.

The caller supplies only a short name. The fragment is the file
``sdkconfig.variant.<name>`` at the root of the already receipt-verified source
snapshot, so its bytes are covered by the source receipt. The broker binds the
name and the fragment SHA-256 into the plan, seeds the isolated build output's
``sdkconfig`` and, after IDF ran, reads the generated ``sdkconfig`` back. No
caller text, fragment text or path reaches a public diagnostic.
"""

import hashlib
import os
import re
import stat
from pathlib import Path
from typing import Any

NAME_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,24}")
FILE_PREFIX = "sdkconfig.variant."
MAX_FRAGMENT_BYTES = 4096
MAX_FRAGMENT_LINES = 128
MAX_SYMBOLS = 16
MAX_CONFIG_BYTES = 4 * 1024 * 1024
# A variant switches features. It never changes layout, security or target.
REJECTED_PREFIXES = (
    "CONFIG_SECURE_",
    "CONFIG_BOOTLOADER_",
    "CONFIG_PARTITION_TABLE_",
    "CONFIG_ESPTOOLPY_",
    "CONFIG_EFUSE_",
    "CONFIG_IDF_TARGET",
    "CONFIG_FLASH_ENCRYPTION_",
    "CONFIG_APP_ROLLBACK_",
    "CONFIG_APP_ANTI_ROLLBACK",
)
INVALID = "build_variant_invalid"
NOT_FOUND = "build_variant_not_found"
REQUIRES_SOURCE = "build_variant_requires_source_build"
CONFLICT = "build_variant_config_conflict"
NOT_APPLIED = "build_variant_not_applied"

_ASSIGNMENT = re.compile(r"(CONFIG_[A-Z0-9_]{1,64})=(y|n|-?[0-9]{1,10}|0x[0-9a-f]{1,8})")
_NOT_SET = re.compile(r"# (CONFIG_[A-Z0-9_]{1,64}) is not set")
_COMMENT = re.compile(r"# [ -~]{0,100}")
_HEX64 = re.compile(r"[a-f0-9]{64}")


def name_ok(value: Any) -> bool:
    return type(value) is str and NAME_PATTERN.fullmatch(value) is not None


def digest_ok(value: Any) -> bool:
    return type(value) is str and _HEX64.fullmatch(value) is not None


def plan_kwargs(plan: dict[str, Any]) -> dict[str, str]:
    """Keyword arguments that re-prepare a stored plan; empty without a variant."""
    if "build_variant" not in plan:
        return {}
    if not name_ok(plan["build_variant"]):
        raise ValueError(INVALID)
    return {"variant": plan["build_variant"]}


def identity(record: dict[str, Any]) -> tuple[str, str] | None:
    """`(name, fragment sha256)` of a plan or manifest, None without a variant.

    Exactly both keys or neither: a half-bound record never validates.
    """
    has = ("build_variant" in record, "build_variant_sha256" in record)
    if has == (False, False):
        return None
    if has != (True, True) or not name_ok(record["build_variant"]):
        raise ValueError(INVALID)
    if not digest_ok(record["build_variant_sha256"]):
        raise ValueError(INVALID)
    return record["build_variant"], record["build_variant_sha256"]


def _read_regular(path: Path, limit: int, code: str) -> bytes:
    """No-follow, regular-file, single-link, size-bounded read."""
    try:
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError(code)
        descriptor = os.open(
            path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
        )
        with os.fdopen(descriptor, "rb") as source:
            opened = os.fstat(source.fileno())
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_nlink != 1
                or not 0 < opened.st_size <= limit
            ):
                raise ValueError(code)
            raw = source.read(limit + 1)
            if len(raw) != opened.st_size:
                raise ValueError(code)
            return raw
    except OSError:
        raise ValueError(code) from None


def parse(raw: bytes) -> dict[str, str]:
    """Strict grammar; symbol -> y|n|integer. Anything else is invalid."""
    if not 0 < len(raw) <= MAX_FRAGMENT_BYTES or not raw.endswith(b"\n"):
        raise ValueError(INVALID)
    try:
        text = raw.decode("ascii")
    except UnicodeError:
        raise ValueError(INVALID) from None
    lines = text.split("\n")[:-1]
    if len(lines) > MAX_FRAGMENT_LINES:
        raise ValueError(INVALID)
    symbols: dict[str, str] = {}
    for line in lines:
        if line == "":
            continue
        assignment = _ASSIGNMENT.fullmatch(line)
        unset = _NOT_SET.fullmatch(line)
        if assignment is not None:
            symbol, value = assignment.group(1), assignment.group(2)
        elif unset is not None:
            symbol, value = unset.group(1), "n"
        elif _COMMENT.fullmatch(line) is not None and not line.startswith("# CONFIG_"):
            continue
        else:
            raise ValueError(INVALID)
        if symbol in symbols or symbol.startswith(REJECTED_PREFIXES):
            raise ValueError(INVALID)
        symbols[symbol] = value
    if not 1 <= len(symbols) <= MAX_SYMBOLS:
        raise ValueError(INVALID)
    return symbols


def _declaration(symbol: str, value: str) -> str:
    return f"# {symbol} is not set" if value == "n" else f"{symbol}={value}"


def load(source_root: Path, name: str) -> tuple[dict[str, str], str]:
    """Read and validate the fragment of `name`; returns (symbols, sha256 of its bytes)."""
    if not name_ok(name):
        raise ValueError(INVALID)
    filename = FILE_PREFIX + name
    try:
        # Exact-case directory entry: a case-insensitive filesystem must not let
        # one name select another name's file.
        with os.scandir(source_root) as entries:
            present = any(entry.name == filename for entry in entries)
    except OSError:
        raise ValueError(INVALID) from None
    if not present:
        raise ValueError(NOT_FOUND)
    raw = _read_regular(source_root / filename, MAX_FRAGMENT_BYTES, INVALID)
    return parse(raw), hashlib.sha256(raw).hexdigest()


def bind(source_root: Path, name: str) -> dict[str, str]:
    """The plan selection for `name`, from the verified source snapshot."""
    _, sha256 = load(source_root, name)
    return {"build_variant": name, "build_variant_sha256": sha256}


def expected_symbols(source_root: Path, name: str, sha256: str) -> dict[str, str]:
    """Symbols of the fragment, which must still hash to the planned bytes."""
    symbols, actual = load(source_root, name)
    if actual != sha256:
        raise ValueError("build_inputs_changed")
    return symbols


def _config(build_dir: Path) -> bytes:
    return _read_regular(build_dir / "sdkconfig", MAX_CONFIG_BYTES, NOT_APPLIED)


def _check_applied(raw: bytes, symbols: dict[str, str], code: str) -> None:
    try:
        lines = raw.decode("utf-8").splitlines()
    except UnicodeError:
        raise ValueError(code) from None
    for symbol, value in symbols.items():
        declared = [
            line
            for line in lines
            if line.startswith(symbol + "=") or line == f"# {symbol} is not set"
        ]
        if declared != [_declaration(symbol, value)]:
            raise ValueError(code)


def seed(build_dir: Path, symbols: dict[str, str]) -> None:
    """Write this isolated output's sdkconfig; a reused, differing config is refused."""
    path = build_dir / "sdkconfig"
    try:
        if path.exists() or path.is_symlink():
            _check_applied(_read_regular(path, MAX_CONFIG_BYTES, CONFLICT), symbols, CONFLICT)
            return
        raw = "".join(_declaration(symbol, symbols[symbol]) + "\n" for symbol in sorted(symbols))
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
            0o600,
        )
        with os.fdopen(descriptor, "wb") as output:
            output.write(raw.encode("ascii"))
            output.flush()
            os.fsync(output.fileno())
    except OSError:
        raise ValueError(CONFLICT) from None


def _receipt(raw: bytes) -> dict[str, Any]:
    return {"name": "sdkconfig", "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}


def verify_applied(build_dir: Path, symbols: dict[str, str]) -> dict[str, Any]:
    """Prove the generated sdkconfig carries every fragment symbol, exactly once."""
    raw = _config(build_dir)
    _check_applied(raw, symbols, NOT_APPLIED)
    return _receipt(raw)


def verify_receipt(build_dir: Path, receipt: Any) -> None:
    """The recorded, previously verified sdkconfig bytes are still what is on disk."""
    if type(receipt) is not dict or _receipt(_config(build_dir)) != receipt:
        raise ValueError(NOT_APPLIED)
