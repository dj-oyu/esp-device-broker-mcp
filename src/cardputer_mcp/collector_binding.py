"""Read-only binding of fixed trials to a successful owned flash receipt.

Configuration hashes and reported boot text alone never select comparison images.
The selected flash must still be the registered device's current durable target.
"""

import hashlib
import re
from typing import TYPE_CHECKING, Any, Literal

from pydantic import ConfigDict, Field

from .build_artifacts import manifest_digest
from .diagnostic_contracts import select_contract
from .diagnostic_legacy import J1_COMMIT
from .diagnostic_registry import (
    same_diagnostic_selection,
    validate_diagnostic_selection,
    validate_entry_binding,
)
from .fixed_diagnostic import (
    DiagnosticContext,
    prepare_fixed_diagnostic,
    verify_build_chain,
    verify_owned_flash,
)
from .manifest_export import validate_plan_export
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
    derby_dma_mode: Literal["ON", "OFF"] | None = None
    expected_elf_sha256: Digest
    expected_app_bin_sha256: Digest
    diagnostic_entry_sha256: Digest | None = None


def validate_collector_schema(plan: dict[str, Any]) -> None:
    binding = CollectorBinding.model_validate(plan["collector_binding"])
    record = plan["collector_binding"]
    if "diagnostic_entry_sha256" in record:
        _digest(record["diagnostic_entry_sha256"])
    elif binding.source_commit != J1_COMMIT or "derby_dma_mode" in record:
        raise ValueError("diagnostic_entry_binding_required")
    if "derby_dma_mode" in record and record["derby_dma_mode"] is None:
        raise ValueError("collector_mode_binding_invalid")
    if select_contract(binding.derby_detail_mode, binding.derby_dma_mode) is None:
        raise ValueError("collector_mode_binding_invalid")
    if (
        plan.get("test") != "derby_fixed_workload"
        or plan.get("flash_job_id") != binding.flash_job_id
        or plan.get("device_fingerprint") != binding.device_fingerprint
        or plan.get("expected_firmware_sha256") != binding.expected_elf_sha256
        or plan.get("firmware_verification") != "successful_flash_receipt"
    ):
        raise ValueError("collector_flash_binding_invalid")


def _validate_selection_record(provenance: dict[str, Any], record: dict[str, Any]) -> None:
    # Presence is significant: explicit null is not an omitted legacy axis.
    if "derby_dma_mode" in record and record["derby_dma_mode"] is None:
        raise ValueError("collector_mode_binding_invalid")
    entry = validate_diagnostic_selection(
        provenance, record.get("derby_detail_mode"), record.get("derby_dma_mode")
    )
    if entry is None or (entry.contract == "j1-fixed-v1" and "derby_dma_mode" in record):
        raise ValueError("collector_mode_binding_invalid")


def _digest(value: Any) -> str:
    if type(value) is not str or re.fullmatch(r"[a-f0-9]{64}", value) is None:
        raise ValueError("collector_flash_binding_invalid")
    return value


