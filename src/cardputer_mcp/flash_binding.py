"""Immutable flash provenance checks; these never grant execution authority."""

import hashlib
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .diagnostic_registry import same_diagnostic_selection
from .models import TestName

if TYPE_CHECKING:
    from .broker import Broker


@dataclass(frozen=True)
class FlashBinding:
    plan_hash: str
    project_sha256: str = ""
    build_hash: str = ""
    fetch_job_id: str = ""
    fetch_hash: str = ""
    fetch_receipt: str = ""


def _project_hash(broker: "Broker", plan: dict[str, Any]) -> str:
    registration = broker.settings.flash_devices[plan["device_id"]]
    project = broker.settings.build_projects[registration.project_id]
    return hashlib.sha256(project.model_dump_json().encode()).hexdigest()


def prepare_binding(broker: "Broker", plan: dict[str, Any]) -> FlashBinding:
    """Validate registered disk inputs and snapshot their durable identities."""
    from .broker import BrokerError, digest_plan

    try:
        binding = FlashBinding(digest_plan(plan))
        if plan["test"] != TestName.FLASH.value:
            return binding
        broker.firmware_flash.settings = broker.settings
        broker.firmware_build.settings = broker.settings
        broker.firmware_source.settings = broker.settings
        details = broker.firmware_flash.prepare_plan(plan)
        if any(plan.get(key) != value for key, value in details.items()):
            raise ValueError
        registration = broker.settings.flash_devices[plan["device_id"]]
        project_id, principal = registration.project_id, plan["flash_principal"]
        store = broker._store()
        job = store.job(plan["build_job_id"])
        build = store.plan(job["plan_hash"]) if job else None
        if (
            job is None
            or job["state"] != "succeeded"
            or build is None
            or digest_plan(build) != job["plan_hash"]
            or build.get("test") != TestName.BUILD.value
            or build.get("project_id") != project_id
            or build.get("build_principal") != principal
        ):
            raise ValueError
        _, manifest = broker.firmware_build.resolve_flash_manifest(
            plan["build_job_id"], principal, project_id
        )
        if not same_diagnostic_selection(manifest, build):
            raise ValueError
        receipt = build.get("source_receipt")
        provenance = manifest.get("source_provenance")
        if receipt is None:
            if provenance is not None or build.get("source_provenance") is not None:
                raise ValueError
            return FlashBinding(binding.plan_hash, _project_hash(broker, plan), job["plan_hash"])
        broker.firmware_source.authorize_receipt(project_id, receipt, principal)
        _, metadata = broker.firmware_source.snapshots.resolve(project_id, receipt, principal)
        if (
            not isinstance(provenance, dict)
            or build.get("source_provenance") != provenance
            or provenance.get("source_receipt") != receipt
            or provenance.get("submodules") != metadata.get("submodules")
            or provenance.get("dependency_policy")
            != (
                "gitlinks_fixed_commits_no_fetch"
                if metadata["schema_version"] == 2
                else "vendored_components_no_fetch"
            )
            or any(
                provenance.get(key) != metadata[key]
                for key in ("repository", "commit", "tree_sha1", "source_sha256")
            )
        ):
            raise ValueError
        fetch = store.job(metadata["fetch_job_id"])
        if fetch is None:
            raise ValueError
        binding = FlashBinding(
            binding.plan_hash,
            _project_hash(broker, plan),
            job["plan_hash"],
            metadata["fetch_job_id"],
            fetch["plan_hash"],
            receipt,
        )
        if not live_binding(broker, binding, plan):
            raise ValueError
        return binding
    except Exception:
        raise BrokerError("flash_binding_invalid") from None


def live_binding(broker: "Broker", binding: FlashBinding, plan: dict[str, Any]) -> bool:
    """Callback-free durable provenance checks for use after authentication."""
    from .broker import digest_plan

    try:
        store = broker._store()
        if digest_plan(plan) != binding.plan_hash or store.plan(binding.plan_hash) != plan:
            return False
        if plan["test"] != TestName.FLASH.value:
            return True
        if _project_hash(broker, plan) != binding.project_sha256:
            return False
        for job_id, digest in (
            (plan["build_job_id"], binding.build_hash),
            (binding.fetch_job_id, binding.fetch_hash),
        ):
            if not job_id:
                continue
            job = store.job(job_id)
            saved = store.plan(digest)
            if (
                job is None
                or job["state"] != "succeeded"
                or job["plan_hash"] != digest
                or saved is None
                or digest_plan(saved) != digest
            ):
                return False
            if job_id == binding.fetch_job_id:
                diagnostic = job.get("diagnostic")
                source = diagnostic.get("source") if isinstance(diagnostic, dict) else None
                if (
                    not isinstance(source, dict)
                    or source.get("source_receipt") != binding.fetch_receipt
                ):
                    return False
        return True
    except Exception:
        return False


def validate_binding(broker: "Broker", binding: FlashBinding, plan: dict[str, Any]) -> bool:
    """Revalidate disk inputs before dispatch, then check the captured identities."""
    try:
        return prepare_binding(broker, plan) == binding and live_binding(broker, binding, plan)
    except Exception:
        return False
