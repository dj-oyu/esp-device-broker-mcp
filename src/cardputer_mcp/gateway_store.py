"""Private SQLite store for the gateway's Claude connections, so a restart keeps them.

Only connections the owner completed (a Google login that produced tokens) are
written: the client registration, the grant (subject, scopes, resource) and the
SHA-256 digest of each access and refresh token with its expiry. Raw tokens,
authorization codes, pending logins and never-used registrations stay in memory.

The registration record includes the client secret that DCR issued, because the
MCP SDK compares it as plain text at ``/token``. The secret alone grants nothing:
a refresh needs the refresh token, and only its digest is stored. The file lives
next to the gateway's broker key file, in the same private directory.
"""

import os
import sqlite3
import stat
from dataclasses import dataclass
from pathlib import Path

from mcp.shared.auth import OAuthClientInformationFull

from ._platform_api import posix_file_api

FILE_NAME = "gateway-grants.sqlite3"
SCHEMA_VERSION = 1
_SCHEMA = """
CREATE TABLE IF NOT EXISTS clients (
    client_id TEXT PRIMARY KEY,
    info TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS grants (
    id INTEGER PRIMARY KEY,
    client_id TEXT NOT NULL REFERENCES clients(client_id) ON DELETE CASCADE,
    subject TEXT NOT NULL,
    scopes TEXT NOT NULL,
    resource TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tokens (
    digest TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('access', 'refresh')),
    grant_id INTEGER NOT NULL REFERENCES grants(id) ON DELETE CASCADE,
    expires_at INTEGER NOT NULL
);
"""


