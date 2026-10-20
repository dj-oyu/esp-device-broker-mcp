"""Bounded resident-thread retention evidence; never access SQLite from a worker."""

import hashlib
import json
import re
import time
from typing import Any

REFERENCE_LIMIT = 10000
PLAN_LEASE_SECONDS = 1800
TERMINAL = frozenset({"succeeded", "failed", "cancelled"})


def retention_evidence(store: Any, project: str, principal: str, epoch: str) -> dict[str, Any]:
    """Protect executable plans, unfinished jobs and every recovery association.

    Completed build plans are provenance, not permanent payload reservations.
    The immutable source receipt is retained even when its payload is reclaimed.
    Unknown/truncated evidence disables all reclamation, including orphan cleanup.
    """
    rows = store.db.execute(
        "SELECT p.hash,CASE WHEN length(CAST(p.payload AS BLOB))<=16384 "
        "THEN p.payload END AS payload,j.id,j.state,"
        "CASE WHEN length(CAST(j.diagnostic AS BLOB))<=16384 "
        "THEN j.diagnostic END AS diagnostic,"
        "j.created,j.finished "
        "FROM plans p LEFT JOIN jobs j ON j.plan_hash=p.hash ORDER BY p.hash LIMIT ?",
        (REFERENCE_LIMIT + 1,),
    ).fetchall()
    complete = (
        len(rows) <= REFERENCE_LIMIT
        and store.db.execute(
            "SELECT 1 FROM jobs j LEFT JOIN plans p ON p.hash=j.plan_hash "
            "WHERE p.hash IS NULL LIMIT 1"
        ).fetchone()
        is None
    )
    sources: dict[str, Any] = {}
    jobs: dict[str, Any] = {}
    plans: dict[str, Any] = {}
    executable: list[dict[str, Any]] = []
    protected: dict[str, list[str]] = {}
    now = time.time()

    def protect(receipt: str, reason: str) -> None:
        reasons = protected.setdefault(receipt, [])
        if reason not in reasons:
            reasons.append(reason)

    for row in rows[:REFERENCE_LIMIT]:
        try:
            plan = json.loads(row["payload"])
            if not isinstance(plan, dict) or not isinstance(plan.get("job_id"), str):
                complete = False
                continue
            encoded = json.dumps(plan, sort_keys=True, separators=(",", ":")).encode()
            if hashlib.sha256(encoded).hexdigest() != row["hash"]:
                complete = False
                continue
            job_id = plan["job_id"]
            if row["id"] is not None and row["id"] != job_id:
                complete = False
                continue
            if row["id"] is not None:
                jobs[job_id] = {"state": row["state"], "plan": plan}
                plans[job_id] = plan
            if (row["id"] is not None and row["state"] not in TERMINAL) or (
                row["id"] is None and plan.get("broker_epoch") == epoch
            ):
                executable.append(plan)
            if plan.get("project_id") != project:
                continue
            receipt = plan.get("source_receipt")
            if receipt is not None:
                if not isinstance(receipt, str) or not re.fullmatch(r"[a-f0-9]{64}", receipt):
                    complete = False
                    continue
                if row["id"] is not None and row["state"] not in TERMINAL:
                    protect(receipt, "unfinished_job")
            if plan.get("source_principal") != principal or row["state"] != "succeeded":
                continue
            diagnostic = json.loads(row["diagnostic"]) if row["diagnostic"] else {}
            summary = diagnostic.get("source", {})
            receipt = summary.get("source_receipt")
            if not isinstance(receipt, str) or not re.fullmatch(r"[a-f0-9]{64}", receipt):
                complete = False
                continue
            if plan.get("test") != "firmware_fetch_source":
                complete = False
                continue
            sources.setdefault(receipt, []).append(
                {
                    "job_id": job_id,
                    "plan": plan,
                    "summary": summary,
                    "finished": row["finished"] or row["created"],
                }
            )
        except (ValueError, TypeError, AttributeError):
            complete = False

    # An alternate prepared plan for a job ID already used by another hash can
    # no longer execute. It must not keep an abandoned reservation alive.
    executable = [
        plan
        for plan in executable
        if plan["job_id"] not in jobs or jobs[plan["job_id"]]["plan"] == plan
    ]
    for plan in executable:
        if plan["job_id"] in jobs or plan.get("project_id") != project:
            continue
        receipt = plan.get("source_receipt")
        expiry = plan.get("source_plan_expires_at")
        if receipt is not None and (
            expiry is None or not isinstance(expiry, (int, float)) or expiry > now
        ):
            protect(receipt, "executable_plan")

    history = store.db.execute(
        "SELECT receipt,completed_job,kind,principal FROM firmware_lkg_history LIMIT ?",
        (REFERENCE_LIMIT + 1,),
    ).fetchall()
    current = store.db.execute(
        "SELECT receipt,completed_job,kind,principal FROM firmware_lkg LIMIT ?",
        (REFERENCE_LIMIT + 1,),
    ).fetchall()
    complete = complete and len(history) <= REFERENCE_LIMIT and len(current) <= REFERENCE_LIMIT
    originals = {row["receipt"]: row for row in history[:REFERENCE_LIMIT]}
    # A flash in flight (or still executable) can promote a completed build to
    # LKG concurrently with a source fetch. Protect that build's source first.
    for plan in executable:
        build = plans.get(plan.get("build_job_id", ""), {})
        if build.get("project_id") == project and build.get("source_receipt") is not None:
            protect(build["source_receipt"], "executable_firmware_operation")
    for targets, reason in (
        (current, "current_lkg_evidence"),
        (history, "recovery_history_evidence"),
    ):
        for target in targets[:REFERENCE_LIMIT]:
            if target["kind"] == "full_backup":
                continue
            original = originals.get(target["receipt"], target)
            flash = plans.get(original["completed_job"], {})
            build = plans.get(flash.get("build_job_id"), {})
            if (
                target["kind"] != "artifact_bundle"
                or not build
                or original["principal"] != target["principal"]
                or flash.get("flash_principal") != target["principal"]
                or build.get("build_principal") != target["principal"]
            ):
                complete = False
                continue
            if build.get("project_id") == project and build.get("source_receipt") is not None:
                protect(build["source_receipt"], reason)
    publications = store.db.execute(
        "SELECT * FROM source_publications LIMIT ?", (REFERENCE_LIMIT + 1,)
    ).fetchall()
    complete = complete and len(publications) <= REFERENCE_LIMIT
    owned_publications = {}
    for row in publications[:REFERENCE_LIMIT]:
        if row["job_id"] not in jobs:
            complete = False
        if row["project_id"] == project and row["principal"] == principal:
            owned_publications[row["receipt"]] = row["job_id"]
    return {
        "complete": complete,
        "sources": sources,
        "protected": protected,
        "jobs": jobs,
        "publications": owned_publications,
    }
