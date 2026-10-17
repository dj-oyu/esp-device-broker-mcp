"""Ephemeral local-client credentials; never external-service or hardware credentials.

The caller must hold the resident broker's state/device locks when publishing or
removing a runtime file. AuthState must be created only for explicitly enabled
real-mode startup. Windows runtime files are deliberately unsupported until private ACLs can be
verified; chmod alone is not an ACL guarantee there. Windows callers may explicitly
choose process-environment credentials instead: a local launcher must generate a
fresh pair for each broker run and pass them only to its broker/client children.
Environment helpers never write a file, registry entry, or environment variable.
"""

import json
import os
import re
import secrets
import stat
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from uuid import uuid4

from ._platform_api import posix_file_api
from .models import Origin

_VERSION = 1
_MAX_BYTES = 4096
_TOKEN = re.compile(r"[a-f0-9]{64}\Z")
_EPOCH = re.compile(r"[a-f0-9]{32}\Z")
_ENVIRONMENT = {
    Origin.LOCAL: "CARDPUTER_LOCAL_TOKEN",
    Origin.REMOTE: "CARDPUTER_REMOTE_TOKEN",
}


class AuthError(ValueError):
    """Fixed public messages only: never include paths, tokens, or OS errors."""


def _platform() -> None:
    if os.name != "posix" or not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY"):
        raise AuthError("private_runtime_credentials_unsupported")


def _open_directory(path: Path) -> int:
    """Traverse using directory descriptors without following any symlink."""
    _platform()
    target = path.absolute()
    if target.name != "runtime-auth.json" or ".." in target.parts:
        raise AuthError("unsafe_runtime_path")
    flags = (
        os.O_RDONLY
        | posix_file_api.O_DIRECTORY
        | posix_file_api.O_NOFOLLOW
        | getattr(os, "O_CLOEXEC", 0)
    )
    descriptor: int | None = None
    try:
        descriptor = os.open("/", flags)
        for component in target.parent.parts[1:]:
            next_descriptor = os.open(component, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
        info = os.fstat(descriptor)
        if info.st_uid != posix_file_api.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise AuthError("unsafe_runtime_directory")
        result = descriptor
        descriptor = None
        return result
    except OSError:
        raise AuthError("unsafe_runtime_path") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _file_info(info: os.stat_result) -> None:
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != posix_file_api.geteuid()
        or stat.S_IMODE(info.st_mode) != 0o600
        or info.st_nlink != 1
    ):
        raise AuthError("unsafe_runtime_file")


def _read_file(directory: int) -> tuple[bytes, os.stat_result]:
    flags = (
        os.O_RDONLY
        | posix_file_api.O_NOFOLLOW
        | getattr(os, "O_CLOEXEC", 0)
        | posix_file_api.O_NONBLOCK
    )
    descriptor = os.open("runtime-auth.json", flags, dir_fd=directory)
    try:
        info = os.fstat(descriptor)
        _file_info(info)
        if info.st_size > _MAX_BYTES:
            raise AuthError("invalid_runtime_credentials")
        data = bytearray()
        while len(data) <= _MAX_BYTES:
            chunk = os.read(descriptor, _MAX_BYTES + 1 - len(data))
            if not chunk:
                break
            data.extend(chunk)
        if len(data) > _MAX_BYTES:
            raise AuthError("invalid_runtime_credentials")
        return bytes(data), info
    finally:
        os.close(descriptor)


def _tokens(values: Mapping[Origin, str]) -> dict[Origin, str]:
    if set(values) != set(Origin):
        raise AuthError("invalid_runtime_credentials")
    result = {origin: values[origin] for origin in Origin}
    if any(type(token) is not str or _TOKEN.fullmatch(token) is None for token in result.values()):
        raise AuthError("invalid_runtime_credentials")
    if secrets.compare_digest(result[Origin.LOCAL], result[Origin.REMOTE]):
        raise AuthError("invalid_runtime_credentials")
    return result


def _decode(data: bytes) -> tuple[str, dict[Origin, str]]:
    try:
        payload: Any = json.loads(data)
        if not isinstance(payload, dict) or set(payload) != {"version", "epoch", "tokens"}:
            raise AuthError("invalid_runtime_credentials")
        if type(payload["version"]) is not int or payload["version"] != _VERSION:
            raise AuthError("invalid_runtime_credentials")
        epoch = payload["epoch"]
        if type(epoch) is not str or _EPOCH.fullmatch(epoch) is None:
            raise AuthError("invalid_runtime_credentials")
        raw = payload["tokens"]
        if not isinstance(raw, dict) or set(raw) != {origin.value for origin in Origin}:
            raise AuthError("invalid_runtime_credentials")
        tokens = _tokens({origin: raw[origin.value] for origin in Origin})
        return epoch, tokens
    except (ValueError, TypeError, KeyError, RecursionError):
        raise AuthError("invalid_runtime_credentials") from None


