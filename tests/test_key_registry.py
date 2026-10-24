"""Registry tests issue synthetic fixture keys only, under temporary directories."""

import hashlib
import os
import sqlite3
import stat
from collections.abc import Iterator
from dataclasses import asdict
from pathlib import Path

import pytest

from cardputer_mcp.key_registry import (
    DEFAULT_TTL_SECONDS,
    MAX_TTL_SECONDS,
    KeyPrincipal,
    KeyRegistry,
    KeyRegistryError,
)
from cardputer_mcp.models import Origin


class SyntheticSources:
    def __init__(self) -> None:
        self.now = 1_700_000_000.0
        self.counter = 0
        self.sizes: list[int] = []

    def clock(self) -> float:
        return self.now

    def random_bytes(self, size: int) -> bytes:
        self.sizes.append(size)
        self.counter += 1
        return self.counter.to_bytes(size, "big")


@pytest.fixture
def sources() -> SyntheticSources:
    return SyntheticSources()


@pytest.fixture
def registry(tmp_path: Path, sources: SyntheticSources) -> Iterator[KeyRegistry]:
    value = KeyRegistry(
        tmp_path / "keys.sqlite3", clock=sources.clock, random_bytes=sources.random_bytes
    )
    yield value
    value.close()


def test_issue_default_read_key(registry: KeyRegistry, sources: SyntheticSources) -> None:
    issued = registry.issue("Operator laptop", Origin.LOCAL)
    assert sources.sizes == [16, 32]
    assert (
        issued.token == "cpk_" + (1).to_bytes(16, "big").hex() + "." + (2).to_bytes(32, "big").hex()
    )
    assert issued.metadata.label == "Operator laptop"
    assert issued.metadata.scopes == frozenset({"read"})
    assert issued.metadata.expires_at == sources.now + DEFAULT_TTL_SECONDS
    assert issued.metadata.revoked_at is None
    assert registry.authenticate("Bearer " + issued.token) == KeyPrincipal(
        "key:" + issued.metadata.key_id,
        issued.metadata.key_id,
        Origin.LOCAL,
        frozenset({"read"}),
    )
    assert registry.list_keys() == [issued.metadata]


def test_origin_and_principal_cannot_be_changed_by_client(registry: KeyRegistry) -> None:
    local = registry.issue("local", Origin.LOCAL, scopes={"control"})
    remote = registry.issue("remote", Origin.REMOTE, scopes={"read", "control"})
    first = registry.authenticate("Bearer " + local.token)
    second = registry.authenticate("Bearer " + remote.token)
    assert first is not None and second is not None
    assert first.origin == Origin.LOCAL and second.origin == Origin.REMOTE
    assert first.principal_id != second.principal_id
    assert first.scopes == second.scopes == frozenset({"read", "control"})
    assert registry.authenticate("Bearer " + local.token) == first
    assert registry.authenticate("Bearer " + remote.token) == second


def test_only_hash_persisted_no_token_in_repr_or_audit(
    registry: KeyRegistry, tmp_path: Path
) -> None:
    issued = registry.issue("safe-label", Origin.REMOTE)
    secret = issued.token.split(".")[1]
    with sqlite3.connect(tmp_path / "keys.sqlite3") as db:
        rows = db.execute("SELECT * FROM api_keys").fetchall()
    assert rows[0][1] == hashlib.sha256(issued.token.encode("ascii")).digest()
    for path in tmp_path.iterdir():
        contents = path.read_bytes()
        assert issued.token.encode() not in contents
        assert secret.encode() not in contents
    rendered = repr(issued) + repr(registry) + repr(registry.list_keys())
    rendered += repr(registry.audit_events())
    assert issued.token not in rendered
    assert secret not in rendered
    event = registry.audit_events()[0]
    assert set(asdict(event)) == {"event", "key_id", "at"}
    assert event.event == "key_issued" and event.key_id == issued.metadata.key_id


@pytest.mark.parametrize(
    "transform",
    [
        lambda token: None,
        lambda token: token,
        lambda token: "Bearer " + token + "\n",
        lambda token: "Bearer " + token.upper(),
        lambda token: "Bearer " + token[:-1] + "f",
        lambda token: "Bearer cpk_" + "0" * 32 + "." + "0" * 64,
    ],
)
def test_malformed_unknown_and_wrong_keys_rejected(
    registry: KeyRegistry, transform: object
) -> None:
    issued = registry.issue("remote", Origin.REMOTE)
    assert callable(transform)
    assert registry.authenticate(transform(issued.token)) is None


