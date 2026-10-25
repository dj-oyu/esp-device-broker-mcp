"""Offline history archive: move old finished rows out of the live store, keep references.

The resident broker refuses new plans, jobs, device requests and work sessions
once a table holds 10,000 records (``history_full_archive_offline``). This
module is the explicit offline step that frees room. It runs only while the
broker is stopped (it takes the same ``broker.lockfile`` as the offline review
commands) and never touches a device, a firmware object or a backup file.

Selection is conservative. A job (and its plan) is kept when it is still
needed by anything that remains live, and every job reachable from a kept job
is kept too:

* non-terminal jobs and unreviewed ``needs_review`` jobs;
* every source fetch, backup, flash and restore job (backups are files keyed by
  their job ID; restore binds a backup job and an LKG receipt; source receipts
  and their reservations name the fetch job);
* the current LKG and every LKG history row (``completed_job`` and ``baseline``);
* the newest succeeded build for each build output slot that still has a
  durable artifact reference (an older build in the same slot was overwritten);
* pending deferred jobs and issued continuation tickets;
* jobs whose stored plan is missing or cannot be read (never guess);
* the newest ``keep_latest`` registrations.

Reachability follows every string in a kept plan or flash summary that equals
a known job ID or plan hash, plus the continuation, deferred and HOME recovery
links. Links are followed in both directions, so a group is archived whole or
kept whole.

Archived rows are copied into a new SQLite file under
``<state_dir>/history-archive/<stamp>/history.sqlite3``, verified, and only then
deleted from the live store. The archived jobs' durable job logs and captures
move into the same directory, so a later job that reuses an archived job ID
never appends to or serves the old files. A crash after the copy leaves the rows
in both places (nothing lost); a crash after the delete leaves the files in the
live directories, and the next run moves them because no live job owns them.
"""

from __future__ import annotations

import json
import os
import re
import stat
import uuid
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

from filelock import FileLock, Timeout

from .models import FLASH_TESTS, TestName
from .store import Store

ARCHIVE_DIRECTORY = "history-archive"
DEFAULT_KEEP_LATEST = 1000
MAX_KEEP_LATEST = 10000
_IDENT = re.compile(r"[a-zA-Z0-9_-]{1,64}")
_TERMINAL = frozenset({"succeeded", "failed", "cancelled", "needs_review"})
_PROTECTED_TESTS = frozenset({TestName.SOURCE.value, *(test.value for test in FLASH_TESTS)})
_REQUEST_ACTIVE = frozenset({"waiting", "owning"})


class HistoryArchiveError(ValueError):
    """A refusal with a public reason code; nothing was moved."""


def _strings(value: Any, found: set[str], depth: int = 0) -> None:
    if depth > 32:
        return
    if isinstance(value, str):
        found.add(value)
    elif isinstance(value, Mapping):
        for item in value.values():
            _strings(item, found, depth + 1)
    elif isinstance(value, list | tuple):
        for item in value:
            _strings(item, found, depth + 1)


def _json(text: Any) -> Any:
    if not isinstance(text, str):
        return None
    try:
        return json.loads(text)
    except ValueError:
        return None


class _Graph:
    def __init__(self) -> None:
        self.edges: dict[str, set[str]] = {}

    def node(self, key: str) -> None:
        self.edges.setdefault(key, set())

    def link(self, one: str, two: str) -> None:
        self.edges.setdefault(one, set()).add(two)
        self.edges.setdefault(two, set()).add(one)

    def closure(self, seeds: Iterable[str]) -> set[str]:
        kept: set[str] = set()
        pending = [seed for seed in seeds if seed in self.edges]
        while pending:
            key = pending.pop()
            if key in kept:
                continue
            kept.add(key)
            pending.extend(self.edges[key] - kept)
        return kept


