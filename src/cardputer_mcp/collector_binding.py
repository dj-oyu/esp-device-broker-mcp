"""Read-only binding of fixed trials to a successful owned flash receipt.

Configuration hashes and reported boot text alone never select comparison images.
The selected flash must still be the registered device's current durable target.
"""

import hashlib
import re
from typing import TYPE_CHECKING, Any, Literal

from pydantic import ConfigDict, Field

from .build_artifacts import manifest_digest
from .models import Digest, Identifier, StrictModel

if TYPE_CHECKING:
    from .broker import Broker


class CollectorBinding(StrictModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    schema_version: Literal[1]
    principal_id: str = Field(pattern=r"^[A-Za-z0-9_:.@+-]{1,128}$")
    device_id: Identifier
    device_fingerprint: Digest
    flash_job_id: Identifier
    flash_plan_hash: Digest
    firmware_receipt: Digest
    build_job_id: Identifier
    build_plan_hash: Digest
    source_receipt: Digest
    source_commit: str = Field(pattern=r"^[a-f0-9]{40}$")
    manifest_sha256: Digest
    modeproof_sha256: Digest
    derby_detail_mode: Literal["ON", "OFF"]
    expected_elf_sha256: Digest
    expected_app_bin_sha256: Digest


def validate_collector_schema(plan: dict[str, Any]) -> None:
    binding = CollectorBinding.model_validate(plan["collector_binding"])
    if (
        plan.get("test") != "derby_fixed_workload"
        or plan.get("flash_job_id") != binding.flash_job_id
        or plan.get("device_fingerprint") != binding.device_fingerprint
        or plan.get("expected_firmware_sha256") != binding.expected_elf_sha256
        or plan.get("firmware_verification") != "successful_flash_receipt"
    ):
        raise ValueError("collector_flash_binding_invalid")


def _digest(value: Any) -> str:
    if type(value) is not str or re.fullmatch(r"[a-f0-9]{64}", value) is None:
        raise ValueError("collector_flash_binding_invalid")
    return value


def _owned_flash(broker: "Broker", flash_job_id: str, principal: str) -> tuple[Any, Any]:
    from .broker import digest_plan

    store = broker._store()
    job = store.job(flash_job_id)
    plan = store.plan(job["plan_hash"]) if job else None
    if (
        not principal
        or job is None
        or job["state"] != "succeeded"
        or plan is None
        or digest_plan(plan) != job["plan_hash"]
        or plan.get("job_id") != flash_job_id
        or plan.get("test") != "firmware_flash"
        or plan.get("flash_operation") != "flash"
        or plan.get("flash_principal") != principal
    ):
        raise ValueError("collector_successful_owned_flash_required")
    summary = store.firmware_summary(flash_job_id, principal)
    current = store.firmware_current(plan["device_id"], principal)
    if (
        not isinstance(summary, dict)
        or any(
            summary.get(key) is not True
            for key in (
                "readback_verified",
                "passive_boot_verified",
                "boot_log_matches_expected_artifact",
            )
        )
        or summary.get("verification_basis") != "full_segment_sha256_readback_and_boot_elf_prefix"
        or current is None
        or current["completed_job"] != flash_job_id
        or current["receipt"] != summary.get("last_known_good_receipt")
        or current["kind"] != "artifact_bundle"
    ):
        raise ValueError("collector_current_verified_flash_required")
    return plan, current


def resolve_collector(broker: "Broker", flash_job_id: str, principal: str) -> dict[str, Any]:
    """Verify retained bytes, source and build chain without creating any job."""
    from .broker import digest_plan
    from .diagnostic_modeproof import verify_modeproof_bytes

    flash, current = _owned_flash(broker, flash_job_id, principal)
    registration = broker.settings.flash_devices.get(flash["device_id"])
    serial = broker.settings.serial
    if (
        registration is None
        or serial is None
        or serial.selector != registration.selector
        or hashlib.sha256(registration.model_dump_json().encode()).hexdigest()
        != flash.get("registration_sha256")
    ):
        raise ValueError("collector_device_binding_invalid")
    broker.firmware_flash.settings = broker.settings
    _, metadata, files = broker.firmware_flash._known(
        registration, flash["device_id"], principal, current["receipt"]
    )
    manifest = metadata.get("manifest")
    if (
        not isinstance(manifest, dict)
        or manifest_digest(manifest) != flash.get("manifest_sha256")
        or metadata.get("expected_elf_sha256") != flash.get("expected_elf_sha256")
        or metadata.get("expected_app_bin_sha256") != flash.get("expected_app_bin_sha256")
    ):
        raise ValueError("collector_image_binding_invalid")
    store = broker._store()
    build_job = store.job(flash["build_job_id"])
    build = store.plan(build_job["plan_hash"]) if build_job else None
    provenance = manifest.get("source_provenance")
    if (
        build_job is None
        or build_job["state"] != "succeeded"
        or build is None
        or digest_plan(build) != build_job["plan_hash"]
        or build.get("test") != "firmware_build"
        or build.get("job_id") != flash["build_job_id"]
        or build.get("build_principal") != principal
        or build.get("project_id") != registration.project_id
        or manifest.get("build_job_id") != flash["build_job_id"]
        or manifest.get("build_plan_hash") != build_job["plan_hash"]
        or not isinstance(provenance, dict)
        or build.get("source_provenance") != provenance
        or build.get("source_receipt") != provenance.get("source_receipt")
        or build.get("derby_detail_mode") != manifest.get("derby_detail_mode")
    ):
        raise ValueError("collector_build_source_binding_invalid")
    envelope = manifest.get("diagnostic_modeproof")
    artifacts = manifest.get("artifacts")
    if not isinstance(envelope, dict) or not isinstance(artifacts, list):
        raise ValueError("collector_mode_binding_invalid")
    verify_modeproof_bytes(envelope, provenance, artifacts, files)
    if envelope["requested_mode"] != manifest["derby_detail_mode"]:
        raise ValueError("collector_mode_binding_invalid")
    elf = _digest(flash.get("expected_elf_sha256"))
    app = _digest(flash.get("expected_app_bin_sha256"))
    if elf not in {hashlib.sha256(v).hexdigest() for k, v in files.items() if k.endswith(".elf")}:
        raise ValueError("collector_image_binding_invalid")
    if app not in {hashlib.sha256(v).hexdigest() for k, v in files.items() if k.endswith(".bin")}:
        raise ValueError("collector_image_binding_invalid")
    return {
        "schema_version": 1,
        "principal_id": principal,
        "device_id": flash["device_id"],
        "device_fingerprint": flash["device_fingerprint"],
        "flash_job_id": flash_job_id,
        "flash_plan_hash": digest_plan(flash),
        "firmware_receipt": current["receipt"],
        "build_job_id": flash["build_job_id"],
        "build_plan_hash": build_job["plan_hash"],
        "source_receipt": provenance["source_receipt"],
        "source_commit": provenance["commit"],
        "manifest_sha256": flash["manifest_sha256"],
        "modeproof_sha256": envelope["proof_sha256"],
        "derby_detail_mode": manifest["derby_detail_mode"],
        "expected_elf_sha256": elf,
        "expected_app_bin_sha256": app,
    }


def prepare_collector(broker: "Broker", flash_job_id: str, principal: str) -> dict[str, Any]:
    from .serial_backend import SerialBackend

    if not isinstance(broker.backend, SerialBackend):
        raise ValueError("collector_serial_backend_required")
    binding = resolve_collector(broker, flash_job_id, principal)
    details = broker.backend.prepare_fixed_receipt(binding["expected_elf_sha256"])
    if details["device_fingerprint"] != binding["device_fingerprint"]:
        raise ValueError("collector_device_binding_invalid")
    return {**details, "flash_job_id": flash_job_id, "collector_binding": binding}


def collector_live(broker: "Broker", plan: dict[str, Any], principal: str) -> bool:
    """Callback-free durable guards; artifact reads happen before dispatch."""
    if "flash_job_id" not in plan:
        return "collector_binding" not in plan
    try:
        from .broker import digest_plan

        flash, current = _owned_flash(broker, plan["flash_job_id"], principal)
        binding = plan["collector_binding"]
        build = broker._store().job(binding["build_job_id"])
        build_plan = broker._store().plan(binding["build_plan_hash"])
        return bool(
            binding["principal_id"] == principal
            and binding["flash_job_id"] == plan["flash_job_id"]
            and binding["flash_plan_hash"] == digest_plan(flash)
            and binding["firmware_receipt"] == current["receipt"]
            and binding["device_fingerprint"] == plan["device_fingerprint"]
            and binding["expected_elf_sha256"] == plan["expected_firmware_sha256"]
            and build is not None
            and build["state"] == "succeeded"
            and build["plan_hash"] == binding["build_plan_hash"]
            and build_plan is not None
            and digest_plan(build_plan) == binding["build_plan_hash"]
        )
    except Exception:
        return False
