"""Bounded, local-operator API-key registry; no remote issuance endpoints.

Only SHA-256 verifiers of 256-bit random secrets are persisted, never live keys.
A token is returned once by issue/rotate; callers must not log or serialize it
except for an explicitly requested one-time handoff. Management methods are for
local operator commands, not MCP tools. Each authentication rereads the registry.

The OS user and the registry's directory are the trust boundary. On POSIX the
file is owner-only and writable directories owned by other users are rejected.
On Windows no private ACL guarantee is inferred from chmod: the operator must
use an OS-user-private directory. Symlinks, junctions/reparse points and hardlinks
are rejected, but this is not a defense against a hostile process running as the
same OS user or an administrator. No raw key is needed to list or revoke keys.
"""

import hashlib
import math
import os
import re
import secrets
import sqlite3
import stat
import time
from collections.abc import Callable, Collection, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from ._platform_api import posix_file_api
from .models import Origin

DEFAULT_TTL_SECONDS = 3600
MAX_TTL_SECONDS = 30 * 24 * 60 * 60
MAX_ENTRIES = 1000
_MAX_AUDIT_ENTRIES = 4000
_MAX_DB_BYTES = 8 * 1024 * 1024
_KEY_ID = re.compile(r"cpk_[a-f0-9]{32}\Z")
_TOKEN = re.compile(r"(cpk_[a-f0-9]{32})\.([a-f0-9]{64})\Z")
_LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._@()\-]{0,63}\Z")
_DUMMY_DIGEST = bytes(32)
Scope = Literal["read", "control"]
AuditEventName = Literal["key_issued", "key_revoked", "key_rotated"]


class KeyRegistryError(ValueError):
    """Fixed messages only; never include keys, labels, paths or OS errors."""


@dataclass(frozen=True)
class KeyPrincipal:
    principal_id: str
    key_id: str
    origin: Origin
    scopes: frozenset[Scope]


@dataclass(frozen=True)
class KeyMetadata(KeyPrincipal):
    label: str
    issued_at: float
    expires_at: float
    revoked_at: float | None


@dataclass(frozen=True)
class IssuedKey:
    token: str = field(repr=False)
    metadata: KeyMetadata


@dataclass(frozen=True)
class KeyAuditEvent:
    event: AuditEventName
    key_id: str
    at: float


def _scopes(scopes: Collection[str]) -> frozenset[Scope]:
    # Control also grants read, but must be explicitly requested at issuance.
    if (
        isinstance(scopes, str)
        or not scopes
        or any(scope not in ("read", "control") for scope in scopes)
    ):
        raise KeyRegistryError("invalid_key_scopes")
    result: set[Scope] = {"read"}
    if "control" in scopes:
        result.add("control")
    return frozenset(result)


def _label(label: str) -> str:
    if type(label) is not str or _LABEL.fullmatch(label) is None or label != label.strip():
        raise KeyRegistryError("invalid_key_label")
    return label


def _key_id(key_id: str) -> str:
    if type(key_id) is not str or _KEY_ID.fullmatch(key_id) is None:
        raise KeyRegistryError("invalid_key_id")
    return key_id


def _timestamp(value: object) -> float:
    if type(value) not in (float, int) or not isinstance(value, (float, int)):
        raise KeyRegistryError("invalid_key_time")
    if not math.isfinite(value) or value < 0:
        raise KeyRegistryError("invalid_key_time")
    return float(value)


def _ttl(value: int) -> int:
    if type(value) is not int or not 1 <= value <= MAX_TTL_SECONDS:
        raise KeyRegistryError("invalid_key_expiry")
    return value


def _is_link(info: os.stat_result) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def _safe_file(path: Path) -> None:
    info = path.lstat()
    if _is_link(info) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise KeyRegistryError("unsafe_key_registry_path")
    if info.st_size > _MAX_DB_BYTES:
        raise KeyRegistryError("key_registry_too_large")
    if os.name == "posix" and (
        info.st_uid != posix_file_api.geteuid() or stat.S_IMODE(info.st_mode) != 0o600
    ):
        raise KeyRegistryError("unsafe_key_registry_permissions")