def test_expiry_checked_on_every_request(registry: KeyRegistry, sources: SyntheticSources) -> None:
    issued = registry.issue("remote", Origin.REMOTE, ttl_seconds=2)
    header = "Bearer " + issued.token
    assert registry.authenticate(header) is not None
    sources.now += 1.999
    assert registry.authenticate(header) is not None
    sources.now += 0.001
    assert registry.authenticate(header) is None
    assert registry.list_keys()[0].revoked_at is None
    sources.now = issued.metadata.issued_at - 1
    assert registry.authenticate(header) is None


def test_other_connection_revocation_immediate(
    registry: KeyRegistry, tmp_path: Path, sources: SyntheticSources
) -> None:
    issued = registry.issue("remote", Origin.REMOTE)
    operator = KeyRegistry(
        tmp_path / "keys.sqlite3", clock=sources.clock, random_bytes=sources.random_bytes
    )
    assert registry.authenticate("Bearer " + issued.token) is not None
    assert operator.revoke(issued.metadata.key_id) is True
    assert registry.authenticate("Bearer " + issued.token) is None
    assert operator.revoke(issued.metadata.key_id) is False
    assert operator.revoke("cpk_" + "f" * 32) is False
    assert registry.list_keys()[0].revoked_at == sources.now
    assert [item.event for item in registry.audit_events()] == ["key_revoked", "key_issued"]
    operator.close()


def test_rotation_preserves_capabilities_and_revokes_old_atomically(
    registry: KeyRegistry, sources: SyntheticSources
) -> None:
    old = registry.issue("remote", Origin.REMOTE, scopes={"read", "control"})
    sources.now += 1
    replacement = registry.rotate(old.metadata.key_id, ttl_seconds=20)
    assert replacement.token != old.token
    assert replacement.metadata.principal_id != old.metadata.principal_id
    assert replacement.metadata.label == old.metadata.label
    assert replacement.metadata.origin == old.metadata.origin
    assert replacement.metadata.scopes == old.metadata.scopes
    assert replacement.metadata.expires_at == sources.now + 20
    assert registry.authenticate("Bearer " + old.token) is None
    assert registry.authenticate("Bearer " + replacement.token) is not None
    assert [item.event for item in registry.audit_events()] == [
        "key_rotated",
        "key_issued",
        "key_issued",
    ]


def test_revoked_and_expired_keys_cannot_be_rotated(
    registry: KeyRegistry, sources: SyntheticSources
) -> None:
    revoked = registry.issue("revoked", Origin.LOCAL)
    expired = registry.issue("expired", Origin.REMOTE, ttl_seconds=1)
    registry.revoke(revoked.metadata.key_id)
    sources.now += 1
    for key_id in (revoked.metadata.key_id, expired.metadata.key_id, "cpk_" + "f" * 32):
        with pytest.raises(KeyRegistryError, match="^key_not_active$"):
            registry.rotate(key_id)


def test_registry_capacity_counts_inactive_records(
    tmp_path: Path, sources: SyntheticSources
) -> None:
    registry = KeyRegistry(
        tmp_path / "keys.sqlite3",
        clock=sources.clock,
        random_bytes=sources.random_bytes,
        max_entries=1,
    )
    key = registry.issue("remote", Origin.REMOTE)
    registry.revoke(key.metadata.key_id)
    with pytest.raises(KeyRegistryError, match="^key_registry_full$"):
        registry.issue("another", Origin.REMOTE)
    assert sources.sizes == [16, 32]


@pytest.mark.parametrize("ttl", [0, MAX_TTL_SECONDS + 1, True, MAX_TTL_SECONDS])
def test_invalid_expiry_rejected(
    registry: KeyRegistry, sources: SyntheticSources, ttl: int
) -> None:
    if ttl == MAX_TTL_SECONDS:
        issued = registry.issue("remote", Origin.REMOTE, ttl_seconds=ttl)
        assert issued.metadata.expires_at - issued.metadata.issued_at == MAX_TTL_SECONDS
        return
    with pytest.raises(KeyRegistryError, match="^invalid_key_expiry$"):
        registry.issue("remote", Origin.REMOTE, ttl_seconds=ttl)
    assert sources.sizes == []