def _owned_flash(broker: "Broker", flash_job_id: str, principal: str) -> tuple[Any, Any]:
    store = broker._store()
    job = store.job(flash_job_id)
    plan = store.plan(job["plan_hash"]) if job else None
    plan = verify_owned_flash(DiagnosticContext(principal), flash_job_id, job, plan)
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
    from .diagnostic_dma_modeproof import verify_dma_modeproof_bytes
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
    validate_plan_export(flash, manifest)
    store = broker._store()
    build_job = store.job(flash["build_job_id"])
    build = store.plan(build_job["plan_hash"]) if build_job else None
    provenance, build_job, build = verify_build_chain(
        DiagnosticContext(principal), registration.project_id, flash, build_job, build, manifest
    )
    entry_hash = validate_entry_binding(provenance, manifest)
    validate_entry_binding(provenance, build)
    _validate_selection_record(provenance, build)
    _validate_selection_record(provenance, manifest)
    if not same_diagnostic_selection(build, manifest):
        raise ValueError("collector_build_source_binding_invalid")
    envelope = manifest.get("diagnostic_modeproof")
    artifacts = manifest.get("artifacts")
    if not isinstance(envelope, dict) or not isinstance(artifacts, list):
        raise ValueError("collector_mode_binding_invalid")
    entry = validate_diagnostic_selection(
        provenance, manifest["derby_detail_mode"], manifest.get("derby_dma_mode")
    )
    if entry is None:
        raise ValueError("collector_mode_binding_invalid")
    if entry.contract == "derby-dma-diagnostic-v1":
        verify_dma_modeproof_bytes(envelope, provenance, artifacts, files)
        if envelope["requested_dma_mode"] != manifest["derby_dma_mode"]:
            raise ValueError("collector_mode_binding_invalid")
    elif entry.contract == "j1-fixed-v1":
        verify_modeproof_bytes(envelope, provenance, artifacts, files)
    else:
        raise ValueError("collector_mode_binding_invalid")
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
        "diagnostic_entry_sha256": entry_hash,
        "derby_detail_mode": manifest["derby_detail_mode"],
        **({"derby_dma_mode": manifest["derby_dma_mode"]} if entry.dma_modes else {}),
        "expected_elf_sha256": elf,
        "expected_app_bin_sha256": app,
    }


def prepare_collector(broker: "Broker", flash_job_id: str, principal: str) -> dict[str, Any]:
    from .serial_backend import SerialBackend

    if not isinstance(broker.backend, SerialBackend):
        raise ValueError("collector_serial_backend_required")
    lease = getattr(broker, "session", None)
    context = DiagnosticContext(
        principal,
        origin=lease.owner if lease is not None else None,
        epoch=getattr(broker, "epoch", None),
        lease_id=lease.id if lease is not None else None,
    )
    return prepare_fixed_diagnostic(context, flash_job_id, _BrokerEvidence(broker), broker.backend)


class _BrokerEvidence:
    """Outer adapter; the inner use case never imports Broker or SQLite."""

    def __init__(self, broker: "Broker") -> None:
        self.broker = broker

    def resolve(self, flash_job_id: str, context: DiagnosticContext) -> dict[str, Any]:
        return resolve_collector(self.broker, flash_job_id, context.principal_id)


def collector_live(broker: "Broker", plan: dict[str, Any], principal: str) -> bool:
    """Callback-free durable guards; artifact reads happen before dispatch."""
    if "flash_job_id" not in plan:
        return "collector_binding" not in plan
    try:
        from .broker import digest_plan

        validate_collector_schema(plan)
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
            and binding["manifest_sha256"] == flash["manifest_sha256"]
            and binding["expected_elf_sha256"] == flash["expected_elf_sha256"]
            and binding["expected_app_bin_sha256"] == flash["expected_app_bin_sha256"]
            and binding["device_id"] == flash["device_id"]
            and binding["build_job_id"] == flash["build_job_id"]
            and build is not None
            and build["state"] == "succeeded"
            and build["plan_hash"] == binding["build_plan_hash"]
            and build_plan is not None
            and digest_plan(build_plan) == binding["build_plan_hash"]
            and build_plan.get("build_principal") == principal
            and build_plan.get("source_receipt") == binding["source_receipt"]
            and build_plan.get("source_provenance", {}).get("commit") == binding["source_commit"]
            and build_plan.get("derby_detail_mode") == binding["derby_detail_mode"]
            and ("derby_dma_mode" in build_plan) == ("derby_dma_mode" in binding)
            and build_plan.get("derby_dma_mode") == binding.get("derby_dma_mode")
            and validate_entry_binding(build_plan.get("source_provenance", {}), binding)
            == validate_entry_binding(build_plan.get("source_provenance", {}), build_plan)
        )
    except Exception:
        return False
