"""Read-only source retention evidence; no deletion or cleanup authorization."""

import json
import re
from typing import Any

REFERENCE_LIMIT = 4096


def resource_report(
    store: Any, project_id: str, principal: str, inventory: dict[str, Any]
) -> dict[str, Any]:
    """Combine a fresh complete filesystem scan with bounded owned DB evidence.

    Filesystem names are private. Only receipts already owned by this principal
    in this project are exposed; unknown/unowned entries are aggregate counts.
    References are retention evidence, never permission to remove an entry.
    """
    rows = store.db.execute(
        "SELECT j.id,j.created,j.finished,j.state,j.diagnostic,p.payload FROM jobs j "
        "JOIN plans p ON p.hash=j.plan_hash ORDER BY j.id LIMIT ?",
        (REFERENCE_LIMIT + 1,),
    ).fetchall()
    complete = len(rows) <= REFERENCE_LIMIT
    sources: dict[str, dict[str, Any]] = {}
    builds: dict[str, str] = {}
    flash_builds: dict[str, str] = {}
    for row in rows[:REFERENCE_LIMIT]:
        try:
            plan = json.loads(row["payload"])
            if plan.get("job_id") != row["id"]:
                complete = False
                continue
            if plan.get("flash_principal") == principal:
                build = plan.get("build_job_id")
                if isinstance(build, str):
                    flash_builds[row["id"]] = build
            if plan.get("project_id") != project_id:
                continue
            if plan.get("source_principal") == principal:
                diagnostic = json.loads(row["diagnostic"]) if row["diagnostic"] else {}
                receipt = diagnostic.get("source", {}).get("source_receipt")
                if isinstance(receipt, str) and re.fullmatch(r"[a-f0-9]{64}", receipt):
                    sources[receipt] = dict(
                        source_receipt=receipt,
                        source_job_id=row["id"],
                        created=row["created"],
                        finished=row["finished"],
                        state=row["state"],
                        protection_reasons=["retained_source_evidence"],
                    )
            if plan.get("build_principal") == principal:
                receipt = plan.get("source_receipt")
                if isinstance(receipt, str):
                    builds[row["id"]] = receipt
        except (ValueError, TypeError, AttributeError):
            complete = False

    reasons: dict[str, set[str]] = {}
    for receipt in builds.values():
        reasons.setdefault(receipt, set()).add("build_plan_or_artifact_evidence")
    # History preserves the original bundle's build association even when a
    # later restore becomes the current completed job for that same receipt.
    history = store.db.execute(
        "SELECT receipt,completed_job FROM firmware_lkg_history WHERE principal=? LIMIT ?",
        (principal, REFERENCE_LIMIT + 1),
    ).fetchall()
    complete = complete and len(history) <= REFERENCE_LIMIT
    original_jobs = {row[0]: row[1] for row in history[:REFERENCE_LIMIT]}
    current = store.db.execute(
        "SELECT receipt,completed_job FROM firmware_lkg WHERE principal=? LIMIT ?",
        (principal, REFERENCE_LIMIT + 1),
    ).fetchall()
    complete = complete and len(current) <= REFERENCE_LIMIT
    for targets, reason in (
        (current, "current_lkg_evidence"),
        (history, "recovery_history_evidence"),
    ):
        for target in targets[:REFERENCE_LIMIT]:
            completed = original_jobs.get(target[0], target[1])
            receipt = builds.get(flash_builds.get(completed, ""))
            if receipt is not None:
                reasons.setdefault(receipt, set()).add(reason)

    records = inventory["records"]
    visible = []
    for record in records:
        source = sources.get(record["name"])
        if source is None:
            continue
        visible.append(
            {
                **source,
                "bytes": record["bytes"],
                "entries": record["entries"],
                "retained_until": "explicit_offline_review_no_automatic_expiry",
                "cleanup_eligible": False,
                "protection_reasons": sorted(
                    set(source["protection_reasons"]) | reasons.get(record["name"], set())
                ),
            }
        )
    usage = inventory["usage"]
    return dict(
        source_cache=usage,
        owned_snapshots=visible,
        other_or_unclassified_snapshots=usage["snapshots"] - len(visible),
        reference_inventory_complete=complete and len(records) == usage["snapshots"],
        scan_complete=True,
        retention_policy="no_automatic_deletion",
        cleanup_action="offline_operator_review_required_preserve_unknown_and_referenced_entries",
        cleanup_authorized=False,
    )