class AuthState:
    """One process epoch and two independently generated in-memory bearer tokens."""

    def __init__(
        self, tokens: Mapping[Origin, str] | None = None, *, enable_real_auth: bool = False
    ) -> None:
        if tokens is None:
            if not enable_real_auth:
                raise AuthError("real_auth_not_enabled")
            tokens = {origin: secrets.token_hex(32) for origin in Origin}
        self._tokens = _tokens(tokens)
        self._epoch = uuid4().hex
        self._published: dict[str, tuple[int, int]] = {}

    @classmethod
    def from_environment(cls, *, enable_real_auth: bool = False) -> "AuthState":
        """Explicit launcher-managed credentials; never creates or saves credentials.

        The launcher, rather than this helper, owns generation of a fresh pair for
        every broker run. Do not use persistent user/system environment settings.
        """
        if not enable_real_auth:
            raise AuthError("real_auth_not_enabled")
        return cls({origin: load_environment_token(origin) for origin in Origin})

    def __repr__(self) -> str:
        return "AuthState(<redacted>)"

    def authenticate(self, header: str | None) -> Origin | None:
        if type(header) is not str or not header.startswith("Bearer "):
            return None
        token = header[7:]
        if _TOKEN.fullmatch(token) is None:
            return None
        local = secrets.compare_digest(token, self._tokens[Origin.LOCAL])
        remote = secrets.compare_digest(token, self._tokens[Origin.REMOTE])
        if local:
            return Origin.LOCAL
        if remote:
            return Origin.REMOTE
        return None

    def write_runtime(self, path: Path) -> None:
        try:
            self._write_runtime(path)
        except OSError:
            raise AuthError("runtime_credentials_write_failed") from None

    def _write_runtime(self, path: Path) -> None:
        directory = _open_directory(path)
        temporary = f".runtime-auth-{uuid4().hex}.tmp"
        descriptor: int | None = None
        try:
            try:
                previous = os.stat("runtime-auth.json", dir_fd=directory, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                _file_info(previous)
            payload = json.dumps(
                {
                    "version": _VERSION,
                    "epoch": self._epoch,
                    "tokens": {origin.value: token for origin, token in self._tokens.items()},
                },
                separators=(",", ":"),
            ).encode("ascii")
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | posix_file_api.O_NOFOLLOW
            descriptor = os.open(temporary, flags, 0o600, dir_fd=directory)
            posix_file_api.fchmod(descriptor, 0o600)
            _file_info(os.fstat(descriptor))
            remaining = memoryview(payload)
            while remaining:
                written = os.write(descriptor, remaining)
                if written == 0:
                    raise AuthError("runtime_credentials_write_failed")
                remaining = remaining[written:]
            os.fsync(descriptor)
            identity = os.fstat(descriptor)
            os.close(descriptor)
            descriptor = None
            os.replace(temporary, "runtime-auth.json", src_dir_fd=directory, dst_dir_fd=directory)
            self._published[str(path.absolute())] = (identity.st_dev, identity.st_ino)
            os.fsync(directory)
        except OSError:
            raise AuthError("runtime_credentials_write_failed") from None
        finally:
            if descriptor is not None:
                os.close(descriptor)
            try:
                os.unlink(temporary, dir_fd=directory)
            except FileNotFoundError:
                pass
            os.close(directory)

    def close_runtime(self, path: Path) -> bool:
        """Remove only this publisher's unchanged file; preserve any replacement."""
        key = str(path.absolute())
        expected = self._published.get(key)
        if expected is None:
            return False
        directory = _open_directory(path)
        try:
            try:
                data, info = _read_file(directory)
            except FileNotFoundError:
                self._published.pop(key, None)
                return False
            if (info.st_dev, info.st_ino) != expected:
                return False
            epoch, _ = _decode(data)
            if epoch != self._epoch:
                return False
            current = os.stat("runtime-auth.json", dir_fd=directory, follow_symlinks=False)
            if (current.st_dev, current.st_ino) != expected:
                return False
            os.unlink("runtime-auth.json", dir_fd=directory)
            os.fsync(directory)
            self._published.pop(key, None)
            return True
        except (AuthError, OSError):
            # A malformed, unsafe, or unrelated replacement is never deleted.
            return False
        finally:
            os.close(directory)


def load_client_token(path: Path, origin: Origin) -> str:
    if not isinstance(origin, Origin):
        raise AuthError("invalid_client_origin")
    directory = _open_directory(path)
    try:
        data, _ = _read_file(directory)
        _, tokens = _decode(data)
        return tokens[origin]
    except OSError:
        raise AuthError("runtime_credentials_unavailable") from None
    finally:
        os.close(directory)


def load_environment_token(origin: Origin) -> str:
    """Read only the selected identity's explicit process-scoped token."""
    if not isinstance(origin, Origin):
        raise AuthError("invalid_client_origin")
    value = os.environ.get(_ENVIRONMENT[origin])
    if value is None or _TOKEN.fullmatch(value) is None:
        raise AuthError("runtime_environment_credentials_unavailable")
    return value
