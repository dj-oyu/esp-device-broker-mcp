"""SQLite is accessed only by the resident broker (or offline operator under its lock)."""

import hashlib
import json
import sqlite3
from collections.abc import Callable
from pathlib import Path
from time import monotonic
from typing import Any

from .console_logs import console_line


class Store:
    def __init__(self, path: Path) -> None:
        self._console_jobs: dict[str, float] = {}
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS build_artifact_references (
            job_id TEXT PRIMARY KEY, principal TEXT NOT NULL, payload TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS firmware_job_summaries (
            job_id TEXT PRIMARY KEY, principal TEXT NOT NULL, payload TEXT NOT NULL
        );
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
        CREATE TABLE IF NOT EXISTS home_closed_sources (
            source_job TEXT PRIMARY KEY, payload TEXT NOT NULL, attempted INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS home_recovery_audit (
            sequence INTEGER PRIMARY KEY, source_job TEXT NOT NULL, recovery_job TEXT NOT NULL,
            event TEXT NOT NULL, created TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS home_recovery_approvals (
            recovery_job TEXT PRIMARY KEY, principal_id TEXT NOT NULL, review_revision TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS execution_requests (
            job_id TEXT PRIMARY KEY, plan_hash TEXT NOT NULL, principal_id TEXT NOT NULL,
            created TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS execution_audit (
            sequence INTEGER PRIMARY KEY, job_id TEXT NOT NULL, plan_hash TEXT NOT NULL,
            principal_id TEXT, event TEXT NOT NULL,
            created TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS operation_migrations (
            version TEXT PRIMARY KEY, created TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS home_recovery_executions (
            recovery_job TEXT PRIMARY KEY, source_job TEXT NOT NULL UNIQUE,
            source_hash TEXT NOT NULL, recovery_hash TEXT NOT NULL,
            principal_id TEXT NOT NULL, proof TEXT NOT NULL,
            created TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
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
        self.migrate_explicit_requests()

    def migrate_explicit_requests(self) -> None:
        """Retire legacy pending work once; historical grants never become requests."""
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            marker = "explicit_client_requests_v1"
            if self.db.execute(
                "SELECT 1 FROM operation_migrations WHERE version=?", (marker,)
            ).fetchone():
                return
            self.db.execute("""INSERT INTO execution_audit(job_id,plan_hash,event)
                SELECT id,plan_hash,CASE WHEN state='running'
                    THEN 'legacy_running_quarantined' ELSE 'legacy_pending_cancelled' END
                FROM jobs WHERE state IN ('awaiting_approval','queued','running')""")
            self.db.execute("""UPDATE jobs SET state='cancelled',
                result='explicit_requests_migration_no_replay', finished=CURRENT_TIMESTAMP
                WHERE state IN ('awaiting_approval','queued')""")
            self.db.execute("""UPDATE jobs SET state='needs_review', reviewed=0,
                result='explicit_requests_migration_running_uncertain', finished=CURRENT_TIMESTAMP
                WHERE state='running'""")
            # Keep the old grants and decision records as historical evidence.
            # No new execution path reads them or turns them into client intent.
            self.db.execute("INSERT INTO operation_migrations(version) VALUES(?)", (marker,))

    def recover(self) -> None:
        self.db.execute("""UPDATE jobs SET state='cancelled',
            result='broker_restart_no_replay', finished=CURRENT_TIMESTAMP
            WHERE state='awaiting_approval'""")
        self.db.execute("""UPDATE jobs SET state='cancelled',
            result='broker_restart_no_replay', finished=CURRENT_TIMESTAMP
            WHERE state='queued'""")
        self.db.execute("""UPDATE jobs SET state='needs_review', reviewed=0,
            result='broker_restart_no_replay', finished=CURRENT_TIMESTAMP
            WHERE state='running'""")
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

    def profile_needs_review(self) -> bool:
        """Exclude only intact, known device plans from the profile barrier.

        Unknown/missing/corrupt plans and registered-project failures stay
        fail-closed, including after restart. Device requests have a separate
        physical-device table and cannot own a source/build resource.
        """
        rows = self.db.execute(
            """SELECT jobs.id, jobs.plan_hash, plans.payload FROM jobs
            LEFT JOIN plans ON jobs.plan_hash=plans.hash
            WHERE jobs.state='needs_review' AND jobs.reviewed=0"""
        )
        device_tests = {
            "diagnostics",
            "firmware_workflow",
            "passive_observe",
            "firmware_observe",
            "restart_and_observe",
            "firmware_backup",
            "firmware_flash",
            "firmware_restore",
            "hello",
            "capture_home",
            "derby_smoke",
        }
        for job_id, digest, payload in rows:
            try:
                plan = json.loads(payload)
                canonical = json.dumps(plan, sort_keys=True, separators=(",", ":")).encode()
                if (
                    not isinstance(plan, dict)
                    or hashlib.sha256(canonical).hexdigest() != digest
                    or plan.get("job_id") != job_id
                    or plan.get("test") not in device_tests
                    or plan.get("execution_context") is not None
                ):
                    return True
            except (TypeError, ValueError):
                return True
        return False

    def home_source(self, source_job: str) -> dict[str, Any] | None:
        row = self.db.execute(
            "SELECT * FROM home_closed_sources WHERE source_job=?", (source_job,)
        ).fetchone()
        return dict(payload=json.loads(row["payload"]), attempted=row["attempted"]) if row else None

    def home_audit(self, source: str, recovery: str, event: str) -> None:
        if self.db.execute("SELECT count(*) FROM home_recovery_audit").fetchone()[0] >= 50000:
            raise ValueError("home_recovery_history_full")
        self.db.execute(
            "INSERT INTO home_recovery_audit(source_job,recovery_job,event) VALUES(?,?,?)",
            (source, recovery, event),
        )

    def record_home_closed(self, source: str, payload: dict[str, Any]) -> None:
        with self.db:
            result = self.db.execute(
                "INSERT OR IGNORE INTO home_closed_sources(source_job,payload) VALUES(?,?)",
                (source, json.dumps(payload, sort_keys=True)),
            )
            if result.rowcount:
                self.home_audit(source, "", "source_close_confirmed")

    def consume_home_attempt(
        self,
        source: str,
        recovery: str,
        expected: dict[str, Any],
        recovery_hash: str,
        principal_id: str,
    ) -> None:
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            if self.home_source(source) != dict(payload=expected, attempted=0):
                raise ValueError("home_recovery_source_changed")
            origin, current = self.job(source), self.job(recovery)
            if (
                origin is None
                or origin["state"] != "needs_review"
                or origin["reviewed"]
                or origin["plan_hash"] != expected["plan_hash"]
                or current is None
                or current["state"] != "queued"
                or current["plan_hash"] != recovery_hash
                or expected.get("principal_id") != principal_id
                or self.db.execute(
                    "SELECT 1 FROM execution_requests WHERE job_id=? AND plan_hash=? "
                    "AND principal_id=?",
                    (recovery, recovery_hash, principal_id),
                ).fetchone()
                is None
            ):
                raise ValueError("home_recovery_source_changed")
            self.db.execute(
                "UPDATE home_closed_sources SET attempted=1 WHERE source_job=?", (source,)
            )
            self.db.execute(
                "UPDATE jobs SET state='running' WHERE id=? AND state='queued'",
                (recovery,),
            )
            self.home_audit(source, recovery, "attempt_consumed")
            self.db.execute(
                "INSERT INTO home_recovery_executions"
                "(recovery_job,source_job,source_hash,recovery_hash,principal_id,proof) "
                "VALUES(?,?,?,?,?,?)",
                (
                    recovery,
                    source,
                    expected["plan_hash"],
                    recovery_hash,
                    principal_id,
                    json.dumps(expected, sort_keys=True),
                ),
            )

    def finish_home_recovery(
        self,
        source: str,
        recovery: str,
        source_hash: str,
        recovery_hash: str,
        guard: Callable[[], bool],
    ) -> None:
        def matches() -> bool:
            row = self.job(source)
            current = self.job(recovery)
            proof = self.home_source(source)
            execution = self.db.execute(
                "SELECT * FROM home_recovery_executions WHERE recovery_job=? AND source_job=? "
                "AND source_hash=? AND recovery_hash=?",
                (recovery, source, source_hash, recovery_hash),
            ).fetchone()
            return bool(
                row is not None
                and row["state"] == "needs_review"
                and row["reviewed"] == 0
                and row["plan_hash"] == source_hash
                and current is not None
                and current["state"] == "running"
                and current["plan_hash"] == recovery_hash
                and proof is not None
                and proof["attempted"] == 1
                and execution is not None
                and execution["principal_id"] == proof["payload"].get("principal_id")
                and json.loads(execution["proof"]) == proof["payload"]
            )

        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            if not matches():
                raise ValueError("home_recovery_source_changed")
            self.home_audit(source, recovery, "fresh_home_and_close_confirmed")
            # Audit hooks and authentication callbacks can expire or revoke the
            # request. Check authority and all durable bindings after both, with
            # no callback between these final checks and the state update.
            if guard() is not True or not matches():
                raise ValueError("home_recovery_final_commit_not_authorized")
            # Preserve the failed result; only confirmed physical HOME resolves
            # this source's uncertainty barrier, never a manual decision.
            self.db.execute("UPDATE jobs SET reviewed=1 WHERE id=?", (source,))
            self.db.execute(
                "UPDATE jobs SET state='succeeded',result='fresh_home_closed',"
                "finished=CURRENT_TIMESTAMP WHERE id=?",
                (recovery,),
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
        raise ValueError("approval_interface_retired")

    def enqueue_requested(self, job_id: str, digest: str, principal_id: str) -> None:
        """Commit one explicit request and its audit without consulting legacy grants.

        The broker authenticates scopes, lease and device before admission and
        again before I/O. This receipt records that request; it is no reusable
        authority, and startup never reconstructs a queue from these rows.
        """
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            plan = self.plan(digest)
            if (
                not isinstance(principal_id, str)
                or not principal_id
                or plan is None
                or plan.get("job_id") != job_id
                or hashlib.sha256(
                    json.dumps(plan, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest()
                != digest
                or (plan.get("principal_id") is not None and plan["principal_id"] != principal_id)
            ):
                raise ValueError("explicit_request_invalid")
            self.db.execute(
                "INSERT INTO jobs(id,plan_hash,state) VALUES(?,?,'queued')", (job_id, digest)
            )
            self.db.execute(
                "INSERT INTO execution_requests(job_id,plan_hash,principal_id) VALUES(?,?,?)",
                (job_id, digest, principal_id),
            )
            self.db.execute(
                "INSERT INTO execution_audit(job_id,plan_hash,principal_id,event) "
                "VALUES(?,?,?,'client_requested')",
                (job_id, digest, principal_id),
            )

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
        self._console_state(job_id, state)

    def _console_state(self, job_id: str, state: str) -> None:
        # Observability must never affect a committed job or its authorization.
        try:
            if state == "running" and job_id not in self._console_jobs:
                if len(self._console_jobs) >= 64:
                    self._console_jobs.pop(next(iter(self._console_jobs)))
                self._console_jobs[job_id] = monotonic()
                console_line(job_id, "started")
            elif state in ("succeeded", "failed", "cancelled", "needs_review"):
                started = self._console_jobs.pop(job_id, None)
                if started is not None:
                    elapsed = max(0.0, monotonic() - started)
                    console_line(job_id, f"{state}; elapsed {elapsed:.1f}s")
        except Exception:
            pass

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
        self,
        job_id: str,
        result: str,
        promotion: dict[str, str] | None,
        guard: Callable[[], bool],
        summary: dict[str, Any] | None = None,
        principal: str | None = None,
    ) -> None:
        """One durable commit: only a fully validated result can select a receipt."""
        try:
            self.db.execute("BEGIN IMMEDIATE")
            row = self.db.execute(
                "SELECT state,plan_hash FROM jobs WHERE id=?", (job_id,)
            ).fetchone()
            if row is None or row[0] != "running" or guard() is not True:
                raise ValueError("firmware_final_commit_not_authorized")
            self.db.execute(
                "UPDATE jobs SET state='succeeded',result=?,finished=CURRENT_TIMESTAMP WHERE id=?",
                (result, job_id),
            )
            if summary is not None:
                payload = json.dumps(summary, sort_keys=True, separators=(",", ":"))
                if principal is None or len(payload.encode()) > 4096:
                    raise ValueError("firmware_summary_invalid")
                self.db.execute(
                    "INSERT INTO firmware_job_summaries VALUES (?,?,?)",
                    (job_id, principal, payload),
                )
            if promotion is not None:
                self.db.execute(
                    "INSERT OR IGNORE INTO firmware_lkg_history VALUES (?,?,?,?,?,?)",
                    (
                        promotion["receipt"],
                        promotion["device_id"],
                        promotion["principal"],
                        promotion["kind"],
                        promotion["baseline"],
                        job_id,
                    ),
                )
                self.db.execute(
                    "INSERT INTO firmware_lkg VALUES (?,?,?,?,?,?) "
                    "ON CONFLICT(device_id) DO UPDATE SET principal=excluded.principal, "
                    "receipt=excluded.receipt,kind=excluded.kind,baseline=excluded.baseline, "
                    "completed_job=excluded.completed_job",
                    (
                        promotion["device_id"],
                        promotion["principal"],
                        promotion["receipt"],
                        promotion["kind"],
                        promotion["baseline"],
                        job_id,
                    ),
                )
            if guard() is not True:
                raise ValueError("firmware_final_commit_not_authorized")
            final = self.db.execute(
                "SELECT state,plan_hash,result FROM jobs WHERE id=?", (job_id,)
            ).fetchone()
            if (
                final is None
                or final["state"] != "succeeded"
                or final["plan_hash"] != row["plan_hash"]
                or final["result"] != result
            ):
                raise ValueError("firmware_final_commit_not_authorized")
            self.db.commit()
            self._console_state(job_id, "succeeded")
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
        raise ValueError("approval_interface_retired")

    def approved_enqueue(
        self,
        job_id: str,
        digest: str,
        allow_mock: bool,
        *,
        policy_authorized: bool = False,
        awaiting_approval: bool = False,
    ) -> None:
        raise ValueError("approval_interface_retired")

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

    def firmware_summary(self, job_id: str, principal: str) -> dict[str, Any] | None:
        row = self.db.execute(
            "SELECT payload FROM firmware_job_summaries WHERE job_id=? AND principal=?",
            (job_id, principal),
        ).fetchone()
        return json.loads(row[0]) if row is not None else None