def select(store: Store, keep_latest: int = DEFAULT_KEEP_LATEST) -> dict[str, Any]:
    """Decide what stays live; reads only."""
    if type(keep_latest) is not int or not 0 <= keep_latest <= MAX_KEEP_LATEST:
        raise HistoryArchiveError("history_archive_keep_latest_invalid")
    db = store.db
    graph = _Graph()
    reasons: dict[str, int] = {}
    seeds: set[str] = set()

    def seed(key: str, reason: str) -> None:
        graph.node(key)
        if key not in seeds:
            seeds.add(key)
            reasons[reason] = reasons.get(reason, 0) + 1

    def job(job_id: Any) -> str | None:
        if isinstance(job_id, str) and job_id:
            graph.node("j:" + job_id)
            return "j:" + job_id
        return None

    plans: dict[str, Any] = {}
    for digest, payload in db.execute("SELECT hash,payload FROM plans"):
        graph.node("p:" + digest)
        plans[digest] = _json(payload)
    jobs = db.execute("SELECT id,plan_hash,state,reviewed FROM jobs").fetchall()
    for row in jobs:
        key = "j:" + row["id"]
        graph.link(key, "p:" + row["plan_hash"])
        plan = plans.get(row["plan_hash"])
        if row["state"] not in _TERMINAL:
            seed(key, "unfinished_job")
        elif row["state"] == "needs_review" and not row["reviewed"]:
            seed(key, "unreviewed_needs_review")
        if not isinstance(plan, dict) or plan.get("job_id") != row["id"]:
            seed(key, "plan_missing_or_unreadable")
        elif plan.get("test") in _PROTECTED_TESTS:
            seed(key, "source_or_firmware_operation")
    for digest, plan in plans.items():
        if not isinstance(plan, dict) or not isinstance(plan.get("job_id"), str):
            seed("p:" + digest, "plan_missing_or_unreadable")
    for table in ("firmware_lkg", "firmware_lkg_history"):
        for row in db.execute(f"SELECT completed_job,baseline FROM {table}"):
            for value in row:
                key = job(value)
                if key is not None:
                    seed(key, "lkg_or_recovery_history")
            first, second = job(row[0]), job(row[1])
            if first is not None and second is not None:
                graph.link(first, second)
    for (job_id,) in db.execute("SELECT job_id FROM source_publications"):
        key = job(job_id)
        if key is not None:
            seed(key, "source_publication")
    slots: dict[str, tuple[int, str]] = {}
    for job_id, payload, sequence in db.execute(
        "SELECT r.job_id,r.payload,COALESCE(t.sequence,-1) FROM build_artifact_references r "
        "JOIN jobs j ON j.id=r.job_id AND j.state='succeeded' "
        "LEFT JOIN timeline_order t ON t.job_id=r.job_id"
    ):
        reference = _json(payload)
        if not isinstance(reference, dict):
            seed("j:" + job_id, "build_reference_unreadable")
            continue
        slot = json.dumps(
            [
                reference.get(name)
                for name in (
                    "project_id",
                    "fingerprint",
                    "environment_fingerprint",
                    "build_location",
                    "root_sha256",
                )
            ],
            sort_keys=True,
        )
        if slot not in slots or slots[slot][0] < sequence:
            slots[slot] = (sequence, job_id)
    for _, job_id in slots.values():
        seed("j:" + job_id, "latest_build_in_slot")
    for row in db.execute("SELECT parent_job_id,consumed_by_job,state FROM continuation_tickets"):
        parent, consumer = job(row[0]), job(row[1])
        if parent is not None and consumer is not None:
            graph.link(parent, consumer)
        if row[2] == "issued":
            for key in (parent, consumer):
                if key is not None:
                    seed(key, "issued_continuation")
    for row in db.execute(
        "SELECT job_id,parent_job_id,plan_hash,resolved_plan_hash,state FROM deferred_jobs"
    ):
        child, parent = job(row[0]), job(row[1])
        if child is None:
            continue
        if parent is not None:
            graph.link(child, parent)
        for digest in (row[2], row[3]):
            if isinstance(digest, str) and digest:
                graph.link(child, "p:" + digest)
        if row[4] == "pending":
            seed(child, "pending_deferred_job")
    for query in (
        "SELECT source_job,recovery_job FROM home_recovery_executions",
        "SELECT source_job,recovery_job FROM home_recovery_audit",
        "SELECT source_job,'' FROM home_closed_sources",
        "SELECT recovery_job,'' FROM home_recovery_approvals",
    ):
        for first_id, second_id in db.execute(query):
            first, second = job(first_id), job(second_id)
            if first is not None and second is not None:
                graph.link(first, second)
    # Any string a stored plan or summary carries that names a known job or plan.
    known = set(graph.edges)
    for digest, plan in plans.items():
        found: set[str] = set()
        _strings(plan, found)
        for value in found:
            for key in ("j:" + value, "p:" + value):
                if key in known and key != "p:" + digest:
                    graph.link("p:" + digest, key)
        if isinstance(plan, dict) and isinstance(plan.get("job_id"), str):
            graph.link("p:" + digest, "j:" + plan["job_id"])
    for job_id, payload in db.execute("SELECT job_id,payload FROM firmware_job_summaries"):
        found = set()
        _strings(_json(payload), found)
        for value in found:
            if "j:" + value in known:
                graph.link("j:" + job_id, "j:" + value)
    # The newest registrations by first-registration order; unordered jobs are kept.
    ordered = db.execute(
        "SELECT job_id FROM timeline_order ORDER BY sequence DESC LIMIT ?", (keep_latest,)
    ).fetchall()
    for (job_id,) in ordered:
        key = job(job_id)
        if key is not None:
            seed(key, "latest_registrations")
    timeline = {row[0] for row in db.execute("SELECT job_id FROM timeline_order")}
    for row in jobs:
        if row["id"] not in timeline:
            seed("j:" + row["id"], "unordered_job")
    kept = graph.closure(seeds)
    job_rows = {row["id"] for row in jobs}
    archived_jobs = sorted(
        key[2:] for key in graph.edges if key.startswith("j:") and key not in kept
    )
    archived_plans = sorted(
        key[2:] for key in graph.edges if key.startswith("p:") and key not in kept
    )
    remaining_plan_jobs = {
        plan["job_id"]
        for digest, plan in plans.items()
        if "p:" + digest in kept and isinstance(plan, dict) and isinstance(plan.get("job_id"), str)
    }
    archived_timeline = sorted(
        job_id
        for job_id in timeline
        if "j:" + job_id not in kept and job_id not in remaining_plan_jobs
    )
    requests = db.execute(
        "SELECT id,session_id,state,reviewed FROM device_requests ORDER BY created DESC,rowid DESC"
    ).fetchall()
    kept_requests: set[str] = set()
    for index, row in enumerate(requests):
        if (
            index < keep_latest
            or row["state"] in _REQUEST_ACTIVE
            or (row["state"] == "needs_review" and not row["reviewed"])
        ):
            kept_requests.add(row["id"])
    archived_requests = sorted(row["id"] for row in requests if row["id"] not in kept_requests)
    sessions = db.execute(
        "SELECT id,state FROM work_sessions ORDER BY created DESC,rowid DESC"
    ).fetchall()
    referenced = {
        row["session_id"] for row in requests if row["id"] in kept_requests and row["session_id"]
    }
    archived_sessions = sorted(
        row["id"]
        for index, row in enumerate(sessions)
        if index >= keep_latest and row["state"] != "open" and row["id"] not in referenced
    )
    return {
        "jobs": archived_jobs,
        "plans": archived_plans,
        "timeline": archived_timeline,
        "requests": archived_requests,
        "sessions": archived_sessions,
        "archived_job_rows": sum(job_id in job_rows for job_id in archived_jobs),
        "kept_jobs": len(jobs) - sum(job_id in job_rows for job_id in archived_jobs),
        "kept_plans": len(plans) - len(archived_plans),
        "kept_requests": len(requests) - len(archived_requests),
        "kept_sessions": len(sessions) - len(archived_sessions),
        "protection": dict(sorted(reasons.items())),
        "live_jobs": {key[2:] for key in kept if key.startswith("j:")},
    }


