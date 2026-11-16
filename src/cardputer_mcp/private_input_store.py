"""Transactional private-input storage for an authorized ingestion adapter.

The connection must point to broker-owned private storage. This is not a remote
upload API and does not open registered roots. A future no-follow reader supplies
bounded file bytes; neither receipt hashes nor this class grant import authority.
"""

import hashlib
import json
import sqlite3
from collections.abc import Callable, Iterable
from dataclasses import asdict

from .private_input_contract import InputFile, _hash, _inventory
from .private_input_registration import PrivateInputRegistration


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def inventory_digest(files: tuple[InputFile, ...]) -> str:
    _inventory(files)
    raw = _json([asdict(file) for file in sorted(files, key=lambda file: file.path)])
    return hashlib.sha256(raw.encode("ascii")).hexdigest()


class PrivateInputReader:
    """Read existing tables only; construction never creates schema or ingests bytes."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def resolve(
        self,
        *,
        receipt: str,
        registration: PrivateInputRegistration,
        project_id: str,
        input_id: str,
        principal: str,
    ) -> tuple[tuple[str, bytes], ...]:
        _hash(receipt)
        row = self.connection.execute(
            "SELECT record FROM private_input_receipts WHERE receipt=?", (receipt,)
        ).fetchone()
        if row is None or hashlib.sha256(row[0].encode("ascii")).hexdigest() != receipt:
            raise ValueError("private_input_receipt_unavailable")
        record = json.loads(row[0])
        if record.get("principal") != principal or not principal:
            raise ValueError("private_input_not_owned")
        if (
            record.get("project") != project_id
            or record.get("input") != input_id
            or record.get("registration")
            != registration.identity(project_id=project_id, input_id=input_id)
        ):
            raise ValueError("private_input_registration_changed")
        count, total = self.connection.execute(
            "SELECT COUNT(*),COALESCE(SUM(LENGTH(payload)),0) "
            "FROM private_input_bytes WHERE receipt=?",
            (receipt,),
        ).fetchone()
        if count > registration.max_files or total > registration.max_bytes:
            raise ValueError("private_input_limit")
        rows = self.connection.execute(
            "SELECT path,payload FROM private_input_bytes WHERE receipt=? ORDER BY path", (receipt,)
        ).fetchall()
        files = tuple(
            InputFile(path, len(payload), hashlib.sha256(payload).hexdigest())
            for path, payload in rows
        )
        if (
            len(files) > registration.max_files
            or sum(file.size for file in files) > registration.max_bytes
            or inventory_digest(files) != registration.inventory_sha256
            or [asdict(file) for file in files] != record.get("files")
            or record.get("schema") != "private-input-receipt-v1"
        ):
            raise ValueError("private_input_bytes_changed")
        return tuple((path, bytes(payload)) for path, payload in rows)


class PrivateInputStore(PrivateInputReader):
    """Append-only operations; a receipt is returned only after durable commit.

    A dedicated connection is required, with no caller transaction in progress.
    Cross-process coordination and the storage directory's protection belong to
    the broker integration; this class never changes permissions or credentials.
    """

    def __init__(self, connection: sqlite3.Connection) -> None:
        if connection.in_transaction:
            raise ValueError("private_input_transaction_active")
        self.connection = connection
        connection.executescript("""
            CREATE TABLE IF NOT EXISTS private_input_receipts (
                receipt TEXT PRIMARY KEY, record TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS private_input_bytes (
                receipt TEXT NOT NULL, path TEXT NOT NULL, payload BLOB NOT NULL,
                PRIMARY KEY(receipt, path)
            );
        """)

    def ingest(
        self,
        *,
        registration: PrivateInputRegistration,
        project_id: str,
        input_id: str,
        principal: str,
        files: Iterable[tuple[str, bytes]],
        cancelled: Callable[[], bool] = lambda: False,
    ) -> str:
        if not principal or not project_id or not input_id:
            raise ValueError("private_input_identity_required")
        if self.connection.in_transaction:
            raise ValueError("private_input_transaction_active")
        manifest: list[InputFile] = []
        content: list[tuple[str, bytes]] = []
        size = 0
        for path, payload in files:
            if cancelled():
                raise ValueError("private_input_cancelled")
            if type(payload) is not bytes:
                raise ValueError("private_input_bytes_required")
            size += len(payload)
            if len(manifest) >= registration.max_files or size > registration.max_bytes:
                raise ValueError("private_input_limit")
            manifest.append(InputFile(path, len(payload), hashlib.sha256(payload).hexdigest()))
            content.append((path, payload))
        digest = inventory_digest(tuple(manifest))
        if digest != registration.inventory_sha256:
            raise ValueError("private_input_inventory_mismatch")
        record = _json(
            {
                "schema": "private-input-receipt-v1",
                "project": project_id,
                "input": input_id,
                "principal": principal,
                "registration": registration.identity(project_id=project_id, input_id=input_id),
                "inventory": digest,
                "files": [asdict(file) for file in sorted(manifest, key=lambda file: file.path)],
            }
        )
        receipt = hashlib.sha256(record.encode("ascii")).hexdigest()
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            existing = self.connection.execute(
                "SELECT record FROM private_input_receipts WHERE receipt=?", (receipt,)
            ).fetchone()
            if existing is None:
                self.connection.execute(
                    "INSERT INTO private_input_receipts VALUES (?, ?)", (receipt, record)
                )
                self.connection.executemany(
                    "INSERT INTO private_input_bytes VALUES (?, ?, ?)",
                    ((receipt, path, payload) for path, payload in content),
                )
            # Existing receipts are revalidated too; a damaged store is not success.
            self.resolve(
                receipt=receipt,
                registration=registration,
                project_id=project_id,
                input_id=input_id,
                principal=principal,
            )
            if cancelled():
                raise ValueError("private_input_cancelled")
            self.connection.execute("COMMIT")
        except BaseException:
            if self.connection.in_transaction:
                self.connection.execute("ROLLBACK")
            raise
        return receipt
