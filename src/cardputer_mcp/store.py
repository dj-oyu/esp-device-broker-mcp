"""SQLite is accessed only by the resident broker (or offline operator under its lock)."""

import json
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any


class Store:
    def __init__(self, path: Path) -> None:
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS firmware_lkg_history (
            receipt TEXT PRIMARY KEY, device_id TEXT NOT NULL, principal TEXT NOT NULL,
            kind TEXT NOT NULL, baseline TEXT NOT NULL, completed_job TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS firmware_lkg (
            device_id TEXT PRIMARY KEY, principal TEXT NOT NULL,
            receipt TEXT NOT NULL, kind TEXT NOT NULL, baseline TEXT NOT NULL,
            completed_job TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS plans (
            hash TEXT PRIMARY KEY, payload TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS approvals (hash TEXT PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY, plan_hash TEXT NOT NULL, state TEXT NOT NULL,
            stage TEXT, result TEXT, diagnostic TEXT, reviewed INTEGER NOT NULL DEFAULT 0,
            created TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            finished TEXT
        );
        CREATE TABLE IF NOT EXISTS work_sessions (
            id TEXT PRIMARY KEY, epoch TEXT NOT NULL, owner TEXT NOT NULL,
            principal_id TEXT NOT NULL, scopes TEXT NOT NULL, state TEXT NOT NULL,
            reason TEXT, created TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS device_requests (
            id TEXT PRIMARY KEY, session_id TEXT, lease_id TEXT,
            state TEXT NOT NULL, lease_seconds INTEGER NOT NULL,
            reason TEXT, reviewed INTEGER NOT NULL DEFAULT 0,
            created TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            finished TEXT
        );
        """)
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(jobs)")}
        if "diagnostic" not in columns:
            self.db.execute("ALTER TABLE jobs ADD COLUMN diagnostic TEXT")
        self.db.commit()

    def recover(self) -> None:
        self.db.execute("""UPDATE jobs SET state='cancelled',
            result='broker_restart_no_replay', finished=CURRENT_TIMESTAMP
            WHERE state='awaiting_approval'""")
        self.db.execute("""UPDATE jobs SET state='needs_review',
            result='broker_restart_no_replay', finished=CURRENT_TIMESTAMP
            WHERE state IN ('queued','running')""")
        self.db.execute("""UPDATE device_requests SET state='needs_review',
            reason='broker_restart_no_replay', finished=CURRENT_TIMESTAMP
            WHERE state IN ('waiting','owning')""")
        self.db.execute("""UPDATE work_sessions SET state='needs_review',
            reason='broker_restart_no_resume' WHERE state='open'""")
        self.db.commit()

    def needs_review(self) -> bool:
        return bool(
            self.db.execute(
                """SELECT 1 FROM jobs WHERE state='needs_review' AND reviewed=0
                UNION ALL SELECT 1 FROM device_requests
                WHERE state='needs_review' AND reviewed=0 LIMIT 1"""
            ).fetchone()
        )

    def put_plan(self, digest: str, payload: dict[str, Any]) -> None:
        self.db.execute(
            "INSERT OR IGNORE INTO plans VALUES (?, ?)",
            (digest, json.dumps(payload, sort_keys=True)),
        )
        self.db.commit()

    def plan(self, digest: str) -> dict[str, Any] | None:
        row = self.db.execute("SELECT payload FROM plans WHERE hash=?", (digest,)).fetchone()
        return json.loads(row[0]) if row else None

    def job(self, job_id: str) -> dict[str, Any] | None:
        row = self.db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["diagnostic"] = json.loads(row["diagnostic"]) if row["diagnostic"] else None
        return result

    def add_job(self, job_id: str, digest: str) -> None:
        self.db.execute(
            "INSERT INTO jobs (id,plan_hash,state) VALUES (?,?,'queued')", (job_id, digest)
        )
        self.db.commit()

    def add_awaiting_job(self, job_id: str, digest: str) -> None:
        # Only the inert audit row is durable. The execution intent and its
        # monotonic deadline belong to this broker process, never SQLite.
        self.db.execute(
            "INSERT INTO jobs (id,plan_hash,state) VALUES (?,?,'awaiting_approval')",
            (job_id, digest),
        )
        self.db.commit()

    def set_job(
        self,
        job_id: str,
        state: str,
        result: str | None = None,
        diagnostic: dict[str, Any] | None = None,
    ) -> None:
        terminal = state not in ("awaiting_approval", "queued", "running")
        self.db.execute(
            """UPDATE jobs SET state=?, result=?, diagnostic=?,
            finished=CASE WHEN ? THEN CURRENT_TIMESTAMP ELSE NULL END WHERE id=?""",
            (state, result, json.dumps(diagnostic) if diagnostic else None, terminal, job_id),
        )
        self.db.commit()

    def firmware_current(self, device_id: str, principal: str) -> dict[str, Any] | None:
        row = self.db.execute(
            "SELECT * FROM firmware_lkg WHERE device_id=? AND principal=?",
            (device_id, principal),
        ).fetchone()
        return dict(row) if row else None

    def firmware_known(self, device_id: str, principal: str, receipt: str) -> dict[str, Any] | None:
        row = self.db.execute(
            "SELECT * FROM firmware_lkg_history WHERE device_id=? AND principal=? AND receipt=?",
            (device_id, principal, receipt),
        ).fetchone()
        return dict(row) if row else None

    def finish_firmware(
        self, job_id: str, result: str, promotion: dict[str, str] | None,
        guard: Callable[[], bool],
    ) -> None:
        """One durable commit: only a fully validated result can select a receipt."""
        try:
            self.db.execute("BEGIN IMMEDIATE")
            row = self.db.execute("SELECT state FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None or row[0] != "running" or guard() is not True:
                raise ValueError("firmware_final_commit_not_authorized")
            self.db.execute(
                "UPDATE jobs SET state='succeeded',result=?,finished=CURRENT_TIMESTAMP WHERE id=?",
                (result, job_id),
            )
            if promotion is not None:
                self.db.execute(
                    "INSERT OR IGNORE INTO firmware_lkg_history VALUES (?,?,?,?,?,?)",
                    (promotion["receipt"], promotion["device_id"], promotion["principal"],
                     promotion["kind"], promotion["baseline"], job_id),
                )
                self.db.execute(
                    "INSERT INTO firmware_lkg VALUES (?,?,?,?,?,?) "
                    "ON CONFLICT(device_id) DO UPDATE SET principal=excluded.principal, "
                    "receipt=excluded.receipt,kind=excluded.kind,baseline=excluded.baseline, "
                    "completed_job=excluded.completed_job",
                    (promotion["device_id"], promotion["principal"], promotion["receipt"],
                     promotion["kind"], promotion["baseline"], job_id),
                )
            if guard() is not True:
                raise ValueError("firmware_final_commit_not_authorized")
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise

    def set_stage(self, job_id: str, stage: str) -> None:
        self.db.execute("UPDATE jobs SET stage=? WHERE id=?", (stage, job_id))
        self.db.commit()

    def counts(self) -> dict[str, int]:
        return dict(self.db.execute("SELECT state, count(*) FROM jobs GROUP BY state").fetchall())

    def close(self) -> None:
        self.db.close()

    def approve(self, digest: str) -> None:
        if self.plan(digest) is None:
            raise ValueError("plan_not_found")
        self.db.execute("INSERT OR IGNORE INTO approvals VALUES (?)", (digest,))
        self.db.commit()

    def approved_enqueue(
        self,
        job_id: str,
        digest: str,
        allow_mock: bool,
        *,
        policy_authorized: bool = False,
        awaiting_approval: bool = False,
    ) -> None:
        # Authorization consumption and durable enqueue form ONE transaction.
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            approved = self.db.execute("SELECT 1 FROM approvals WHERE hash=?", (digest,)).fetchone()
            if not allow_mock and not policy_authorized and not approved:
                raise ValueError("operator_approval_required")
            if awaiting_approval:
                changed = self.db.execute(
                    """UPDATE jobs SET state='queued',result=NULL,finished=NULL
                    WHERE id=? AND plan_hash=? AND state='awaiting_approval'""",
                    (job_id, digest),
                )
                if changed.rowcount != 1:
                    raise ValueError("execution_intent_not_available")
            else:
                self.db.execute(
                    "INSERT INTO jobs (id,plan_hash,state) VALUES (?,?,'queued')", (job_id, digest)
                )
            self.db.execute("DELETE FROM approvals WHERE hash=?", (digest,))

    def record_count(self, table: str) -> int:
        if table not in ("jobs", "plans", "work_sessions", "device_requests"):
            raise ValueError("invalid_table")
        return int(self.db.execute(f"SELECT count(*) FROM {table}").fetchone()[0])

    def put_work_session(
        self,
        session_id: str,
        epoch: str,
        owner: str,
        principal_id: str,
        scopes: frozenset[str],
    ) -> None:
        # Capabilities and their verifiers are deliberately never persisted.
        self.db.execute(
            """INSERT INTO work_sessions(id,epoch,owner,principal_id,scopes,state)
            VALUES(?,?,?,?,?,'open')""",
            (session_id, epoch, owner, principal_id, json.dumps(sorted(scopes))),
        )
        self.db.commit()

    def set_work_session(self, session_id: str, state: str, reason: str) -> None:
        self.db.execute(
            "UPDATE work_sessions SET state=?,reason=? WHERE id=?",
            (state, reason, session_id),
        )
        self.db.commit()

    def put_request(
        self,
        request_id: str,
        session_id: str | None,
        lease_seconds: int,
        *,
        state: str = "waiting",
        lease_id: str | None = None,
    ) -> None:
        self.db.execute(
            """INSERT INTO device_requests(id,session_id,lease_seconds,state,lease_id)
            VALUES(?,?,?,?,?)""",
            (request_id, session_id, lease_seconds, state, lease_id),
        )
        self.db.commit()

    def set_request(
        self,
        request_id: str,
        state: str,
        reason: str | None = None,
        *,
        lease_id: str | None = None,
    ) -> None:
        terminal = state not in ("waiting", "owning")
        self.db.execute(
            """UPDATE device_requests SET state=?,reason=?,lease_id=COALESCE(?,lease_id),
            finished=CASE WHEN ? THEN CURRENT_TIMESTAMP ELSE NULL END WHERE id=?""",
            (state, reason, lease_id, terminal, request_id),
        )
        self.db.commit()

    def request(self, request_id: str) -> dict[str, Any] | None:
        row = self.db.execute("SELECT * FROM device_requests WHERE id=?", (request_id,)).fetchone()
        return dict(row) if row else None

    def recovery_requests(self, limit: int = 100) -> list[dict[str, Any]]:
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("invalid_recovery_limit")
        rows = self.db.execute(
            """SELECT * FROM device_requests WHERE state='needs_review' AND reviewed=0
            ORDER BY created,id LIMIT ?""",
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]

    def review_request(self, request_id: str) -> None:
        # Only the offline operator may call this after acquiring the state lock.
        request = self.request(request_id)
        if not request or request["state"] != "needs_review":
            raise ValueError("request_does_not_need_review")
        self.db.execute("UPDATE device_requests SET reviewed=1 WHERE id=?", (request_id,))
        self.db.commit()

    def session_history(self, limit: int = 100) -> list[dict[str, Any]]:
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("invalid_session_limit")
        rows = self.db.execute(
            "SELECT * FROM work_sessions ORDER BY created DESC,id LIMIT ?",
            (limit,),
        ).fetchall()
        return [{**dict(row), "scopes": json.loads(row["scopes"])} for row in rows]
