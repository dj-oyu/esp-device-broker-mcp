"""Automatic private discovery with synthetic keys only; never user state."""

import itertools
import os
import sqlite3
import tempfile
from importlib import import_module
from pathlib import Path

import pytest

from cardputer_mcp.key_registry import KeyRegistry, KeyRegistryError
from cardputer_mcp.models import Origin
from cardputer_mcp.registry_defaults import (
    LiveKeyRegistry,
    default_key_registry_path,
    prepare_default_key_directory,
)


def private_directory(path):
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if os.name == "nt":
        security = import_module("win32security")
        api = import_module("win32api")
        token = security.OpenProcessToken(api.GetCurrentProcess(), 8)
        try:
            sid = security.GetTokenInformation(token, 1)[0]
        finally:
            token.Close()
        acl = security.ACL()
        acl.AddAccessAllowedAceEx(2, 3, 0x1F01FF, sid)
        security.SetNamedSecurityInfo(str(path), 1, 4 | 0x80000000, None, None, acl, None)


@pytest.fixture
def automatic_path(tmp_path, monkeypatch):
    base = os.environ.get("LOCALAPPDATA") if os.name == "nt" else None
    with tempfile.TemporaryDirectory(prefix="broker-registry-test-", dir=base) as directory:
        root = Path(directory)
        monkeypatch.setenv("LOCALAPPDATA", str(root))
        monkeypatch.setenv("XDG_STATE_HOME", str(root))
        yield root / "DeviceBroker" / "keys.sqlite3"


def test_default_is_stable_across_cwd_and_executable(automatic_path, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert default_key_registry_path() == automatic_path
    version = tmp_path / "versions" / "synthetic"
    version.mkdir(parents=True)
    monkeypatch.chdir(version)
    monkeypatch.setattr("sys.executable", str(version / "cardputer.exe"))
    assert default_key_registry_path() == automatic_path
    assert not automatic_path.exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX state fallback")
def test_posix_home_fallback_is_absolute(tmp_path, monkeypatch):
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert default_key_registry_path() == tmp_path / ".local/state/DeviceBroker/keys.sqlite3"
    monkeypatch.setenv("XDG_STATE_HOME", "relative")
    with pytest.raises(KeyRegistryError, match="private_app_state_unavailable"):
        default_key_registry_path()


def test_missing_then_issue_revoke_atomic_replace_and_corrupt_fail_closed(automatic_path):
    source = LiveKeyRegistry(automatic_path)
    assert source.list_keys() == []
    assert source.authenticate("Bearer cpk_" + "a" * 32 + "." + "b" * 64) is None
    assert not automatic_path.parent.exists()
    private_directory(automatic_path.parent)
    sequence = itertools.count(1)
    registry = KeyRegistry(automatic_path, random_bytes=lambda n: bytes([next(sequence)]) * n)
    key = registry.issue("synthetic", Origin.REMOTE)
    header = "Bearer " + key.token
    assert source.authenticate(header).principal_id == key.metadata.principal_id
    registry.revoke(key.metadata.key_id)
    assert source.authenticate(header) is None
    registry.close()
    replacement = automatic_path.with_name("replacement.sqlite3")
    other = KeyRegistry(replacement, random_bytes=lambda n: b"\x09" * n)
    second = other.issue("replacement", Origin.REMOTE)
    other.close()
    os.replace(replacement, automatic_path)
    assert source.authenticate(header) is None
    assert source.authenticate("Bearer " + second.token) is not None
    # Atomic replacement with incomplete/unrecognized content must not retain grants.
    replacement.write_bytes(b"incomplete transaction or database")
    replacement.chmod(0o600)
    os.replace(replacement, automatic_path)
    assert source.authenticate("Bearer " + second.token) is None
    with pytest.raises(KeyRegistryError):
        LiveKeyRegistry(automatic_path)
    automatic_path.unlink()
    assert source.authenticate(header) is None and not automatic_path.exists()
    source.close()
    assert source.authenticate(header) is None


def test_uncommitted_revocation_and_rollback_do_not_cache_grants(automatic_path):
    private_directory(automatic_path.parent)
    registry = KeyRegistry(automatic_path, random_bytes=lambda n: b"\x03" * n)
    key = registry.issue("synthetic", Origin.REMOTE)
    source = LiveKeyRegistry(automatic_path)
    with sqlite3.connect(automatic_path) as writer:
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("UPDATE api_keys SET revoked_at=issued_at")
        assert source.authenticate("Bearer " + key.token) is not None
        writer.commit()
    assert source.authenticate("Bearer " + key.token) is None
    registry.close()


def test_replaced_schema_version_is_rejected_by_existing_reader(automatic_path):
    private_directory(automatic_path.parent)
    registry = KeyRegistry(automatic_path, random_bytes=lambda n: b"\x04" * n)
    key = registry.issue("synthetic", Origin.REMOTE)
    with sqlite3.connect(automatic_path) as writer:
        writer.execute("PRAGMA user_version=99")
    assert registry.authenticate("Bearer " + key.token) is None
    registry.close()


def test_default_registry_permissions_fail_closed(automatic_path):
    private_directory(automatic_path.parent)
    registry = KeyRegistry(automatic_path, random_bytes=lambda n: b"\x05" * n)
    key = registry.issue("synthetic", Origin.REMOTE)
    source = LiveKeyRegistry(automatic_path)
    if os.name == "nt":
        security = import_module("win32security")
        descriptor = security.GetNamedSecurityInfo(str(automatic_path), 1, 4)
        acl = descriptor.GetSecurityDescriptorDacl()
        acl.AddAccessAllowedAceEx(2, 0, 0x120089, security.CreateWellKnownSid(1, None))
        security.SetNamedSecurityInfo(str(automatic_path), 1, 4, None, None, acl, None)
    else:
        automatic_path.chmod(0o644)
    assert source.authenticate("Bearer " + key.token) is None
    with pytest.raises(KeyRegistryError, match="unsafe_key_registry_permissions"):
        LiveKeyRegistry(automatic_path)
    registry.close()


@pytest.mark.skipif(os.name != "nt", reason="Native Windows inheritable ACL")
def test_foreign_inherit_only_grant_cannot_create_default_database(automatic_path):
    private_directory(automatic_path.parent)
    security = import_module("win32security")
    descriptor = security.GetNamedSecurityInfo(str(automatic_path.parent), 1, 4)
    acl = descriptor.GetSecurityDescriptorDacl()
    acl.AddAccessAllowedAceEx(2, 1 | 2 | 8, 0x1F01FF, security.CreateWellKnownSid(1, None))
    security.SetNamedSecurityInfo(str(automatic_path.parent), 1, 4, None, None, acl, None)
    with pytest.raises(KeyRegistryError, match="unsafe_key_registry_permissions"):
        prepare_default_key_directory(automatic_path)
    assert not automatic_path.exists()
