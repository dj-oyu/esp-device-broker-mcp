"""Owned diagnostic details over the existing shared persisted job summary."""

import base64
import copy
import hashlib
import json
import re
from typing import Any

from .derby_fixed import MAX_BYTES, PARSER_COMMIT, parse_collected

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
    result.pop("fixed_trial", None)
    return result


def owned_request(store: Any, job: dict[str, Any], plan: Any, principal: str) -> bool:
    if (
        not isinstance(plan, dict)
        or plan.get("test") not in ("derby_smoke", "derby_fixed_workload")
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
    if plan.get("test") == "derby_fixed_workload":
        try:
            record = store.fixed_trial(job["id"], job["plan_hash"], principal)
            if record is None:
                return result
            raw, receipt = record["raw"], record["receipt"]
            flags = {
                "gaps",
                "overflow",
                "truncated",
                "partial_tail",
                "disconnect",
                "reset",
                "refused",
            }
            if (
                type(raw) is not bytes
                or len(raw) > MAX_BYTES
                or type(receipt) is not dict
                or set(receipt) != {"complete", "bytes", "duration_ms", "sha256", *flags}
                or type(receipt["complete"]) is not bool
                or any(type(receipt[key]) is not bool for key in flags)
                or type(receipt["bytes"]) is not int
                or receipt.get("bytes") != len(raw)
                or type(receipt["duration_ms"]) is not int
                or not 0 <= receipt["duration_ms"] <= 200000
                or receipt.get("sha256") != hashlib.sha256(raw).hexdigest()
                or record["sha256"] != receipt["sha256"]
                or (not record["valid"] and record["parsed"] is not None)
                or plan.get("backend") != "serial"
                or type(plan.get("fixed_workload")) is not dict
                or plan["fixed_workload"].get("parser_commit") != PARSER_COMMIT
                or not re.fullmatch(r"[a-f0-9]{32}", plan.get("broker_epoch", ""))
                or not re.fullmatch(r"[a-f0-9]{64}", plan.get("device_fingerprint", ""))
                or not re.fullmatch(r"[a-f0-9]{64}", plan.get("expected_firmware_sha256", ""))
            ):
                raise ValueError("fixed_record_invalid")
            if record["valid"]:
                if record["parsed"] != parse_collected(raw, receipt):
                    raise ValueError("fixed_record_invalid")
            elif receipt["complete"]:
                try:
                    parse_collected(raw, receipt)
                except ValueError:
                    pass
                else:
                    raise ValueError("fixed_record_invalid")
            report = {
                "available": True,
                "valid": record["valid"],
                "receipt": receipt,
                "parsed": record["parsed"],
                "raw_base64": base64.b64encode(raw).decode("ascii"),
                "binding": {
                    "job_id": job["id"],
                    "plan_hash": job["plan_hash"],
                    "principal_id": principal,
                    "broker_epoch": plan["broker_epoch"],
                    "device_fingerprint": plan["device_fingerprint"],
                    "configured_expected_firmware_sha256": plan["expected_firmware_sha256"],
                    "protocol_parser_commit": PARSER_COMMIT,
                    "device_reported_session": (
                        record["parsed"]["session"] if record["parsed"] is not None else None
                    ),
                    "installed_boot_source_image": "unverified",
                },
            }
            if len(json.dumps(report, separators=(",", ":")).encode()) > MAX_REPORT_BYTES:
                raise ValueError("fixed_report_limit")
        except (KeyError, TypeError, ValueError):
            result["fixed_trial"] = {"available": False, "reason": "fixed_details_unavailable"}
        else:
            result["fixed_trial"] = report
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
