"""Replace the client key inside an existing, already private stdio env file.

Used only by the explicit local ``keys issue/rotate --write-env`` operator
command. The target must already exist and pass the same location, link,
identity, size and grammar checks the stdio adapter applies when reading it
(:mod:`cardputer_mcp.client_environment`). Only the ``CARDPUTER_CLIENT_KEY``
value changes (a key line is appended when the file has none); every other
byte, the BOM and the newline style are preserved.

The new content is written to a temporary file in the same directory, flushed
and synced, and then swapped in. Windows uses ``ReplaceFileW``, which keeps
the replaced file's security descriptor and attributes instead of taking the
directory's inherited ACL; POSIX uses an exclusive no-follow temporary file
with the original mode and ``os.replace``. Until the swap, the temporary file
has the directory's default protection, so the directory must be private.
This module never changes ACLs, never prints paths or values, and raises only
fixed messages. Like the reader, it limits replacement races; it is not a
defense against a hostile owner of the containing directories.
"""

import os
import secrets
import stat
import sys
from pathlib import Path

from .client_environment import (
    _MAX_BYTES,
    _check_location,
    _identity,
    _is_link,
    _parse,
    _path_identity,
    _read_file,
    load_client_environment,
)

_PREFIX = b"CARDPUTER_CLIENT_KEY="
_BOM = b"\xef\xbb\xbf"
# Shape-valid stand-in used to validate the rendered file before a real key exists.
_PROBE_TOKEN = "cpk_" + "0" * 32 + "." + "0" * 64
_READONLY = getattr(stat, "FILE_ATTRIBUTE_READONLY", 0x1)


class EnvFileWriteError(ValueError):
    """A fixed public error that never contains file paths, values or OS errors."""


def _render(original: bytes, token: str) -> bytes:
    bom = original.startswith(_BOM)
    body = original[len(_BOM) :] if bom else original
    # Same line-separator rules as the reader, checked before splitting so a
    # stray separator can never hide or drop content within the replaced line.
    text = body.decode("utf-8").replace("\r\n", "\n")
    if any(character in text for character in "\r\x00\v\f\x85\u2028\u2029"):
        raise ValueError("invalid_client_environment")
    newline = b"\r\n" if b"\r\n" in body else b"\n"
    value = token.encode("ascii")
    lines = body.split(b"\n")
    replaced = False
    for index, line in enumerate(lines):
        if line.startswith(_PREFIX):
            ending = b"\r" if line.endswith(b"\r") else b""
            lines[index] = _PREFIX + value + ending
            replaced = True
    rendered = b"\n".join(lines)
    if not replaced:
        if rendered and not rendered.endswith(b"\n"):
            rendered += newline
        rendered += _PREFIX + value + newline
    return (_BOM if bom else b"") + rendered


def _current_key_id(original: bytes) -> str | None:
    try:
        token = _parse(original).token
    except ValueError:
        return None
    return token.partition(".")[0]


def _inspect(path: Path, expected_key_id: str | None) -> tuple[bytes, os.stat_result]:
    """Read-only validation shared by the pre-confirmation check and prepare()."""
    if not isinstance(path, Path):
        raise EnvFileWriteError("key_env_file_invalid")
    try:
        _check_location(path)
        info = _path_identity(path)
        original = _read_file(path)
        _check_location(path)
        # The file may hold a placeholder or old key; everything else must parse.
        # Real keys have the probe's fixed length, so the size bound carries over.
        probe = _render(original, _PROBE_TOKEN)
        _parse(probe)
        if len(probe) > _MAX_BYTES:
            raise EnvFileWriteError("key_env_file_invalid")
    except (OSError, ValueError, RuntimeError):
        raise EnvFileWriteError("key_env_file_invalid") from None
    if os.name == "nt" and getattr(info, "st_file_attributes", 0) & _READONLY:
        raise EnvFileWriteError("key_env_file_not_writable")
    if expected_key_id is not None:
        current = _current_key_id(original)
        if current is not None and current != expected_key_id:
            raise EnvFileWriteError("key_env_file_holds_other_key")
    return original, info


def check_env_file(path: Path, *, expected_key_id: str | None = None) -> None:
    """Validate the target without creating or changing anything."""
    _inspect(path, expected_key_id)


if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.ReplaceFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.LPVOID,
    ]
    _kernel32.ReplaceFileW.restype = wintypes.BOOL
    _kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    _kernel32.CreateFileW.restype = wintypes.HANDLE
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.CloseHandle.restype = wintypes.BOOL
    _INVALID_HANDLE = wintypes.HANDLE(-1).value

    def _probe_replaceable(path: Path) -> None:
        # ReplaceFileW needs read + delete access to the replaced file.
        handle = _kernel32.CreateFileW(
            str(path),
            0x80000000 | 0x00010000,  # GENERIC_READ | DELETE
            0x1 | 0x2 | 0x4,  # share read/write/delete
            None,
            3,  # OPEN_EXISTING
            0x00200000,  # FILE_FLAG_OPEN_REPARSE_POINT: never follow a raced-in link
            None,
        )
        if handle is None or handle == _INVALID_HANDLE:
            raise EnvFileWriteError("key_env_file_not_writable")
        _kernel32.CloseHandle(handle)

    _UNABLE_TO_MOVE_REPLACEMENT_2 = 1177

    def _replace_file(replaced: Path, replacement: Path, backup: Path) -> int:
        """Return 0 on success, else the Win32 error code."""
        if _kernel32.ReplaceFileW(str(replaced), str(replacement), str(backup), 0, None, None):
            return 0
        return ctypes.get_last_error() or -1

    def _swap(temporary: Path, path: Path) -> None:
        # A backup name makes ReplaceFileW keep both names on a failed rename
        # (without one, the target can vanish). It is the renamed original
        # file and is removed after success.
        backup = path.parent / f".{path.name}.{secrets.token_hex(8)}.bak"
        original = path.lstat()
        error = _replace_file(path, temporary, backup)
        if error == _UNABLE_TO_MOVE_REPLACEMENT_2:
            raise EnvFileWriteError("key_env_file_replace_failed_original_kept_as_backup")
        if error:
            raise EnvFileWriteError("key_env_file_replace_failed")
        try:
            info = backup.lstat()
            if (info.st_dev, info.st_ino) == (original.st_dev, original.st_ino) and not _is_link(
                info
            ):
                backup.unlink()
        except OSError:
            pass

    def _sync_directory(directory: Path) -> None:
        # NTFS metadata for ReplaceFileW is journaled; there is no directory fsync.
        return None