def _safe_directory(path: Path) -> None:
    for directory in (*reversed(path.parents), path):
        info = directory.lstat()
        if _is_link(info) or not stat.S_ISDIR(info.st_mode):
            raise KeyRegistryError("unsafe_key_registry_path")
        if os.name == "posix":
            # Directory owners can rename descendants even when the leaf is
            # private. Only the current OS user and root are trusted owners.
            if info.st_uid not in (0, posix_file_api.geteuid()):
                raise KeyRegistryError("unsafe_key_registry_permissions")
            # A trusted sticky directory such as /tmp protects existing children.
            if info.st_mode & 0o022 and not info.st_mode & stat.S_ISVTX:
                raise KeyRegistryError("unsafe_key_registry_permissions")
    info = path.lstat()
    if os.name == "posix" and (info.st_uid != posix_file_api.geteuid() or info.st_mode & 0o022):
        raise KeyRegistryError("unsafe_key_registry_permissions")


class KeyRegistry:
    """Hash-only local storage. Defaults are cryptographic; tests may inject sources.

    Construction creates an empty DB only; it never issues a key. With create=False,
    an initialized existing DB is required and construction makes no schema writes.
    Parent directory must already exist. Existing POSIX files must have mode 0600.
    Do not use a shared/network directory. close() prevents subsequent operations.
    """

    def __init__(
        self,
        path: Path,
        *,
        clock: Callable[[], float] = time.time,
        random_bytes: Callable[[int], bytes] = secrets.token_bytes,
        max_entries: int = MAX_ENTRIES,
        create: bool = True,
    ) -> None:
        if type(create) is not bool:
            raise KeyRegistryError("invalid_key_registry_mode")
        if type(max_entries) is not int or not 1 <= max_entries <= MAX_ENTRIES:
            raise KeyRegistryError("invalid_key_registry_limit")
        if ".." in path.parts or not path.name or path.name in (".", ".."):
            raise KeyRegistryError("unsafe_key_registry_path")
        self._path = path.absolute()
        self._clock = clock
        self._random_bytes = random_bytes
        self._max_entries = max_entries
        self._closed = False
        try:
            _safe_directory(self._path.parent)
            if create and not self._path.exists() and not self._path.is_symlink():
                flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
                try:
                    descriptor = os.open(self._path, flags, 0o600)
                except FileExistsError:
                    pass  # A concurrent constructor must still pass all file checks.
                else:
                    os.close(descriptor)
            with self._connection() as db:
                version = db.execute("PRAGMA user_version").fetchone()[0]
                if version not in (0, 1):
                    raise KeyRegistryError("unsupported_key_registry_version")
                if not create:
                    if version != 1:
                        raise KeyRegistryError("unsupported_key_registry_version")
                    db.execute(
                        """SELECT key_id,verifier,label,origin,scopes,
                           issued_at,expires_at,revoked_at FROM api_keys LIMIT 0"""
                    )
                    db.execute("SELECT sequence,event,key_id,at FROM key_audit LIMIT 0")
                    return
                db.execute("PRAGMA journal_mode=DELETE")
                db.execute("PRAGMA max_page_count=2048")
                db.executescript("""
                    CREATE TABLE IF NOT EXISTS api_keys (
                        key_id TEXT PRIMARY KEY,
                        verifier BLOB NOT NULL CHECK(length(verifier)=32),
                        label TEXT NOT NULL,
                        origin TEXT NOT NULL CHECK(origin IN ('local_cli','remote_mcp')),
                        scopes TEXT NOT NULL CHECK(scopes IN ('read','control,read')),
                        issued_at REAL NOT NULL,
                        expires_at REAL NOT NULL,
                        revoked_at REAL,
                        CHECK(expires_at > issued_at),
                        CHECK(expires_at - issued_at <= 2592000)
                    );
                    CREATE TABLE IF NOT EXISTS key_audit (
                        sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                        event TEXT NOT NULL CHECK(event IN
                            ('key_issued','key_revoked','key_rotated')),
                        key_id TEXT NOT NULL,
                        at REAL NOT NULL
                    );
                    PRAGMA user_version=1;
                """)
        except (OSError, sqlite3.Error):
            raise KeyRegistryError("key_registry_unavailable") from None

    def __repr__(self) -> str:
        return "KeyRegistry(<local hash-only registry>)"

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        if self._closed:
            raise KeyRegistryError("key_registry_closed")
        db: sqlite3.Connection | None = None
        try:
            _safe_directory(self._path.parent)
            _safe_file(self._path)
            for suffix in ("-journal", "-wal", "-shm"):
                sidecar = self._path.with_name(self._path.name + suffix)
                try:
                    _safe_file(sidecar)
                except FileNotFoundError:
                    pass
            # mode=rw must not silently create a replacement after file removal.
            db = sqlite3.connect(self._path.as_uri() + "?mode=rw", uri=True, timeout=2.0)
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA trusted_schema=OFF")
            db.execute("PRAGMA synchronous=FULL")
            yield db
        except (OSError, sqlite3.Error):
            raise KeyRegistryError("key_registry_unavailable") from None
        finally:
            if db is not None:
                db.close()

    def _now(self) -> float:
        return _timestamp(self._clock())

    def _random(self, size: int) -> bytes:
        value = self._random_bytes(size)
        if type(value) is not bytes or len(value) != size:
            raise KeyRegistryError("invalid_key_random_source")
        return value

    @staticmethod
    def _metadata(row: sqlite3.Row) -> KeyMetadata:
        try:
            identifier = _key_id(row["key_id"])
            label = _label(row["label"])
            origin = Origin(row["origin"])
            encoded_scopes = row["scopes"]
            if encoded_scopes not in ("read", "control,read"):
                raise KeyRegistryError("invalid_key_scopes")
            scopes = _scopes(encoded_scopes.split(","))
            issued_at = _timestamp(row["issued_at"])
            expires_at = _timestamp(row["expires_at"])
            revoked_at = None if row["revoked_at"] is None else _timestamp(row["revoked_at"])
            if not 0 < expires_at - issued_at <= MAX_TTL_SECONDS:
                raise KeyRegistryError("invalid_key_expiry")
            return KeyMetadata(
                principal_id=f"key:{identifier}",
                key_id=identifier,
                origin=origin,
                scopes=scopes,
                label=label,
                issued_at=issued_at,
                expires_at=expires_at,
                revoked_at=revoked_at,
            )
        except (ValueError, TypeError, KeyError, IndexError):
            raise KeyRegistryError("invalid_key_registry_record") from None

    @staticmethod
    def _audit(db: sqlite3.Connection, event: AuditEventName, key_id: str, at: float) -> None:
        db.execute("INSERT INTO key_audit(event,key_id,at) VALUES (?,?,?)", (event, key_id, at))
        db.execute(
            """DELETE FROM key_audit WHERE sequence NOT IN
               (SELECT sequence FROM key_audit ORDER BY sequence DESC LIMIT ?)""",
            (_MAX_AUDIT_ENTRIES,),
        )

    def _issue(
        self,
        db: sqlite3.Connection,
        label: str,
        origin: Origin,
        scopes: frozenset[Scope],
        now: float,
        ttl_seconds: int,
    ) -> IssuedKey:
        count = db.execute("SELECT count(*) FROM api_keys").fetchone()[0]
        if count >= self._max_entries:
            raise KeyRegistryError("key_registry_full")
        for _ in range(4):
            identifier = "cpk_" + self._random(16).hex()
            if (
                db.execute("SELECT 1 FROM api_keys WHERE key_id=?", (identifier,)).fetchone()
                is None
            ):
                break
        else:
            raise KeyRegistryError("key_identifier_collision")
        token = identifier + "." + self._random(32).hex()
        verifier = hashlib.sha256(token.encode("ascii")).digest()
        metadata = KeyMetadata(
            principal_id=f"key:{identifier}",
            key_id=identifier,
            origin=origin,
            scopes=scopes,
            label=label,
            issued_at=now,
            expires_at=now + ttl_seconds,
            revoked_at=None,
        )
        db.execute(
            """INSERT INTO api_keys
               (key_id,verifier,label,origin,scopes,issued_at,expires_at,revoked_at)
               VALUES (?,?,?,?,?,?,?,NULL)""",
            (
                identifier,
                verifier,
                label,
                origin.value,
                ",".join(sorted(scopes)),
                now,
                metadata.expires_at,
            ),
        )
        self._audit(db, "key_issued", identifier, now)
        return IssuedKey(token=token, metadata=metadata)

    def issue(
        self,
        label: str,
        origin: Origin,
        *,
        scopes: Collection[str] = frozenset({"read"}),
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
    ) -> IssuedKey:
        """Explicit local issuance only. Return raw token once; never save or log it."""
        label = _label(label)
        if not isinstance(origin, Origin):
            raise KeyRegistryError("invalid_key_origin")
        validated_scopes = _scopes(scopes)
        _ttl(ttl_seconds)
        now = self._now()
        with self._connection() as db, db:
            db.execute("BEGIN IMMEDIATE")
            return self._issue(db, label, origin, validated_scopes, now, ttl_seconds)

    def authenticate(self, header: str | None) -> KeyPrincipal | None:
        """Exact Bearer header; unknown/expired/revoked keys all return None.

        Availability, corruption and path-check failures fail closed. Callers must
        not fall back to an unauthenticated principal when None is returned.
        """
        if type(header) is not str or not header.startswith("Bearer "):
            return None
        match = _TOKEN.fullmatch(header[7:])
        if match is None:
            return None
        try:
            now = self._now()
            with self._connection() as db:
                row = db.execute("SELECT * FROM api_keys WHERE key_id=?", (match[1],)).fetchone()
            expected = _DUMMY_DIGEST if row is None else row["verifier"]
            if type(expected) is not bytes or len(expected) != 32:
                return None
            actual = hashlib.sha256(header[7:].encode("ascii")).digest()
            valid = secrets.compare_digest(actual, expected)
            if not valid or row is None:
                return None
            metadata = self._metadata(row)
            if (
                metadata.revoked_at is not None
                or not metadata.issued_at <= now < metadata.expires_at
            ):
                return None
            return KeyPrincipal(
                metadata.principal_id, metadata.key_id, metadata.origin, metadata.scopes
            )
        except KeyRegistryError:
            return None

    def list_keys(self) -> list[KeyMetadata]:
        """Metadata only, including expired/revoked entries. No verifier or token."""
        with self._connection() as db:
            rows = db.execute(
                "SELECT * FROM api_keys ORDER BY issued_at,key_id LIMIT ?", (MAX_ENTRIES + 1,)
            ).fetchall()
        if len(rows) > MAX_ENTRIES:
            raise KeyRegistryError("key_registry_full")
        return [self._metadata(row) for row in rows]

    def revoke(self, key_id: str) -> bool:
        """Idempotent, explicit local revocation. True only when a key changed."""
        identifier = _key_id(key_id)
        now = self._now()
        with self._connection() as db, db:
            db.execute("BEGIN IMMEDIATE")
            cursor = db.execute(
                "UPDATE api_keys SET revoked_at=? WHERE key_id=? AND revoked_at IS NULL",
                (now, identifier),
            )
            if cursor.rowcount != 1:
                return False
            self._audit(db, "key_revoked", identifier, now)
            return True

    def rotate(self, key_id: str, *, ttl_seconds: int = DEFAULT_TTL_SECONDS) -> IssuedKey:
        """Explicitly replace an active key and revoke its predecessor atomically.

        No automatic rotation. Scope/origin/label are retained; the replacement
        gets a different principal. Failure rolls back both issuance and revocation.
        """
        identifier = _key_id(key_id)
        _ttl(ttl_seconds)
        now = self._now()
        with self._connection() as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM api_keys WHERE key_id=?", (identifier,)).fetchone()
            if row is None:
                raise KeyRegistryError("key_not_active")
            old = self._metadata(row)
            if old.revoked_at is not None or not old.issued_at <= now < old.expires_at:
                raise KeyRegistryError("key_not_active")
            issued = self._issue(db, old.label, old.origin, old.scopes, now, ttl_seconds)
            db.execute("UPDATE api_keys SET revoked_at=? WHERE key_id=?", (now, identifier))
            self._audit(db, "key_rotated", identifier, now)
            return issued

    def audit_events(self, *, limit: int = 100) -> list[KeyAuditEvent]:
        if type(limit) is not int or not 1 <= limit <= _MAX_AUDIT_ENTRIES:
            raise KeyRegistryError("invalid_key_audit_limit")
        with self._connection() as db:
            rows = db.execute(
                "SELECT event,key_id,at FROM key_audit ORDER BY sequence DESC LIMIT ?", (limit,)
            ).fetchall()
        events: list[KeyAuditEvent] = []
        for row in rows:
            event = row["event"]
            if event not in ("key_issued", "key_revoked", "key_rotated"):
                raise KeyRegistryError("invalid_key_registry_record")
            events.append(KeyAuditEvent(event, _key_id(row["key_id"]), _timestamp(row["at"])))
        return events

    def close(self) -> None:
        self._closed = True
