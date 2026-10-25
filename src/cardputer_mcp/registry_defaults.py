"""Stable private registry discovery; never creates keys or caches authorization."""

import os
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


def prepare_default_key_directory(path: Path) -> None:
    """Only called after explicit local ISSUE confirmation; no ACL repair."""
    ancestor = path.parent
    while not ancestor.exists() and not ancestor.is_symlink():
        ancestor = ancestor.parent
    _safe_directory(ancestor)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    _safe_directory(path.parent)


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

    def same_owner(self, first: str, second: str) -> bool:
        """Rotation lineage lookup; a missing/unavailable store returns False."""
        try:
            if not self.path.exists() and not self.path.is_symlink():
                return False
            registry = self._open()
            try:
                return registry.same_owner(first, second)
            finally:
                registry.close()
        except (KeyRegistryError, OSError):
            return False

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