@pytest.mark.parametrize("label", ["", "x" * 65, "x\x1by", "<x>"])
def test_unsafe_labels_never_echoed(registry: KeyRegistry, label: str) -> None:
    with pytest.raises(KeyRegistryError) as error:
        registry.issue(label, Origin.REMOTE)
    assert str(error.value) == "invalid_key_label"
    assert registry.list_keys() == []


@pytest.mark.parametrize(
    ("origin", "scopes", "code"),
    [
        (Origin.REMOTE, set(), "invalid_key_scopes"),
        (Origin.REMOTE, {"read", "admin"}, "invalid_key_scopes"),
        ("remote_mcp", {"read"}, "invalid_key_origin"),
    ],
)
def test_invalid_scopes_rejected(
    registry: KeyRegistry, origin: Origin, scopes: set[str], code: str
) -> None:
    with pytest.raises(KeyRegistryError, match=f"^{code}$"):
        registry.issue("remote", origin, scopes=scopes)  # type: ignore[arg-type]


@pytest.mark.parametrize("trigger", ["corrupt", "removed", "closed", "verifier"])
def test_database_and_error_fail_closed(
    registry: KeyRegistry, tmp_path: Path, trigger: str
) -> None:
    issued = registry.issue("remote", Origin.REMOTE)
    path = tmp_path / "keys.sqlite3"
    if trigger == "corrupt":
        path.write_bytes(b"not sqlite")
    elif trigger == "removed":
        path.unlink()
    elif trigger == "closed":
        registry.close()
    else:
        with sqlite3.connect(path) as db:
            db.execute("UPDATE api_keys SET verifier=?", ("x" * 32,))
    assert registry.authenticate("Bearer " + issued.token) is None
    assert path.exists() == (trigger != "removed")
    expected = {"corrupt": "key_registry_unavailable", "closed": "key_registry_closed"}
    if trigger in expected:
        with pytest.raises(KeyRegistryError) as error:
            registry.list_keys()
        assert str(error.value) == expected[trigger]
        assert issued.token not in repr(error.value)
        assert str(tmp_path) not in repr(error.value)


@pytest.mark.skipif(os.name != "posix", reason="POSIX file modes only")
def test_file_permissions(registry: KeyRegistry, tmp_path: Path) -> None:
    assert stat.S_IMODE((tmp_path / "keys.sqlite3").stat().st_mode) == 0o600
    issued = registry.issue("remote", Origin.REMOTE)
    (tmp_path / "keys.sqlite3").chmod(0o644)
    assert registry.authenticate("Bearer " + issued.token) is None
    with pytest.raises(KeyRegistryError, match="^unsafe_key_registry_permissions$"):
        registry.list_keys()
    open_parent = tmp_path / "open"
    open_parent.mkdir()
    open_parent.chmod(0o777)
    with pytest.raises(KeyRegistryError, match="^unsafe_key_registry_permissions$"):
        KeyRegistry(open_parent / "keys.sqlite3")


@pytest.mark.parametrize("kind", ["symlink", "parent", "hardlink", "traversal", "journal"])
def test_symlink_registry_rejected(tmp_path: Path, sources: SyntheticSources, kind: str) -> None:
    real = tmp_path / "real.sqlite3"
    real.touch(mode=0o600)
    try:
        if kind == "symlink":
            (tmp_path / "link.sqlite3").symlink_to(real)
        elif kind == "parent":
            (tmp_path / "real").mkdir()
            (tmp_path / "link").symlink_to(tmp_path / "real", target_is_directory=True)
        elif kind == "hardlink":
            os.link(real, tmp_path / "link.sqlite3")
        elif kind == "journal":
            real.unlink()
            (tmp_path / "innocent").write_bytes(b"unchanged")
            (tmp_path / "keys.sqlite3-journal").symlink_to(tmp_path / "innocent")
    except (OSError, NotImplementedError):
        pytest.skip("Link creation not available")
    path = {
        "symlink": tmp_path / "link.sqlite3",
        "parent": tmp_path / "link" / "keys.sqlite3",
        "hardlink": tmp_path / "link.sqlite3",
        "traversal": tmp_path / ".." / "keys.sqlite3",
        "journal": tmp_path / "keys.sqlite3",
    }[kind]
    with pytest.raises(KeyRegistryError, match="^unsafe_key_registry_path$"):
        if kind == "journal":
            KeyRegistry(path, random_bytes=sources.random_bytes).issue("remote", Origin.REMOTE)
        else:
            KeyRegistry(path, random_bytes=sources.random_bytes)
    if kind == "symlink":
        assert real.read_bytes() == b""
    elif kind == "parent":
        assert not (tmp_path / "real" / "keys.sqlite3").exists()
    elif kind == "journal":
        assert (tmp_path / "innocent").read_bytes() == b"unchanged"


