"""Read-only job lookups shared by every reader (MCP status and the local operator).

Reads are not bound to the principal that started a job: any authenticated
caller with the read scope may read a job's status, logs, device report and
capture. Each reader is still served through the owner-scoped code path with
the job owner's principal, so the returned body is byte-for-byte what the owner
would see, with the owner's redactions. The owner identity itself never leaves
this module; callers only learn ``owned_by_caller``.

Mutations (cancel, execute, session actions, app input, home recovery) keep
their own owner checks and do not use these helpers.
"""

from __future__ import annotations

import json
import re
import secrets
import stat
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .models import FLASH_TESTS, TestName

PLAN_PRINCIPAL_KEYS = ("principal_id", "flash_principal", "build_principal", "source_principal")
_IDENT = re.compile(r"[a-zA-Z0-9_-]{1,64}")
_DIGEST = re.compile(r"[a-f0-9]{64}")
_FLASH_TEST_VALUES = frozenset(test.value for test in FLASH_TESTS)
RECOVERY_HISTORY_LIMIT = 20
RECOVERY_BACKUP_LIMIT = 20


def plan_owner(plan: Mapping[str, Any] | None) -> str | None:
    """The single principal a plan is bound to, or None when unbound/ambiguous."""
    if not isinstance(plan, Mapping):
        return None
    bound = {plan[key] for key in PLAN_PRINCIPAL_KEYS if isinstance(plan.get(key), str)}
    return next(iter(bound)) if len(bound) == 1 else None


def job_owner(broker: Any, job_id: str, plan: Mapping[str, Any] | None) -> str | None:
    """The principal that owns a stored job: plan binding first, then its queue record."""
    owner = plan_owner(plan)
    if owner is not None:
        return owner
    principal = broker.job_principal(job_id)
    return principal if isinstance(principal, str) and principal else None


def same_principal(owner: str | None, caller: str | None) -> bool:
    return (
        isinstance(owner, str)
        and isinstance(caller, str)
        and bool(owner)
        and secrets.compare_digest(owner.encode(), caller.encode())
    )


def log_stream(broker: Any, plan: Mapping[str, Any], job_id: str) -> tuple[Any, Any, str]:
    """(owner principal, RAM log service or None, durable stream) for one job's output."""
    test = plan.get("test")
    if test == TestName.SOURCE.value:
        return plan.get("source_principal"), broker.firmware_source, "source"
    if test == TestName.BUILD.value:
        return plan.get("build_principal"), broker.firmware_build, "build"
    if test in _FLASH_TEST_VALUES:
        return plan.get("flash_principal"), broker.firmware_flash, "flash"
    return broker.job_logs.owner(job_id), None, "device"


def device_log_owner(broker: Any, job_id: str) -> str | None:
    """Recipient of the retained device report, else the durable device log's owner."""
    retained = broker.device_observations.get(job_id)
    if retained is not None and isinstance(retained.principal_id, str):
        return str(retained.principal_id)
    owner = broker.job_principal(job_id)
    if isinstance(owner, str) and owner:
        return owner
    recorded = broker.job_logs.owner(job_id)
    return recorded if isinstance(recorded, str) and recorded else None


def _saved_capture_principal(broker: Any, job_id: str) -> str | None:
    """Principal recorded in a saved capture's metadata (no image bytes are read)."""
    try:
        root = broker.capture_store.root
        if not isinstance(root, Path) or _IDENT.fullmatch(job_id) is None:
            return None
        path = root / job_id / "capture.json"
        info = path.lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or getattr(info, "st_file_attributes", 0) & 0x400
            or info.st_size > 4096
        ):
            return None
        metadata = json.loads(path.read_text("utf-8"))
        if type(metadata) is not dict or metadata.get("job_id") != job_id:
            return None
        principal = metadata.get("principal_id")
        return principal if isinstance(principal, str) and principal else None
    except Exception:
        return None


def capture_owner(broker: Any, job_id: str) -> str | None:
    """RAM capture's principal, else the saved copy's, else the job's owner."""
    retained = broker.captures.get(job_id)
    principal = getattr(retained, "principal_id", None)
    if isinstance(principal, str) and principal:
        return principal
    saved = _saved_capture_principal(broker, job_id)
    if saved is not None:
        return saved
    job = broker._store().job(job_id)
    plan = broker._store().plan(job["plan_hash"]) if job is not None else None
    return job_owner(broker, job_id, plan)


def app_event_owner(broker: Any, job_id: str) -> str | None:
    """RAM app session's principal, else the job's queue record, else the durable log's."""
    run = broker.app_sessions.jobs.get(job_id)
    principal = getattr(run, "principal", None)
    if isinstance(principal, str) and principal:
        return principal
    owner = broker.job_principal(job_id)
    if isinstance(owner, str) and owner:
        return owner
    recorded = broker.job_logs.owner(job_id)
    return recorded if isinstance(recorded, str) and recorded else None


def deferred_owner(broker: Any, job_id: str) -> str | None:
    row = broker.deferred.get(job_id)
    principal = row["principal_id"] if row is not None else None
    return principal if isinstance(principal, str) and principal else None


def _identifier(value: Any) -> str | None:
    return value if isinstance(value, str) and _IDENT.fullmatch(value) else None


def _digest(value: Any) -> str | None:
    return value if isinstance(value, str) and _DIGEST.fullmatch(value) else None


def job_kind(plan: Mapping[str, Any] | None, test_kind: Mapping[str, str]) -> str:
    if not isinstance(plan, Mapping):
        return "unknown"
    if plan.get("operation") == "home_recovery":
        return "home_recovery"
    test = plan.get("test")
    return test_kind.get(test, "other") if isinstance(test, str) else "other"


