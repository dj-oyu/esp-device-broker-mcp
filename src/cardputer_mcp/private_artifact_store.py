"""Disconnected private artifact store; no upload/download or runtime migration.

Use a dedicated connection to the broker's protected database containing jobs and
plans. The caller must serialize accepted cancellation against final guard/commit
and attest all build processes are joined. This class grants no execution rights.
"""

import hashlib
import json
import sqlite3
from collections.abc import Callable
from dataclasses import asdict
from typing import Any

from .private_build_output import private_artifact_provenance
from .private_input_contract import InputFile, _hash, _inventory


def _canonical(value: Any) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    )


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("ascii")).hexdigest()


class PrivateArtifactStore:
    MAX_FILES = 32
    MAX_BYTES = 128 * 1024 * 1024

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.db = connection
        self._idle()
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS private_artifact_receipts (
                receipt TEXT PRIMARY KEY, job_id TEXT NOT NULL UNIQUE, record TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS private_artifact_bytes (
                receipt TEXT NOT NULL, path TEXT NOT NULL, payload BLOB NOT NULL,
                PRIMARY KEY(receipt,path)
            );
            CREATE TABLE IF NOT EXISTS private_artifact_pins (
                receipt TEXT NOT NULL, reference TEXT NOT NULL, PRIMARY KEY(receipt,reference)
            );
        """)

    def _idle(self) -> None:
        if self.db.in_transaction:
            raise ValueError("private_artifact_transaction_active")

    def _rollback(self) -> None:
        if self.db.in_transaction:
            self.db.execute("ROLLBACK")

    def _manifest(self, files: tuple[tuple[str, bytes], ...]) -> list[dict[str, Any]]:
        if (
            not files
            or len(files) > self.MAX_FILES
            or any(type(data) is not bytes for _, data in files)
            or sum(len(data) for _, data in files) > self.MAX_BYTES
        ):
            raise ValueError("private_artifact_limit")
        values = tuple(InputFile(p, len(b), hashlib.sha256(b).hexdigest()) for p, b in files)
        _inventory(values)
        return [asdict(value) for value in sorted(values, key=lambda value: value.path)]

    def publish(
        self,
        *,
        plan: dict[str, Any],
        principal: str,
        composition_identity: str,
        files: tuple[tuple[str, bytes], ...],
        guard: Callable[[], bool],
        runtime_bookkeeping: bool = False,
    ) -> str:
        self._idle()
        plan = json.loads(_canonical(plan))
        if plan.get("test") != "firmware_build" or not plan.get("job_id"):
            raise ValueError("private_artifact_job_invalid")
        provenance = private_artifact_provenance(
            plan, principal=principal, composition_identity=composition_identity
        )
        payload = _canonical(plan)
        plan_hash = _digest(payload)
        record = _canonical(
            {
                "schema": "private-build-artifact-v1",
                "job_id": plan["job_id"],
                "principal": principal,
                "plan_hash": plan_hash,
                "private_build": provenance,
                "files": self._manifest(files),
            }
        )
        receipt = _digest(record)
        self.db.execute("BEGIN IMMEDIATE")
        try:
            job = self.db.execute(
                "SELECT j.state,j.plan_hash,p.payload FROM jobs j "
                "JOIN plans p ON p.hash=j.plan_hash WHERE j.id=?",
                (plan["job_id"],),
            ).fetchone()
            if (
                job is None
                or job[0] != "running"
                or job[1] != plan_hash
                or _canonical(json.loads(job[2])) != payload
                or guard() is not True
            ):
                raise ValueError("private_artifact_final_commit_denied")
            self.db.execute(
                "INSERT INTO private_artifact_receipts VALUES (?,?,?)",
                (receipt, plan["job_id"], record),
            )
            self.db.executemany(
                "INSERT INTO private_artifact_bytes VALUES (?,?,?)",
                ((receipt, p, b) for p, b in files),
            )
            if runtime_bookkeeping:
                # No nested Store/ledger methods: they commit independently.
                self.db.execute(
                    "INSERT INTO execution_audit(job_id,plan_hash,principal_id,event) "
                    "VALUES(?,?,?,?)",
                    (plan["job_id"], plan_hash, principal, "private_build_committed"),
                )
                self.db.execute(
                    "INSERT INTO private_artifact_pins VALUES (?,?)",
                    (receipt, "recovery:" + plan["job_id"]),
                )
            # The runtime live check requires running. Keep the final callback
            # before the terminal transition; remaining validation/SQL is synchronous,
            # with no external callbacks.
            if guard() is not True:
                raise ValueError("private_artifact_final_commit_denied")
            self.db.execute(
                "UPDATE jobs SET state='succeeded',result='private_build_succeeded_no_flash',"
                "finished=CURRENT_TIMESTAMP WHERE id=? AND state='running'",
                (plan["job_id"],),
            )
            self.resolve(receipt=receipt, principal=principal)
            self.db.execute("COMMIT")
        except BaseException:
            self._rollback()
            raise
        return receipt

    def resolve(self, *, receipt: str, principal: str) -> tuple[tuple[str, bytes], ...]:
        _hash(receipt)
        row = self.db.execute(
            "SELECT record,job_id FROM private_artifact_receipts WHERE receipt=?", (receipt,)
        ).fetchone()
        if row is None or _digest(row[0]) != receipt:
            raise ValueError("private_artifact_unavailable")
        record = json.loads(row[0])
        if not principal or record.get("principal") != principal:
            raise ValueError("private_artifact_not_owned")
        job = self.db.execute("SELECT state,plan_hash FROM jobs WHERE id=?", (row[1],)).fetchone()
        if (
            record.get("schema") != "private-build-artifact-v1"
            or record.get("job_id") != row[1]
            or job is None
            or tuple(job) != ("succeeded", record.get("plan_hash"))
        ):
            raise ValueError("private_artifact_job_changed")
        count, total = self.db.execute(
            "SELECT COUNT(*),COALESCE(SUM(LENGTH(payload)),0) "
            "FROM private_artifact_bytes WHERE receipt=?",
            (receipt,),
        ).fetchone()
        if count > self.MAX_FILES or total > self.MAX_BYTES:
            raise ValueError("private_artifact_limit")
        files = tuple(
            (str(row[0]), bytes(row[1]))
            for row in self.db.execute(
                "SELECT path,payload FROM private_artifact_bytes WHERE receipt=? ORDER BY path",
                (receipt,),
            ).fetchall()
        )
        if self._manifest(files) != record["files"]:
            raise ValueError("private_artifact_bytes_changed")
        return files

    def receipt_for_job(self, *, job_id: str, principal: str) -> str:
        """Readback/reconciliation after uncertain publication; never replay writes."""
        row = self.db.execute(
            "SELECT receipt FROM private_artifact_receipts WHERE job_id=?", (job_id,)
        ).fetchone()
        if row is None:
            raise ValueError("private_artifact_unavailable")
        self.resolve(receipt=row[0], principal=principal)
        return str(row[0])

    def pin(self, *, receipt: str, principal: str, reference: str) -> None:
        """Internal active/recovery reference; retaining is never implicit release."""
        self._idle()
        if not reference or len(reference) > 128:
            raise ValueError("private_artifact_pin_invalid")
        self.db.execute("BEGIN IMMEDIATE")
        try:
            self.resolve(receipt=receipt, principal=principal)
            self.db.execute(
                "INSERT OR IGNORE INTO private_artifact_pins VALUES (?,?)", (receipt, reference)
            )
            self.db.execute("COMMIT")
        except BaseException:
            self._rollback()
            raise

    def discard_unpinned(self, *, receipt: str, principal: str) -> bool:
        """Retention primitive, not remote deletion. Never remove a pinned result."""
        self._idle()
        self.db.execute("BEGIN IMMEDIATE")
        try:
            self.resolve(receipt=receipt, principal=principal)
            if self.db.execute(
                "SELECT 1 FROM private_artifact_pins WHERE receipt=? LIMIT 1", (receipt,)
            ).fetchone():
                self.db.execute("COMMIT")
                return False
            self.db.execute("DELETE FROM private_artifact_bytes WHERE receipt=?", (receipt,))
            self.db.execute("DELETE FROM private_artifact_receipts WHERE receipt=?", (receipt,))
            self.db.execute("COMMIT")
            return True
        except BaseException:
            self._rollback()
            raise
