"""Owned diagnostic details over the existing shared persisted job summary."""

import copy
import hashlib
import json
from typing import Any

MAX_REPORT_BYTES = 128 * 1024
PROVENANCE_KEY = "derby_diagnostics_provenance"


def shared_job(job: dict[str, Any]) -> dict[str, Any]:
    """Never expose new private details through the legacy shared diagnostic."""
    result = copy.deepcopy(job)
    diagnostic = result.get("diagnostic")
    if isinstance(diagnostic, dict):
        diagnostic.pop(PROVENANCE_KEY, None)
        observation = diagnostic.get("derby_observation")
        if isinstance(observation, dict):
            observation.pop("diagnostics", None)
    result.pop("derby_diagnostics", None)
    return result


def owned_request(store: Any, job: dict[str, Any], plan: Any, principal: str) -> bool:
    if (
        not isinstance(plan, dict)
        or plan.get("test") != "derby_smoke"
        or plan.get("job_id") != job.get("id")
        or hashlib.sha256(
            json.dumps(plan, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        != job.get("plan_hash")
    ):
        return False
    return (
        store.db.execute(
            "SELECT 1 FROM execution_requests WHERE job_id=? AND plan_hash=? AND principal_id=?",
            (job["id"], job["plan_hash"], principal),
        ).fetchone()
        is not None
    )


def binding_unchanged(store: Any, job: dict[str, Any], plan: Any, principal: str) -> bool:
    """Callback-free check after the final current authentication lookup."""
    return (
        owned_request(store, job, plan, principal)
        and store.job(job["id"]) == job
        and store.plan(job["plan_hash"]) == plan
    )


def private_job(store: Any, job: dict[str, Any], plan: Any, principal: str) -> dict[str, Any]:
    result = shared_job(job)
    if not owned_request(store, job, plan, principal):
        return result
    diagnostic = job.get("diagnostic")
    if not isinstance(diagnostic, dict):
        return result
    observation = diagnostic.get("derby_observation")
    if not isinstance(observation, dict) or observation.get("diagnostics") is None:
        return result
    # Import only when an actual new report exists; legacy rows retain their
    # original shape and never acquire invented diagnostic records.
    from .derby_provenance import DerbyProvenance
    from .derby_telemetry import DerbyDiagnostics

    try:
        provenance = DerbyProvenance.model_validate_json(json.dumps(diagnostic.get(PROVENANCE_KEY)))
        expected = {
            "job_id": job["id"],
            "plan_hash": job["plan_hash"],
            "principal_id": principal,
            "broker_epoch": plan.get("broker_epoch"),
            "device_fingerprint": plan.get("device_fingerprint"),
            "configured_expected_firmware_sha256": plan.get("expected_firmware_sha256"),
        }
        if any(
            (value := getattr(provenance, key)) is not None and value != bound
            for key, bound in expected.items()
        ):
            raise ValueError("diagnostic_binding_mismatch")
        report = {
            "available": True,
            "observation": DerbyDiagnostics.model_validate_json(
                json.dumps(observation["diagnostics"])
            ).model_dump(mode="json", exclude_none=True),
            "provenance": provenance.model_dump(mode="json", exclude_none=True),
        }
        encoded = json.dumps(report, sort_keys=True, separators=(",", ":")).encode()
        if len(encoded) > MAX_REPORT_BYTES:
            raise ValueError("report_size_limit")
    except (TypeError, ValueError):
        result["derby_diagnostics"] = {
            "available": False,
            "reason": "diagnostic_details_unavailable",
        }
    else:
        result["derby_diagnostics"] = report
    return result