else:

    def _probe_replaceable(path: Path) -> None:
        if not os.access(path.parent, os.W_OK | os.X_OK):
            raise EnvFileWriteError("key_env_file_not_writable")

    def _swap(temporary: Path, path: Path) -> None:
        os.replace(temporary, path)

    def _sync_directory(directory: Path) -> None:
        descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


class PreparedEnvWrite:
    """A validated target plus an empty same-directory temporary file.

    ``commit`` writes the key and swaps it in; ``discard`` removes the
    temporary file if it still exists. Neither repr nor errors expose paths.
    """

    __slots__ = ("_committed", "_identity", "_mode", "_original", "_path", "_temp", "_temp_id")

    def __init__(self, path: Path, *, expected_key_id: str | None = None) -> None:
        self._committed = False
        self._temp: Path | None = None
        self._temp_id: tuple[int, int] | None = None
        original, info = _inspect(path, expected_key_id)
        self._path = path
        self._original = original
        self._identity = _identity(info)
        self._mode = stat.S_IMODE(info.st_mode)
        try:
            _probe_replaceable(path)
            self._create_temporary()
        except EnvFileWriteError:
            self.discard()
            raise
        except OSError:
            self.discard()
            raise EnvFileWriteError("key_env_file_not_writable") from None

    def __repr__(self) -> str:
        return "PreparedEnvWrite()"

    def _create_temporary(self) -> None:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_BINARY", 0) | getattr(os, "O_NOINHERIT", 0)
        flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        candidate = self._path.parent / f".{self._path.name}.{secrets.token_hex(8)}.tmp"
        descriptor = os.open(candidate, flags, 0o600)
        self._temp = candidate
        try:
            if sys.platform != "win32":
                # Owner-only regardless of umask; the original 0400/0600 mode is
                # applied after writing. The owner is this euid, as the reader requires.
                os.fchmod(descriptor, 0o600)
            opened = os.fstat(descriptor)
            self._temp_id = (opened.st_dev, opened.st_ino)
        finally:
            os.close(descriptor)

    def _write_temporary(self, content: bytes) -> None:
        assert self._temp is not None
        flags = os.O_WRONLY | os.O_TRUNC | getattr(os, "O_BINARY", 0)
        flags |= getattr(os, "O_NOINHERIT", 0) | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(self._temp, flags)
        try:
            opened = os.fstat(descriptor)
            if (opened.st_dev, opened.st_ino) != self._temp_id or opened.st_nlink != 1:
                raise EnvFileWriteError("key_env_file_replace_failed")
            view = memoryview(content)
            while view:
                written = os.write(descriptor, view)
                view = view[written:]
            if sys.platform != "win32":
                os.fchmod(descriptor, self._mode)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def commit(self, token: str) -> None:
        """Write ``token`` into the file. Any failure leaves no temporary file."""
        if self._committed or self._temp is None:
            raise EnvFileWriteError("key_env_file_replace_failed")
        try:
            content = _render(self._original, token)
            if _parse(content).token != token:
                raise EnvFileWriteError("key_env_file_replace_failed")
            self._write_temporary(content)
            # Re-check the target right before the swap: same location rules,
            # same file identity, not a link/reparse point, unchanged contents.
            _check_location(self._path)
            if _identity(_path_identity(self._path)) != self._identity:
                raise EnvFileWriteError("key_env_file_changed")
            if _read_file(self._path) != self._original:
                raise EnvFileWriteError("key_env_file_changed")
            _swap(self._temp, self._path)
            self._committed = True
            self._temp = None
            _sync_directory(self._path.parent)
            if load_client_environment(self._path).token != token:
                raise EnvFileWriteError("key_env_file_unverified")
        except EnvFileWriteError:
            self.discard()
            raise
        except (OSError, ValueError, RuntimeError):
            self.discard()
            raise EnvFileWriteError("key_env_file_replace_failed") from None

    def discard(self) -> None:
        """Remove our temporary file, never a file swapped in by someone else."""
        temporary, self._temp = self._temp, None
        if temporary is None:
            return
        try:
            info = temporary.lstat()
            if (
                stat.S_ISREG(info.st_mode)
                and not stat.S_ISLNK(info.st_mode)
                and (info.st_dev, info.st_ino) == self._temp_id
            ):
                temporary.unlink()
        except OSError:
            pass


def prepare_env_write(path: Path, *, expected_key_id: str | None = None) -> PreparedEnvWrite:
    """Validate the target and create the temporary file, before any key exists."""
    return PreparedEnvWrite(path, expected_key_id=expected_key_id)