def _linked(path: Path) -> bool:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400)


def _job_files(state_dir: Path, live: set[str]) -> list[tuple[Path, str]]:
    """Archived jobs' logs/captures, plus leftovers no live job owns (an interrupted run)."""
    moves: list[tuple[Path, str]] = []
    for folder, suffix in (("job-logs", ".jsonl"), ("captures", "")):
        root = state_dir / folder
        if _linked(root) or not root.is_dir():
            continue
        for name in sorted(os.listdir(root)):
            job_id = name[: -len(suffix)] if suffix and name.endswith(suffix) else name
            if suffix and not name.endswith(suffix):
                continue
            path = root / name
            if _IDENT.fullmatch(job_id) is None or job_id in live or _linked(path):
                continue
            moves.append((path, folder))
    return moves


def _package_version() -> str:
    try:
        return version("cardputer-device-mcp")
    except PackageNotFoundError:
        return "unknown"


def archive_history(
    state_dir: Path,
    *,
    keep_latest: int = DEFAULT_KEEP_LATEST,
    dry_run: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Explicit offline archive. Refuses while the broker holds the state lock."""
    if not (state_dir / "history.sqlite3").is_file():
        raise HistoryArchiveError("broker_state_not_found")
    lock = FileLock(str(state_dir / "broker.lockfile"), timeout=0)
    try:
        lock.acquire()
    except Timeout as exc:
        raise HistoryArchiveError("stop_broker_before_history_archive") from exc
    try:
        store = Store(state_dir / "history.sqlite3")
        try:
            chosen = select(store, keep_latest)
            files = _job_files(state_dir, chosen["live_jobs"])
            counts = {
                "jobs": chosen["archived_job_rows"],
                "plans": len(chosen["plans"]),
                "device_requests": len(chosen["requests"]),
                "work_sessions": len(chosen["sessions"]),
                "job_logs": sum(folder == "job-logs" for _, folder in files),
                "captures": sum(folder == "captures" for _, folder in files),
            }
            result: dict[str, Any] = {
                "ok": True,
                "dry_run": dry_run,
                "keep_latest": keep_latest,
                "archive": counts,
                "kept": {
                    "jobs": chosen["kept_jobs"],
                    "plans": chosen["kept_plans"],
                    "device_requests": chosen["kept_requests"],
                    "work_sessions": chosen["kept_sessions"],
                },
                "protection": chosen["protection"],
                "replayed": False,
            }
            if dry_run or not any(counts.values()):
                result["archive_directory"] = None
                return result
            stamp = (now or datetime.now(UTC)).strftime("%Y%m%dT%H%M%SZ")
            name = f"{stamp}-{uuid.uuid4().hex[:8]}"
            parent = state_dir / ARCHIVE_DIRECTORY
            if _linked(parent):
                raise HistoryArchiveError("history_archive_linked_path_not_supported")
            parent.mkdir(mode=0o700, exist_ok=True)
            destination = parent / name
            destination.mkdir(mode=0o700)
            rows = store.archive_rows(
                destination / "history.sqlite3",
                {
                    "jobs": chosen["jobs"],
                    "plans": chosen["plans"],
                    "timeline": chosen["timeline"],
                    "requests": chosen["requests"],
                    "sessions": chosen["sessions"],
                },
                {
                    "created_utc": stamp,
                    "broker_version": _package_version(),
                    "keep_latest": str(keep_latest),
                },
            )
            moved = {"job-logs": 0, "captures": 0}
            for path, folder in files:
                target = destination / folder
                target.mkdir(mode=0o700, exist_ok=True)
                try:
                    os.rename(path, target / path.name)
                except OSError:
                    continue
                moved[folder] += 1
            result["archive"] = {
                **counts,
                "job_logs": moved["job-logs"],
                "captures": moved["captures"],
            }
            result["rows"] = rows
            result["archive_directory"] = f"{ARCHIVE_DIRECTORY}/{name}"
            return result
        finally:
            store.close()
    finally:
        lock.release()
