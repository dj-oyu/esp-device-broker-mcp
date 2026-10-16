"""Fail-closed local unattended permission for an exact owned source receipt."""

import hashlib
import math
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from .models import TestName

if TYPE_CHECKING:
    from .broker import Broker, ExecutionIntent, Session, WorkSession


def _digest(value: Any) -> str:
    return hashlib.sha256(value.model_dump_json().encode()).hexdigest()


@dataclass(frozen=True)
class FlashAuthorization:
    plan_hash: str
    registration_sha256: str
    project_sha256: str
    lease: "Session"
    intent: "ExecutionIntent"
    work: "WorkSession | None"
    build_hash: str = ""
    fetch_job_id: str = ""
    fetch_hash: str = ""
    fetch_receipt: str = ""


class FlashPolicies:
    def __init__(self, broker: "Broker") -> None:
        self.broker = broker

    def _candidate(self, plan: dict[str, Any]) -> FlashAuthorization | None:
        from .broker import ExecutionIntent, digest_plan

        broker = self.broker
        lease = broker.session
        registration = broker.settings.flash_devices.get(plan.get("device_id", ""))
        policy = registration.unattended_flash if registration is not None else None
        if (
            lease is None
            or registration is None
            or registration.allow_flash is not True
            or policy is None
            or policy.enabled is not True
            or plan.get("test") != TestName.FLASH.value
            or plan.get("flash_operation") != "flash"
            or policy.operation != plan["test"]
            or policy.principal_id != lease.principal_id
            or policy.principal_id != plan.get("flash_principal")
            or policy.device_id != plan.get("device_id")
            or policy.project_id != registration.project_id
            or plan.get("recovery_kind") != "artifact_bundle"
            or not plan.get("lkg_receipt")
            or plan.get("backup_job_id") is not None
        ):
            return None
        project = broker.settings.build_projects.get(policy.project_id)
        if (
            project is None
            or project.source_repository is None
            or project.source_repository.github_repository != policy.source_repository
        ):
            return None
        digest = digest_plan(plan)
        intent = ExecutionIntent(
            broker.epoch,
            digest,
            plan["job_id"],
            lease.id,
            lease.principal_id,
            lease.owner,
            lease.work_session_id,
            lease.deadline,
        )
        return FlashAuthorization(
            digest,
            _digest(registration),
            _digest(project),
            lease,
            intent,
            broker.work_sessions.get(lease.work_session_id or ""),
        )

    def _binding_live(
        self, token: FlashAuthorization, plan: dict[str, Any], *, committing: bool = False
    ) -> bool:
        """No external callbacks or source rehashes; safe after every auth callback."""
        from .broker import digest_plan

        broker = self.broker
        current = self._candidate(plan)
        job = broker._store().job(token.intent.job_id)
        # Only the already-running operation may follow explicit lease renewals.
        # Admission (including queued dispatch) retains its original deadline.
        running = bool(
            job is not None
            and broker.active_job == token.intent.job_id
            and (
                job["state"] == "running"
                or (
                    committing
                    and broker._store().db.in_transaction
                    and job["state"] == "succeeded"
                    and job["result"] == "firmware_flash_completed"
                )
            )
        )
        if (
            current is None
            or current.lease is not token.lease
            or current.work is not token.work
            or current.plan_hash != token.plan_hash
            or current.registration_sha256 != token.registration_sha256
            or current.project_sha256 != token.project_sha256
            or plan.get("registration_sha256") != token.registration_sha256
            or not (
                broker._execution_lease_binding_live(token.intent)
                if running
                else broker._execution_intent_binding_live(token.intent)
            )
            or not math.isfinite(broker.clock())
            or broker.closing
            or broker._store().needs_review()
            or not broker._device_lock.is_locked
            or not broker._state_lock.is_locked
            or plan.get("broker_epoch") != broker.epoch
            or plan.get("session_id") != token.lease.id
            or plan.get("backend") != broker.backend.name
            or plan.get("device") != broker.backend.device
        ):
            return False
        saved = broker._store().plan(token.plan_hash)
        return bool(
            saved is not None
            and saved == plan
            and digest_plan(saved) == token.plan_hash
            and (
                job is None
                or (
                    job["plan_hash"] == token.plan_hash
                    and (
                        job["state"] in ("queued", "running")
                        or (
                            committing
                            and broker._store().db.in_transaction
                            and job["state"] == "succeeded"
                            and job["result"] == "firmware_flash_completed"
                        )
                    )
                    and (job["state"] == "queued" or broker.active_job == token.intent.job_id)
                )
            )
        )

    def _live(
        self, token: FlashAuthorization, plan: dict[str, Any], *, committing: bool = False
    ) -> bool:
        from .broker import digest_plan

        if not self._binding_live(token, plan, committing=committing):
            return False
        for job_id, digest in (
            (plan["build_job_id"], token.build_hash),
            (token.fetch_job_id, token.fetch_hash),
        ):
            job = self.broker._store().job(job_id)
            saved = self.broker._store().plan(digest) if digest else None
            if (
                job is None
                or job["state"] != "succeeded"
                or job["plan_hash"] != digest
                or saved is None
                or digest_plan(saved) != digest
            ):
                return False
            if job_id == token.fetch_job_id:
                diagnostic = job.get("diagnostic")
                source = diagnostic.get("source") if isinstance(diagnostic, dict) else None
                if (
                    not token.fetch_receipt
                    or not isinstance(source, dict)
                    or source.get("source_receipt") != token.fetch_receipt
                ):
                    return False
        return True

    def live(
        self, token: FlashAuthorization, plan: dict[str, Any], *, committing: bool = False
    ) -> bool:
        try:
            return self._live(token, plan, committing=committing)
        except Exception:
            return False

    def authorize(self, plan: dict[str, Any]) -> FlashAuthorization | None:
        """Full disk provenance/preflight, then current authority and final binding."""
        try:
            token = self._candidate(plan)
            if token is None or not self._binding_live(token, plan):
                return None
            broker = self.broker
            registration = broker.settings.flash_devices[plan["device_id"]]
            policy = registration.unattended_flash
            assert policy is not None
            broker.firmware_flash.settings = broker.settings
            broker.firmware_build.settings = broker.settings
            broker.firmware_source.settings = broker.settings
            details = broker.firmware_flash.prepare_plan(plan)
            if any(plan.get(key) != value for key, value in details.items()):
                return None
            job = broker._store().job(plan["build_job_id"])
            build = broker._store().plan(job["plan_hash"]) if job else None
            if (
                job is None
                or job["state"] != "succeeded"
                or build is None
                or build.get("test") != TestName.BUILD.value
                or build.get("project_id") != policy.project_id
                or build.get("build_principal") != policy.principal_id
                or build.get("source_receipt") != policy.source_receipt
            ):
                return None
            broker.firmware_source.authorize_receipt(
                policy.project_id, policy.source_receipt, policy.principal_id
            )
            _, metadata = broker.firmware_source.snapshots.resolve(
                policy.project_id, policy.source_receipt, policy.principal_id
            )
            _, manifest = broker.firmware_build.resolve_flash_manifest(
                plan["build_job_id"], policy.principal_id, policy.project_id
            )
            provenance = manifest.get("source_provenance")
            if (
                not isinstance(provenance, dict)
                or build.get("source_provenance") != provenance
                or provenance.get("source_receipt") != policy.source_receipt
                or metadata["repository"] != policy.source_repository
                or metadata["commit"] != policy.source_commit
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
                return None
            fetch_job = broker._store().job(metadata["fetch_job_id"])
            if fetch_job is None:
                return None
            token = replace(
                token,
                build_hash=job["plan_hash"],
                fetch_job_id=metadata["fetch_job_id"],
                fetch_hash=fetch_job["plan_hash"],
                fetch_receipt=policy.source_receipt,
            )
            if (
                broker.principal_valid is None
                or broker.principal_valid(
                    policy.principal_id, token.intent.owner, frozenset({"read", "control"})
                )
                is not True
            ):
                return None
            return token if self.live(token, plan) else None
        except Exception:
            # A missing/tampered receipt, failed callbacks or stale config grants nothing.
            return None

    def validate(self, token: FlashAuthorization, plan: dict[str, Any]) -> bool:
        current = self.authorize(plan)
        return bool(
            current is not None
            and current.lease is token.lease
            and current.plan_hash == token.plan_hash
            and current.registration_sha256 == token.registration_sha256
            and current.project_sha256 == token.project_sha256
            and current.fetch_receipt == token.fetch_receipt
            and self.live(token, plan)
        )