class GatewayStoreError(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class StoredToken:
    grant_id: int
    client_id: str
    subject: str
    scopes: list[str]
    resource: str
    expires_at: int


def default_store_path(broker_env_file: Path) -> Path:
    return broker_env_file.parent / FILE_NAME


def _check_location(path: Path) -> None:
    if not path.is_absolute() or ".." in path.parts:
        raise GatewayStoreError("gateway_store_unsafe_path")
    try:
        parent = path.parent.lstat()
    except OSError:
        raise GatewayStoreError("gateway_store_unavailable") from None
    if not stat.S_ISDIR(parent.st_mode) or path.parent.is_symlink():
        raise GatewayStoreError("gateway_store_unsafe_path")
    if os.name == "posix" and (parent.st_uid != posix_file_api.geteuid() or parent.st_mode & 0o022):
        raise GatewayStoreError("gateway_store_unsafe_path")
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise GatewayStoreError("gateway_store_unsafe_path")
    if not path.exists():
        # Created before SQLite touches it so POSIX gets 0600; Windows inherits the
        # private directory's ACL like the key files beside it.
        os.close(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
    elif os.name == "posix":
        info = path.stat()
        if info.st_uid != posix_file_api.geteuid() or info.st_mode & 0o077:
            raise GatewayStoreError("gateway_store_unsafe_path")


class GatewayStore:
    """Synchronous and single-threaded: the gateway calls it from its event loop."""

    def __init__(self, path: Path | None, *, subject: str, resource: str) -> None:
        if path is not None:
            _check_location(path)
        try:
            self._db = sqlite3.connect(":memory:" if path is None else str(path))
        except sqlite3.Error:
            raise GatewayStoreError("gateway_store_unavailable") from None
        try:
            self._db.execute("PRAGMA foreign_keys = ON")
            version = self._db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, SCHEMA_VERSION):
                raise GatewayStoreError("gateway_store_version_unsupported")
            with self._db:
                self._db.executescript(_SCHEMA)
                self._db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                # A different owner or public URL invalidates every stored connection.
                self._db.execute(
                    "DELETE FROM grants WHERE subject != ? OR resource != ?", (subject, resource)
                )
                self._drop_orphans()
        except (sqlite3.Error, GatewayStoreError) as error:
            self._db.close()
            if isinstance(error, GatewayStoreError):
                raise
            raise GatewayStoreError("gateway_store_unavailable") from None

    def close(self) -> None:
        self._db.close()

    # Reads ------------------------------------------------------------------------

    def clients(self) -> dict[str, OAuthClientInformationFull]:
        rows = self._db.execute("SELECT client_id, info FROM clients").fetchall()
        return {cid: OAuthClientInformationFull.model_validate_json(info) for cid, info in rows}

    def token(self, digest: str, kind: str) -> StoredToken | None:
        row = self._db.execute(
            "SELECT g.id, g.client_id, g.subject, g.scopes, g.resource, t.expires_at"
            " FROM tokens t JOIN grants g ON g.id = t.grant_id"
            " WHERE t.digest = ? AND t.kind = ?",
            (digest, kind),
        ).fetchone()
        if row is None:
            return None
        grant_id, client_id, subject, scopes, resource, expires_at = row
        return StoredToken(grant_id, client_id, subject, scopes.split(), resource, expires_at)

    def grant_of(self, digest: str) -> int | None:
        row = self._db.execute("SELECT grant_id FROM tokens WHERE digest = ?", (digest,)).fetchone()
        return None if row is None else int(row[0])

    def has_grant(self, client_id: str) -> bool:
        row = self._db.execute("SELECT 1 FROM grants WHERE client_id = ?", (client_id,)).fetchone()
        return row is not None

    # Writes -----------------------------------------------------------------------

    def create_grant(
        self,
        client: OAuthClientInformationFull,
        subject: str,
        scopes: list[str],
        resource: str,
    ) -> int:
        with self._db:
            self._db.execute(
                "INSERT INTO clients (client_id, info) VALUES (?, ?)"
                " ON CONFLICT (client_id) DO UPDATE SET info = excluded.info",
                (client.client_id, client.model_dump_json()),
            )
            cursor = self._db.execute(
                "INSERT INTO grants (client_id, subject, scopes, resource) VALUES (?, ?, ?, ?)",
                (client.client_id, subject, " ".join(scopes), resource),
            )
        assert cursor.lastrowid is not None
        return cursor.lastrowid

    def issue(
        self,
        grant_id: int,
        scopes: list[str],
        access: tuple[str, int],
        refresh: tuple[str, int],
        old_refresh: str | None = None,
    ) -> bool:
        """Store a new token pair; with ``old_refresh``, only if that token still exists."""
        with self._db:
            if old_refresh is not None:
                used = self._db.execute(
                    "DELETE FROM tokens WHERE digest = ? AND kind = 'refresh' AND grant_id = ?",
                    (old_refresh, grant_id),
                )
                if used.rowcount != 1:
                    return False
            self._db.execute(
                "UPDATE grants SET scopes = ? WHERE id = ?", (" ".join(scopes), grant_id)
            )
            self._db.executemany(
                "INSERT INTO tokens (digest, kind, grant_id, expires_at) VALUES (?, ?, ?, ?)",
                [
                    (access[0], "access", grant_id, access[1]),
                    (refresh[0], "refresh", grant_id, refresh[1]),
                ],
            )
        return True

    def revoke_grant(self, grant_id: int) -> None:
        with self._db:
            self._db.execute("DELETE FROM grants WHERE id = ?", (grant_id,))
            self._drop_orphans()

    def purge(self, now: float) -> None:
        with self._db:
            self._db.execute("DELETE FROM tokens WHERE expires_at < ?", (now,))
            self._drop_orphans()

    def _drop_orphans(self) -> None:
        # A grant lives as long as one of its refresh tokens; a stored client as long
        # as one of its grants.
        self._db.execute(
            "DELETE FROM grants WHERE id NOT IN"
            " (SELECT grant_id FROM tokens WHERE kind = 'refresh')"
        )
        self._db.execute(
            "DELETE FROM clients WHERE client_id NOT IN (SELECT client_id FROM grants)"
        )
