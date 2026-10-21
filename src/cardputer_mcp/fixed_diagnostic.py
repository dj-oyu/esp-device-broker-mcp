"""Inner fixed-diagnostic rules and ports (U2/U5, I1/I2/I5/I8).

No transport, database, firmware registry or device implementation belongs here.
The caller supplies a subject resolved by existing authentication/lease guards;
this context carries identity, not a new grant or a client-supplied authority.
Current authorization, cancellation and close checks remain at their boundaries.
"""

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Protocol


def plan_digest(plan: dict[str, Any]) -> str:
    raw = json.dumps(plan, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()


@dataclass(frozen=True)
class DiagnosticContext:
    principal_id: str
    origin: str | None = None
    epoch: str | None = None
    lease_id: str | None = None


class DiagnosticEvidence(Protocol):
    def resolve(self, flash_job_id: str, context: DiagnosticContext) -> dict[str, Any]: ...


class DiagnosticDevice(Protocol):
    def prepare_fixed_receipt(self, elf_sha256: str, /) -> dict[str, Any]: ...


class DiagnosticCompletion(Protocol):
    def finish_fixed_trial(
        self,
        job_id: str,
        plan_hash: str,
        principal_id: str,
        state: str,
        code: str,
        trial: Any,
        diagnostic: dict[str, Any] | None = None,
    ) -> None: ...


def finish_fixed_diagnostic(
    context: DiagnosticContext,
    job_id: str,
    plan_hash: str,
    code: str,
    capture: Any,
    interrupted: bool,
    completion: DiagnosticCompletion,
) -> None:
    """I8: one atomic terminal/capture publication, after existing final guards.

    The runner adapter establishes transport cleanup; firmware quality fields
    never authorize this call. The repository rechecks the exact running job.
    """
    if not context.principal_id:
        raise ValueError("fixed_trial_result_invalid")
    completion.finish_fixed_trial(
        job_id,
        plan_hash,
        context.principal_id,
        "needs_review" if interrupted else "succeeded",
        "operation_interrupted_review_required" if interrupted else code,
        capture,
    )


def prepare_fixed_diagnostic(
    context: DiagnosticContext,
    flash_job_id: str,
    evidence: DiagnosticEvidence,
    device: DiagnosticDevice,
) -> dict[str, Any]:
    """Prepare one receipt-bound operation; never execute or acquire a device."""
    if not context.principal_id:
        raise ValueError("collector_successful_owned_flash_required")
    binding = evidence.resolve(flash_job_id, context)
    if binding["principal_id"] != context.principal_id or binding["flash_job_id"] != flash_job_id:
        raise ValueError("collector_successful_owned_flash_required")
    details = device.prepare_fixed_receipt(binding["expected_elf_sha256"])
    if details["device_fingerprint"] != binding["device_fingerprint"]:
        raise ValueError("collector_device_binding_invalid")
    return {**details, "flash_job_id": flash_job_id, "collector_binding": binding}


def verify_owned_flash(
    context: DiagnosticContext,
    flash_job_id: str,
    job: dict[str, Any] | None,
    plan: dict[str, Any] | None,
) -> dict[str, Any]:
    if (
        not context.principal_id
        or job is None
        or job["state"] != "succeeded"
        or plan is None
        or plan_digest(plan) != job["plan_hash"]
        or plan.get("job_id") != flash_job_id
        or plan.get("test") != "firmware_flash"
        or plan.get("flash_operation") != "flash"
        or plan.get("flash_principal") != context.principal_id
    ):
        raise ValueError("collector_successful_owned_flash_required")
    return plan


def verify_build_chain(
    context: DiagnosticContext,
    project_id: str,
    flash: dict[str, Any],
    build_job: dict[str, Any] | None,
    build: dict[str, Any] | None,
    manifest: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    provenance = manifest.get("source_provenance")
    if (
        build_job is None
        or build_job["state"] != "succeeded"
        or build is None
        or plan_digest(build) != build_job["plan_hash"]
        or build.get("test") != "firmware_build"
        or build.get("job_id") != flash["build_job_id"]
        or build.get("build_principal") != context.principal_id
        or build.get("project_id") != project_id
        or manifest.get("build_job_id") != flash["build_job_id"]
        or manifest.get("build_plan_hash") != build_job["plan_hash"]
        or not isinstance(provenance, dict)
        or build.get("source_provenance") != provenance
        or build.get("source_receipt") != provenance.get("source_receipt")
        or build.get("derby_detail_mode") != manifest.get("derby_detail_mode")
    ):
        raise ValueError("collector_build_source_binding_invalid")
    return provenance, build_job, build
