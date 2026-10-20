"""Read-only source retention evidence; no deletion or cleanup authorization."""

import json
import re
from typing import Any

REFERENCE_LIMIT = 4096


def resource_report(
    store: Any,
    project_id: str,
    principal: str,
    inventory: dict[str, Any],
    *,
    epoch: str | None = None,
) -> dict[str, Any]:
    """Combine a fresh complete filesystem scan with bounded owned DB evidence.

    Filesystem names are private. Only receipts already owned by this principal
    in this project are exposed; unknown/unowned entries are aggregate counts.
    References are retention evidence, never permission to remove an entry.
    """
    if epoch is not None:
        return _lifecycle_report(store, project_id, principal, inventory, epoch)
    rows = store.db.execute(
        "SELECT j.id,j.created,j.finished,j.state,j.diagnostic,p.payload FROM jobs j "
        "JOIN plans p ON p.hash=j.plan_hash ORDER BY j.id LIMIT ?",
        (REFERENCE_LIMIT + 1,),
    ).fetchall()
    complete = len(rows) <= REFERENCE_LIMIT
    sources: dict[str, dict[str, Any]] = {}
    builds: dict[str, str] = {}
    owned_builds: dict[str, tuple[Any, Any]] = {}
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
            if plan.get("build_principal") == principal:
                owned_builds[row["id"]] = (plan.get("project_id"), plan.get("source_receipt"))
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
        "SELECT receipt,completed_job,kind FROM firmware_lkg_history WHERE principal=? LIMIT ?",
        (principal, REFERENCE_LIMIT + 1),
    ).fetchall()
    complete = complete and len(history) <= REFERENCE_LIMIT
    original_jobs = {row[0]: row[1] for row in history[:REFERENCE_LIMIT]}
    current = store.db.execute(
        "SELECT receipt,completed_job,kind FROM firmware_lkg WHERE principal=? LIMIT ?",
        (principal, REFERENCE_LIMIT + 1),
    ).fetchall()
    complete = complete and len(current) <= REFERENCE_LIMIT
    unresolved = 0
    for targets, reason in (
        (current, "current_lkg_evidence"),
        (history, "recovery_history_evidence"),
    ):
        for target in targets[:REFERENCE_LIMIT]:
            if target[2] == "full_backup":
                continue  # A full backup has no build/source association.
            completed = original_jobs.get(target[0], target[1])
            build = owned_builds.get(flash_builds.get(completed, ""))
            if target[2] != "artifact_bundle" or build is None:
                complete = False
                unresolved += 1
                continue
            if build[0] != project_id or build[1] is None:
                continue  # Known other project, or a worktree build without a source receipt.
            receipt = build[1]
            if not isinstance(receipt, str) or receipt not in sources:
                complete = False
                unresolved += 1
                continue
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
    result = dict(
        source_cache=usage,
        owned_snapshots=visible,
        other_or_unclassified_snapshots=usage.get("snapshot_entries", usage["snapshots"])
        - len(visible),
        reference_inventory_complete=complete
        and len(records) == usage.get("snapshot_entries", usage["snapshots"]),
        unresolved_reference_count=unresolved,
        scan_complete=True,
        retention_policy="no_automatic_deletion",
        cleanup_action="offline_operator_review_required_preserve_unknown_and_referenced_entries",
        cleanup_authorized=False,
    )
    return result


def _lifecycle_report(store: Any, project: str, principal: str, inventory: dict[str, Any],
                      epoch: str) -> dict[str, Any]:
    from .source_retention import retention_evidence

    policy = retention_evidence(store, project, principal, epoch)
    visible = []
    reason_counts: dict[str, int] = {}
    for record in inventory["records"]:
        proofs = policy["sources"].get(record["name"])
        if not proofs:
            continue
        proof = min(proofs, key=lambda item: item["finished"])
        reasons = policy["protected"].get(record["name"], [])
        present = record.get("payload_present", True)
        candidate = bool(present and policy["complete"] and not reasons)
        for reason in reasons:
            reason_counts[reason] = reason_counts.get(reason, 0) + 1
        visible.append({
            "source_receipt": record["name"], "source_job_id": proof["job_id"],
            "finished": proof["finished"], "state": "succeeded", "bytes": record["bytes"],
            "entries": record["entries"], "payload_present": present,
            "payload_reclamation_candidate": candidate,
            "cleanup_eligible": False,  # A reference read is not full content validation.
            "protection_reasons": ["immutable_receipt_evidence_retained", *reasons,
                *([] if policy["complete"] else ["references_incomplete"])],
            "retained_until": "payload_while_referenced_receipt_within_fixed_evidence_bounds",
        })
    usage = inventory["usage"]
    return {
        "source_cache": usage, "owned_snapshots": visible,
        "other_or_unclassified_snapshots": usage["snapshot_entries"] - len(visible),
        "reference_inventory_complete": policy["complete"] and len(inventory["records"]) == usage["snapshot_entries"],
        "protection_reason_counts": reason_counts,
        "reclamation_candidate_payloads": sum(row["payload_reclamation_candidate"] for row in visible),
        "scan_complete": True,
        "retention_policy": "bounded_unreferenced_payload_reclamation_on_explicit_fetch",
        "cleanup_action": "automatic_on_fetch_preserve_receipts_unknown_and_referenced_entries",
        "cleanup_authorized": False, "automatic_payload_reclamation": True,
    }
