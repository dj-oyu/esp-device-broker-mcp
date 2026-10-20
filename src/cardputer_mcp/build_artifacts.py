"""Internal successful-build references; physical layout remains builder-owned."""

import copy
import hashlib
import json
import sqlite3
import threading
from contextlib import closing
from pathlib import Path
from typing import Any

from .diagnostic_registry import same_diagnostic_selection, same_entry_binding


def manifest_digest(manifest: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class BuildArtifacts:
    def __init__(self, history: Path | None = None) -> None:
        self.history = history
        self._records: dict[str, tuple[str, Path, dict[str, Any]]] = {}
        self._lock = threading.RLock()

    def _connect(self, mode: str) -> sqlite3.Connection:
        if self.history is None:
            raise ValueError("owned_build_artifact_not_available")
        return sqlite3.connect(self.history.resolve().as_uri() + "?mode=" + mode, uri=True)

    @staticmethod
    def _plan(
        db: sqlite3.Connection, job_id: str, principal: str
    ) -> tuple[str, dict[str, Any], str]:
        row = db.execute(
            "SELECT j.plan_hash,p.payload,j.state FROM jobs j JOIN plans p ON p.hash=j.plan_hash "
            "WHERE j.id=?",
            (job_id,),
        ).fetchone()
        if row is None:
            raise ValueError("owned_build_artifact_not_available")
        plan = json.loads(row[1])
        if (
            not principal
            or plan.get("build_principal") != principal
            or plan.get("test") != "firmware_build"
            or plan.get("job_id") != job_id
        ):
            raise ValueError("owned_build_artifact_not_available")
        return row[0], plan, row[2]

    def publish(self, job_id: str, principal: str, root: Path, manifest: dict[str, Any]) -> None:
        if not principal or not root.is_dir() or manifest.get("flash_authorized") is not False:
            raise ValueError("invalid_internal_artifact_receipt")
        with self._lock:
            if self.history is not None:
                # Workers use a short-lived connection, never the loop-owned Store connection.
                # The reference is inert until the existing job success commit completes.
                with closing(self._connect("rw")) as db, db:
                    digest, plan, state = self._plan(db, job_id, principal)
                    if (
                        state != "running"
                        or manifest.get("build_job_id") != job_id
                        or manifest.get("build_plan_hash") != digest
                        or manifest.get("fingerprint") != plan.get("fingerprint")
                        or not same_diagnostic_selection(manifest, plan)
                    ):
                        raise ValueError("invalid_internal_artifact_receipt")
                    reference = {
                        "project_id": plan["project_id"],
                        "build_plan_hash": digest,
                        "fingerprint": manifest["fingerprint"],
                        "environment_fingerprint": manifest["environment_fingerprint"],
                        "build_location": manifest.get("build_location", {"layout_version": 1}),
                        "manifest_sha256": manifest_digest(manifest),
                        "root_sha256": hashlib.sha256(str(root.resolve()).encode()).hexdigest(),
                    }
                    if "diagnostic_entry_sha256" in plan:
                        reference["diagnostic_entry_sha256"] = plan["diagnostic_entry_sha256"]
                    payload = json.dumps(reference, sort_keys=True, separators=(",", ":"))
                    if len(payload.encode()) > 4096:
                        raise ValueError("invalid_internal_artifact_receipt")
                    db.execute(
                        "INSERT INTO build_artifact_references VALUES (?,?,?)",
                        (job_id, principal, payload),
                    )
            while len(self._records) >= 8:
                self._records.pop(next(iter(self._records)))
            self._records[job_id] = principal, root.resolve(), copy.deepcopy(manifest)

    def reference(self, job_id: str, principal: str, project_id: str) -> dict[str, Any]:
        if self.history is None:
            raise ValueError("owned_build_artifact_not_available")
        with closing(self._connect("ro")) as db:
            digest, plan, state = self._plan(db, job_id, principal)
            row = db.execute(
                "SELECT payload FROM build_artifact_references WHERE job_id=? AND principal=?",
                (job_id, principal),
            ).fetchone()
            if (
                state != "succeeded"
                or plan.get("project_id") != project_id
                or row is None
                or len(row[0].encode()) > 4096
            ):
                raise ValueError("owned_build_artifact_not_available")
            reference = json.loads(row[0])
            if (
                not isinstance(reference, dict)
                or reference.get("project_id") != project_id
                or reference.get("build_plan_hash") != digest
                or reference.get("fingerprint") != plan.get("fingerprint")
                or not same_entry_binding(reference, plan)
            ):
                raise ValueError("owned_build_artifact_not_available")
            return reference

    def discard(self, job_id: str) -> None:
        with self._lock:
            if self.history is not None:
                with closing(self._connect("rw")) as db, db:
                    db.execute(
                        "DELETE FROM build_artifact_references WHERE job_id=? AND NOT EXISTS "
                        "(SELECT 1 FROM jobs WHERE id=? AND state='succeeded')",
                        (job_id, job_id),
                    )
            self._records.pop(job_id, None)

    def resolve(self, job_id: str, principal: str) -> tuple[Path, dict[str, Any]]:
        with self._lock:
            record = self._records.get(job_id)
            if record is None or record[0] != principal:
                raise ValueError("owned_build_artifact_not_available")
            return record[1], copy.deepcopy(record[2])