def list_jobs(
    broker: Any,
    caller: str | None,
    test_kind: Mapping[str, str],
    *,
    before: int | None,
    limit: int,
) -> dict[str, Any]:
    """Newest-first page of stored jobs of every principal (no plan or owner text)."""
    store = broker._store()
    query = "SELECT rowid,id,plan_hash,state,result,created,finished FROM jobs"
    arguments: tuple[Any, ...] = ()
    if before is not None:
        query += " WHERE rowid<?"
        arguments = (before,)
    rows = store.db.execute(query + " ORDER BY rowid DESC LIMIT ?", (*arguments, limit + 1))
    page = list(rows)
    jobs = []
    for row in page[:limit]:
        plan = store.plan(row["plan_hash"])
        jobs.append(
            {
                "job_id": row["id"],
                "kind": job_kind(plan, test_kind),
                "state": row["state"],
                "result": row["result"],
                "created": row["created"],
                "finished": row["finished"],
                "device_id": _identifier(plan.get("device_id")) if plan else None,
                "owned_by_caller": same_principal(job_owner(broker, row["id"], plan), caller),
            }
        )
    has_more = len(page) > limit
    return {
        "jobs": jobs,
        "has_more": has_more,
        "next_cursor": {"before": int(page[limit - 1]["rowid"])} if has_more else None,
    }


def _backup_metadata(directory: Any, job_id: str) -> dict[str, Any] | None:
    """Digest/size of a retained full backup from its receipt; never the bytes or path."""
    try:
        if not isinstance(directory, Path) or _IDENT.fullmatch(job_id) is None:
            return None
        path = directory / (job_id + ".json")
        info = path.lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or getattr(info, "st_file_attributes", 0) & 0x400
            or info.st_size > 4096
        ):
            return None
        record = json.loads(path.read_text("utf-8"))
        if type(record) is not dict or record.get("job_id") != job_id:
            return None
        size = record.get("flash_bytes")
        return {
            "sha256": _digest(record.get("sha256")),
            "flash_bytes": size if type(size) is int else None,
        }
    except Exception:
        return None


def _firmware(plan: Mapping[str, Any] | None) -> dict[str, Any]:
    plan = plan if isinstance(plan, Mapping) else {}
    return {
        "firmware_sha256": _digest(plan.get("expected_app_bin_sha256")),
        "elf_sha256": _digest(plan.get("expected_elf_sha256")),
        "backup_sha256": _digest(plan.get("backup_sha256")),
    }


def recovery(broker: Any, caller: str | None) -> dict[str, Any]:
    """Per device: the current LKG, recent LKG history and retained full backups."""
    store = broker._store()
    registrations = broker.settings.flash_devices
    known = {
        str(row[0])
        for table in ("firmware_lkg", "firmware_lkg_history")
        for row in store.db.execute(f"SELECT DISTINCT device_id FROM {table}")
    }
    devices = sorted(
        device for device in set(registrations) | known if _IDENT.fullmatch(device) is not None
    )
    plans: dict[str, Any] = {}

    def job_plan(job_id: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        job = store.job(job_id)
        if job is None:
            return None, None
        digest = job["plan_hash"]
        if digest not in plans:
            plans[digest] = store.plan(digest)
        return job, plans[digest]

    report = []
    for device_id in devices:
        current = store.firmware_current(device_id)
        history = store.db.execute(
            "SELECT history.rowid,history.receipt,history.principal,history.kind,"
            "history.baseline,history.completed_job,jobs.finished "
            "FROM firmware_lkg_history AS history "
            "LEFT JOIN jobs ON jobs.id=history.completed_job "
            "WHERE history.device_id=? "
            "ORDER BY jobs.finished DESC, history.rowid DESC LIMIT ?",
            (device_id, RECOVERY_HISTORY_LIMIT),
        ).fetchall()
        lkg = []
        for row in history:
            _, plan = job_plan(row["completed_job"])
            lkg.append(
                {
                    "receipt": _digest(row["receipt"]),
                    "kind": row["kind"],
                    "current": current is not None and current["receipt"] == row["receipt"],
                    "job_id": _identifier(row["completed_job"]),
                    "baseline_job_id": _identifier(row["baseline"]),
                    "recorded": row["finished"],
                    "recorded_by_caller": same_principal(row["principal"], caller),
                    **_firmware(plan),
                }
            )
        backups: list[dict[str, Any]] = []
        registration = registrations.get(device_id)
        for row in store.db.execute(
            "SELECT id,plan_hash,created,finished FROM jobs "
            "WHERE state='succeeded' AND plan_hash IN "
            "(SELECT hash FROM plans WHERE payload LIKE '%\"firmware_backup\"%') "
            "ORDER BY rowid DESC"
        ):
            if len(backups) >= RECOVERY_BACKUP_LIMIT:
                break
            plan = store.plan(row["plan_hash"])
            if (
                not isinstance(plan, dict)
                or plan.get("test") != TestName.BACKUP.value
                or plan.get("device_id") != device_id
            ):
                continue
            receipt = _backup_metadata(
                getattr(registration, "backup_directory", None), str(row["id"])
            )
            backups.append(
                {
                    "job_id": _identifier(row["id"]),
                    "created": row["created"],
                    "finished": row["finished"],
                    "sha256": receipt["sha256"] if receipt else None,
                    "flash_bytes": receipt["flash_bytes"] if receipt else None,
                    "receipt_available": receipt is not None,
                    "owned_by_caller": same_principal(plan_owner(plan), caller),
                }
            )
        report.append(
            {
                "device_id": device_id,
                "registered": device_id in registrations,
                "current_lkg": _digest(current["receipt"]) if current else None,
                "lkg_history": lkg,
                "full_backups": backups,
            }
        )
    return {"devices": report}