def test_invalid_key_ids_never_echo(registry: KeyRegistry) -> None:
    private = "synthetic-private-not-a-key-id"
    for method in (registry.revoke, registry.rotate):
        with pytest.raises(KeyRegistryError) as error:
            method(private)
        assert str(error.value) == "invalid_key_id"
        assert private not in repr(error.value)


def test_rotation_late_failure_rolls_back_both_changes(
    registry: KeyRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    old = registry.issue("remote", Origin.REMOTE)
    audit = registry._audit

    def fail_late(db: sqlite3.Connection, event: str, key_id: str, at: float) -> None:
        if event == "key_rotated":
            raise KeyRegistryError("synthetic_audit_failure")
        audit(db, event, key_id, at)  # type: ignore[arg-type]

    monkeypatch.setattr(registry, "_audit", fail_late)
    with pytest.raises(KeyRegistryError, match="^synthetic_audit_failure$"):
        registry.rotate(old.metadata.key_id)
    assert registry.authenticate("Bearer " + old.token) is not None
    assert registry.list_keys() == [old.metadata]
    assert len(registry.audit_events()) == 1


def test_schema_version_not_silently_downgraded(tmp_path: Path) -> None:
    path = tmp_path / "keys.sqlite3"
    path.touch(mode=0o600)
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA user_version=2")
    with pytest.raises(KeyRegistryError, match="^unsupported_key_registry_version$"):
        KeyRegistry(path)
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 2
        assert db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall() == []


def test_open_existing_never_creates_missing_database(
    registry: KeyRegistry, tmp_path: Path, sources: SyntheticSources
) -> None:
    path = tmp_path / "absent.sqlite3"
    with pytest.raises(KeyRegistryError, match="^key_registry_unavailable$"):
        KeyRegistry(path, create=False)
    assert not path.exists()
    empty = tmp_path / "empty.sqlite3"
    empty.touch(mode=0o600)
    with pytest.raises(KeyRegistryError, match="^unsupported_key_registry_version$"):
        KeyRegistry(empty, create=False)
    assert empty.read_bytes() == b""
    issued = registry.issue("remote", Origin.REMOTE)
    existing = tmp_path / "keys.sqlite3"
    before = existing.read_bytes()
    reopened = KeyRegistry(existing, clock=sources.clock, create=False)
    assert reopened.authenticate("Bearer " + issued.token) is not None
    assert existing.read_bytes() == before
    reopened.close()


@pytest.mark.skipif(os.name != "posix", reason="POSIX directory ownership only")
@pytest.mark.parametrize(
    ("mode", "owner", "allowed"),
    [(0o755, 10000, False), (0o1777, 10000, False), (0o1777, None, True)],
)
def test_foreign_owned_ancestor_rejected_despite_private_leaf(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: int, owner: int | None, allowed: bool
) -> None:
    ancestor = tmp_path / "foreign-owner"
    ancestor.mkdir()
    leaf = ancestor / "private"
    leaf.mkdir(mode=0o700)
    original = Path.lstat

    def stat_with_foreign_owner(path: Path) -> os.stat_result:
        info = original(path)
        if path == ancestor:
            values = list(info)
            values[0] = stat.S_IFDIR | mode
            values[4] = 0 if owner is None else os.geteuid() + owner
            return os.stat_result(values)
        return info

    monkeypatch.setattr(Path, "lstat", stat_with_foreign_owner)
    if allowed:
        KeyRegistry(leaf / "keys.sqlite3").close()
        return
    with pytest.raises(KeyRegistryError, match="^unsafe_key_registry_permissions$"):
        KeyRegistry(leaf / "keys.sqlite3")
    assert not (leaf / "keys.sqlite3").exists()
