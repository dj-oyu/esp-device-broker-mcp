"""Load an explicitly selected, private, repository-external client env file.

Grammar: UTF-8 (an initial BOM is allowed), LF or CRLF lines, blank lines and
whole-line ``#`` comments. Assignments have no whitespace or quoting:
``CARDPUTER_CLIENT_KEY=cpk_<32 lowercase hex>.<64 lowercase hex>`` is required;
``CARDPUTER_BROKER_URL=http://127.0.0.1:<1024..65535>/mcp`` is optional.
No other keys, duplicates, inline comments, escapes, expansion or shell syntax
are accepted. Nothing is read from or written to the process environment.

The operator must place the file outside the adapter installation/source tree
and Git checkouts, in a private directory. POSIX file ownership and permissions
are checked; Windows ACL privacy is an operator prerequisite, NOT verified here.
Linked/reparse paths and multiply linked files are rejected. Descriptor checks
limit replacement races; this is not a defense against a hostile account owner
who can change the containing directories. No permissions or ACLs are modified.
"""

import os
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path

_MAX_BYTES = 8192
_DEFAULT_URL = "http://127.0.0.1:8765/mcp"
_KEY = re.compile(r"cpk_[a-f0-9]{32}\.[a-f0-9]{64}\Z")
_URL = re.compile(r"http://127\.0\.0\.1:([1-9][0-9]{3,4})/mcp\Z")
_ALLOWED_KEYS = {"CARDPUTER_CLIENT_KEY", "CARDPUTER_BROKER_URL"}


class ClientEnvironmentError(ValueError):
    """A fixed public error that never contains file paths, values or OS errors."""

    def __init__(self) -> None:
        super().__init__("invalid_client_environment")


@dataclass(frozen=True, slots=True)
class ClientEnvironment:
    url: str
    token: str = field(repr=False)


def _adapter_root() -> Path:
    package_parent = Path(__file__).resolve().parent.parent
    return package_parent.parent if package_parent.name == "src" else package_parent


def _check_location(path: Path) -> None:
    if not path.is_absolute() or ".." in path.parts:
        raise ClientEnvironmentError()
    root = _adapter_root()
    # Resolve aliases as well as checking the supplied spelling, including
    # Windows short names. Linked components are separately rejected before IO.
    resolved = path.resolve(strict=True)
    if path.is_relative_to(root) or resolved.is_relative_to(root):
        raise ClientEnvironmentError()
    # A .git directory, worktree file or even a dangling .git link counts.
    # Check every ancestor, not only the nearest repository boundary.
    for origin in (path.parent, resolved.parent, root, Path.cwd()):
        for ancestor in (origin, *origin.parents):
            try:
                (ancestor / ".git").lstat()
            except FileNotFoundError:
                continue
            if path.is_relative_to(ancestor) or resolved.is_relative_to(ancestor):
                raise ClientEnvironmentError()


def _is_link(info: os.stat_result) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def _check_file(info: os.stat_result) -> None:
    if _is_link(info) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ClientEnvironmentError()
    if info.st_size > _MAX_BYTES:
        raise ClientEnvironmentError()
    if os.name == "posix" and (
        info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) not in (0o400, 0o600)
    ):
        raise ClientEnvironmentError()


def _path_identity(path: Path) -> os.stat_result:
    for directory in reversed(path.parents):
        info = directory.lstat()
        if _is_link(info) or not stat.S_ISDIR(info.st_mode):
            raise ClientEnvironmentError()
    info = path.lstat()
    _check_file(info)
    return info


def _open_file(path: Path) -> int:
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_CLOEXEC", 0)
    # Opening a raced-in FIFO must not block before the fstat regular-file check.
    flags |= getattr(os, "O_NONBLOCK", 0)
    if (
        os.name != "posix"
        or not hasattr(os, "O_NOFOLLOW")
        or not hasattr(os, "O_DIRECTORY")
        or os.open not in os.supports_dir_fd
    ):
        return os.open(path, flags | getattr(os, "O_NOFOLLOW", 0))
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    directory_flags |= getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(path.anchor, directory_flags)
    try:
        for component in path.parts[1:-1]:
            child = os.open(component, directory_flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        return os.open(path.name, flags | os.O_NOFOLLOW, dir_fd=descriptor)
    finally:
        os.close(descriptor)


def _identity(info: os.stat_result) -> tuple[int, int, int, int, int]:
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _read_file(path: Path) -> bytes:
    before = _path_identity(path)
    descriptor = _open_file(path)
    try:
        opened = os.fstat(descriptor)
        _check_file(opened)
        if _identity(before) != _identity(opened):
            raise ClientEnvironmentError()
        data = bytearray()
        while len(data) <= _MAX_BYTES:
            chunk = os.read(descriptor, _MAX_BYTES + 1 - len(data))
            if not chunk:
                break
            data.extend(chunk)
        after = os.fstat(descriptor)
        _check_file(after)
        if len(data) > _MAX_BYTES or _identity(opened) != _identity(after):
            raise ClientEnvironmentError()
        if _identity(opened) != _identity(_path_identity(path)):
            raise ClientEnvironmentError()
        return bytes(data)
    finally:
        os.close(descriptor)


def _parse(data: bytes) -> ClientEnvironment:
    text = data.decode("utf-8-sig").replace("\r\n", "\n")
    # splitlines() accepts extra Unicode and control-character line separators.
    if any(character in text for character in "\r\x00\v\f\x85\u2028\u2029"):
        raise ClientEnvironmentError()
    values: dict[str, str] = {}
    for line in text.split("\n"):
        if not line.strip(" \t") or line.lstrip(" \t").startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator or key not in _ALLOWED_KEYS or key in values:
            raise ClientEnvironmentError()
        values[key] = value
    token = values.get("CARDPUTER_CLIENT_KEY", "")
    url = values.get("CARDPUTER_BROKER_URL", _DEFAULT_URL)
    match = _URL.fullmatch(url)
    if _KEY.fullmatch(token) is None or match is None or not 1024 <= int(match.group(1)) <= 65535:
        raise ClientEnvironmentError()
    return ClientEnvironment(url=url, token=token)


def load_client_environment(path: Path) -> ClientEnvironment:
    """Read one private file, returning only validated URL/key values.

    The absolute path must be supplied explicitly; there is no default search,
    environment fallback, interpolation, or process-environment mutation.
    """
    try:
        _check_location(path)
        data = _read_file(path)
        _check_location(path)
        return _parse(data)
    except (OSError, ValueError, RuntimeError):
        raise ClientEnvironmentError() from None
