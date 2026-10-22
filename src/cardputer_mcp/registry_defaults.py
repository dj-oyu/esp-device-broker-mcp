"""Stable private registry discovery; never creates keys or caches authorization."""

import os
from importlib import import_module
from pathlib import Path

from .key_registry import KeyMetadata, KeyPrincipal, KeyRegistry, KeyRegistryError, _safe_directory


def default_key_registry_path() -> Path:
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA", "")
        if not base or not Path(base).is_absolute():
            raise KeyRegistryError("private_app_state_unavailable")
        root = Path(base)
    else:
        xdg_state = os.environ.get("XDG_STATE_HOME")
        root = Path(xdg_state) if xdg_state else Path.home() / ".local" / "state"
        if not root.is_absolute():
            raise KeyRegistryError("private_app_state_unavailable")
    if ".." in root.parts:
        raise KeyRegistryError("unsafe_key_registry_path")
    return root / "DeviceBroker" / "keys.sqlite3"


def _windows_private(path: Path, *, private: bool = True) -> None:
    if os.name != "nt":
        return
    try:
        security = import_module("win32security")
        api = import_module("win32api")
        token = security.OpenProcessToken(api.GetCurrentProcess(), 8)
        try:
            current = security.ConvertSidToStringSid(security.GetTokenInformation(token, 1)[0])
        finally:
            token.Close()
        trusted = {current, "S-1-5-18", "S-1-5-32-544"}
        if not private:
            trusted.add("S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464")
        descriptor = security.GetNamedSecurityInfo(str(path), 1, 1 | 4)
        if security.ConvertSidToStringSid(descriptor.GetSecurityDescriptorOwner()) not in trusted:
            raise KeyRegistryError("unsafe_key_registry_permissions")
        acl = descriptor.GetSecurityDescriptorDacl()
        if acl is None:
            raise KeyRegistryError("unsafe_key_registry_permissions")
        for index in range(acl.GetAceCount()):
            ace = acl.GetAce(index)
            kind, flags = ace[0]
            if kind in (1, 6) or (not private and flags & 8):  # No effective grant here.
                continue
            if kind not in (0, 5):
                raise KeyRegistryError("unsafe_key_registry_permissions")
            sid = security.ConvertSidToStringSid(ace[-1])
            # OWNER RIGHTS refers to the actual owner verified above (also used
            # by Python's Windows mkdir(0o700)); never trust it as an owner SID.
            # CREATOR OWNER only grants inherited access to the child's owner.
            if sid in trusted or sid == "S-1-3-4" or (flags & 8 and sid == "S-1-3-0"):
                continue
            forbidden = ace[1] if private else ace[1] & 0x100D0040
            if forbidden:
                raise KeyRegistryError("unsafe_key_registry_permissions")
    except KeyRegistryError:
        raise
    except Exception:
        raise KeyRegistryError("unsafe_key_registry_permissions") from None


def validate_default_key_path(path: Path) -> None:
    try:
        _safe_directory(path.parent)
        for directory in reversed(path.parent.parents):
            _windows_private(directory, private=False)
        _windows_private(path.parent)
        if path.exists() or path.is_symlink():
            _windows_private(path)
        for suffix in ("-journal", "-wal", "-shm"):
            sidecar = path.with_name(path.name + suffix)
            if sidecar.exists() or sidecar.is_symlink():
                _windows_private(sidecar)
    except OSError:
        raise KeyRegistryError("key_registry_unavailable") from None


def prepare_default_key_directory(path: Path) -> None:
    """Only called after explicit local ISSUE confirmation; no ACL repair."""
    ancestor = path.parent
    while not ancestor.exists() and not ancestor.is_symlink():
        ancestor = ancestor.parent
    _safe_directory(ancestor)
    for directory in (*reversed(ancestor.parents), ancestor):
        _windows_private(directory, private=False)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    validate_default_key_path(path)


class LiveKeyRegistry:
    """Missing default DB denies issued keys; later creation/replacement is visible.

    Existing malformed/unsafe stores reject startup. Every operation opens and
    validates the current store; no raw token, key row, failure or absence is cached.
    Existing process-only credentials are independent of registry availability.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.closed = False
        try:
            if path.exists() or path.is_symlink():
                self._open().close()
        except OSError:
            raise KeyRegistryError("key_registry_unavailable") from None

    def _open(self) -> KeyRegistry:
        if self.closed:
            raise KeyRegistryError("key_registry_closed")
        validate_default_key_path(self.path)
        return KeyRegistry(self.path, create=False)

    def authenticate(self, header: str | None) -> KeyPrincipal | None:
        try:
            registry = self._open()
            try:
                return registry.authenticate(header)
            finally:
                registry.close()
        except (KeyRegistryError, OSError):
            return None

    def list_keys(self) -> list[KeyMetadata]:
        if not self.path.exists() and not self.path.is_symlink() and not self.closed:
            return []
        registry = self._open()
        try:
            return registry.list_keys()
        finally:
            registry.close()

    def close(self) -> None:
        self.closed = True
