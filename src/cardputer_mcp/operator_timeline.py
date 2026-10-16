"""Bounded, read-only job continuity for the authenticated local operator.

Durable job outcomes outlive the short-lived pixel cache. Reviews are borrowed
from PlanApprovals, never reconstructed as a second authorization mechanism.
"""

import math
import re
from datetime import UTC, datetime
from typing import Any

from .backend import DeviceOperationFailure
from .broker import Broker, BrokerError
from .device_observation import FIRMWARE_LIMITATIONS, REDACTION_LIMITATIONS
from .plan_approval import _ATTESTATIONS, PlanApprovals
from .source_snapshot import SOURCE_ERROR_CODES

MAX_CARDS = 32
STATES = frozenset(
    {
        "prepared",
        "approval_required",
        "awaiting_approval",
        "approved",
        "ready",
        "queued",
        "running",
        "succeeded",
        "failed",
        "cancelled",
        "needs_review",
        "expired",
        "unknown",
    }
)
STAGES = frozenset(
    {
        "mock_capture",
        "mock_flash",
        "mock_boot",
        "mock_test",
        "mock_diagnostics",
        "serial_open",
        "serial_close",
        "serial_close_unconfirmed",
        "passive_observe",
        "firmware_observe",
        "restart_and_observe",
        "serial_restart_requested",
        "serial_restart_complete",
        "serial_reconnect_wait",
        "serial_reopened",
        "hello",
        "capture_home",
        "serial_home_ready",
        "serial_category_apps",
        "serial_row_hello",
        "serial_identity_hello",
        "serial_hello_ready",
        "serial_hello_count_one",
        "serial_frame_presented",
        "serial_app_stopped",
        "source_download",
        "source_validate",
        "source_publish",
        "idf_profile",
        "idf_diagnostics",
        "idf_build",
        "flash_security_probe",
        "flash_backup_or_verify",
        "flash_backup_read",
        "flash_backup_prewrite_check",
        "flash_write_or_restore",
        "flash_write",
        "flash_restore_write",
        "flash_readback_verify",
        "flash_boot_observe",
        "flash_close",
    }
) | frozenset("serial_wait_" + step for step in DeviceOperationFailure.STEPS)
RESULTS = (
    DeviceOperationFailure.CODES
    | SOURCE_ERROR_CODES
    | frozenset(
        {
            "mock_pass",
            "synthetic_capture_not_device",
            "render_buffer_captured_lcd_unverified",
            "serial_observation_completed_no_commands",
            "firmware_observation_completed_no_commands",
            "restart_observation_completed_unverified",
            "hello_markers_passed_physical_checks_not_assessed",
            "broker_restart_no_replay",
            "broker_shutdown",
            "lease_expired",
            "session_released",
            "work_session_closed",
            "operator_released_idle",
            "recovery_review_required",
            "client_cancelled_queued",
            "client_cancelled_awaiting_approval",
            "execution_intent_expired",
            "execution_intent_not_available",
            "session_not_available",
            "plan_not_available",
            "principal_or_lease_no_longer_valid",
            "capture_policy_no_longer_valid",
            "flash_policy_no_longer_valid",
            "local_home_diagnostic_no_longer_valid",
            "device_disconnected_no_retry",
            "backend_error_no_retry",
            "source_snapshot_fetched_no_build_no_flash",
            "firmware_build_succeeded_no_flash",
            "firmware_build_failed",
            "firmware_backup_completed",
            "firmware_flash_completed",
            "firmware_restore_completed",
            "client_cancelled",
            "operator_cancelled",
            "execution_deadline_expired",
            "profile_or_principal_no_longer_valid",
            "profile_plan_inconsistent",
            "build_process_exit_unconfirmed",
            "build_output_release_unconfirmed",
            "registered_project_state_inconsistent",
            "idf_version_diagnostic_failed",
            "idf_version_mismatch_no_fallback",
            "external_python_diagnostic_failed",
            "external_toolchain_missing",
            "registered_tool_diagnostic_failed",
            "registered_tool_version_mismatch",
            "registered_tool_resolution_ambiguous",
            "external_toolchain_resolution_changed",
            "node_requires_explicit_local_registration",
            "build_inputs_changed",
            "profile_export_rejected_or_failed",
            "external_toolchain_diagnostic_failed",
            "build_flash_layout_invalid",
            "build_flash_security_invalid",
            "flash_cancelled_needs_review",
            "flash_authority_changed_needs_review",
            "flash_inputs_changed_needs_review",
            "flash_device_busy_or_uncertain",
            "flash_backup_incomplete_needs_review",
            "flash_backup_stale_needs_review",
            "flash_readback_mismatch_needs_review",
            "flash_boot_unverified_needs_review",
            "flash_timeout_needs_review",
            "flash_failed_needs_review",
            "flash_shutdown_uncertain_needs_review",
            "flash_read_progress_invalid_needs_review",
            "flash_read_deadline_needs_review",
            "flash_read_stalled_needs_review",
            "firmware_final_commit_failed",
            "backup_linked_path_not_supported",
            "backup_missing_or_changed",
            "backup_owner_mismatch",
            "backup_persistence_failed_needs_review",
            "backup_receipt_mismatch",
            "backup_registered_device_mismatch",
            "build_flash_layout_receipt_required",
            "build_flash_security_receipt_required",
            "build_manifest_not_flash_eligible",
            "build_manifest_resolver_unavailable",
            "build_output_identity_invalid",
            "build_output_identity_mismatch",
            "build_output_in_use",
            "build_output_insufficient_space",
            "build_output_linked",
            "build_output_path_too_long",
            "build_output_root_invalid",
            "build_output_storage_unavailable",
            "build_security_configuration_not_authorized",
            "device_security_or_identity_not_verified",
            "duplicate_flash_layout_key",
            "encrypted_flash_not_authorized",
            "flash_app_elf_receipt_required",
            "flash_app_receipt_required",
            "flash_approval_or_handoff_required",
            "flash_artifact_hash_mismatch",
            "flash_artifact_missing",
            "flash_artifact_size_mismatch",
            "flash_authenticated_principal_required",
            "flash_boot_device_changed",
            "flash_boot_observation_missing",
            "flash_build_project_mismatch",
            "flash_cancelled_or_authority_changed_needs_review",
            "flash_capacity_mismatch",
            "flash_expected_elf_receipt_required",
            "flash_layout_target_mismatch",
            "flash_manifest_changed_needs_review",
            "flash_native_failed_needs_review",
            "flash_native_read_incomplete",
            "flash_read_size_invalid",
            "flash_reader_handoff_unconfirmed",
            "flash_registered_device_changed",
            "flash_registration_selector_mismatch",
            "flash_serial_opt_in_required",
            "invalid_backup_job",
            "invalid_build_flash_security",
            "invalid_flash_artifact_map",
            "invalid_flash_layout",
            "invalid_flash_offset",
            "invalid_flash_receipt",
            "invalid_internal_build_manifest_context",
            "linked_esptool_not_supported",
            "linked_flash_artifact",
            "lkg_bytes_changed",
            "lkg_current_firmware_changed_needs_review",
            "lkg_data_or_unknown_write_not_allowed",
            "lkg_device_binding_changed",
            "lkg_firmware_range_not_allowed",
            "lkg_firmware_role_changed",
            "lkg_firmware_roles_required",
            "lkg_initial_partition_change_not_allowed",
            "lkg_object_changed",
            "lkg_object_conflict",
            "lkg_object_missing_or_changed",
            "lkg_partition_checksum_invalid",
            "lkg_partition_label_invalid",
            "lkg_partition_layout_unsupported",
            "lkg_partition_or_security_change_not_allowed",
            "lkg_partition_overlap",
            "lkg_persistence_failed_needs_review",
            "lkg_promotion_cancelled_needs_review",
            "lkg_publication_failed",
            "lkg_receipt_invalid",
            "lkg_receipt_kind_changed",
            "lkg_restore_layout_evidence_required",
            "lkg_restore_partition_changed_needs_review",
            "lkg_snapshot_missing_or_changed",
            "lkg_snapshot_size_invalid",
            "overlapping_flash_erase_sectors",
            "private_backup_acl_confirmation_required",
            "private_backup_directory_required",
            "registered_build_project_required",
            "registered_esptool_limit",
            "registered_esptool_missing",
            "registered_flash_device_required",
            "registered_flash_operation_not_enabled",
            "registered_flash_tools_changed",
            "registered_private_backup_directory_required",
            "registered_windows_serial_required",
            "restore_original_firmware_receipt_required",
            "successful_owned_backup_required",
            "successful_owned_build_required",
            "unattested_flash_artifact",
            "unsafe_flash_artifact",
            "unsafe_flash_range",
            "unsupported_flash_arguments",
            "unsupported_flash_settings",
            "verified_device_backup_required",
            "verified_device_recovery_required",
            "verified_firmware_recovery_required",
            "verified_flash_capacity_required",
            "verified_lkg_or_initial_backup_required",
        }
    )
)
_JOB_STATES = frozenset(
    {"awaiting_approval", "queued", "running", "succeeded", "failed", "cancelled", "needs_review"}
)
_MAX_PLAN_BYTES = 16 * 1024
_ACTIVE = (
    "(j.state IN ('awaiting_approval','queued','running') "
    "OR (j.state='needs_review' AND j.reviewed=0))"
)
_UNEXECUTED = """CASE WHEN length(CAST(p.payload AS BLOB))<=? AND json_valid(p.payload)
    THEN NOT EXISTS (SELECT 1 FROM jobs j WHERE j.id=json_extract(p.payload,'$.job_id'))
    ELSE 0 END"""


def _matches(value: object, pattern: str) -> bool:
    return type(value) is str and re.fullmatch(pattern, value) is not None


def _utc(value: object) -> str | None:
    if not _matches(
        value,
        r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})?",
    ):
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC).isoformat()
    except (ValueError, OverflowError):
        return None


def _enum(value: object, values: frozenset[str]) -> str | None:
    return value if type(value) is str and value in values else None


def observation_summary(data: Any, job_id: str) -> dict[str, Any]:
    """Project bounded metadata only, never arbitrary device text or extension fields."""
    if type(data) is not dict or data.get("job_id") != job_id:
        raise ValueError("invalid_device_observation")
    remaining = data.get("remaining_seconds")
    if (
        not _matches(data.get("sha256"), r"[a-f0-9]{64}")
        or not _matches(data.get("recipient_principal"), r"[A-Za-z0-9_.:@-]{1,128}")
        or not isinstance(remaining, (int, float))
        or isinstance(remaining, bool)
        or not math.isfinite(remaining)
        or not 0 < remaining <= 300
    ):
        raise ValueError("invalid_device_observation")
    source = data.get("metadata")
    if type(source) is not dict or type(source.get("schema_version")) is not int:
        raise ValueError("invalid_device_observation")
    if source["schema_version"] != 1:
        raise ValueError("invalid_device_observation")
    metadata: dict[str, Any] = {"schema_version": 1}
    for key, limit in (
        ("bytes_received", 65536),
        ("complete_lines", 65536),
        ("retained_lines", 256),
        ("redacted_lines", 65536),
        ("omitted_lines", 65537),
        ("rendered_bytes", 32768),
    ):
        value = source.get(key)
        if type(value) is not int or not 0 <= value <= limit:
            raise ValueError("invalid_device_observation")
        metadata[key] = value
    for key in ("truncated", "pending_line_omitted", "partial"):
        if type(source.get(key)) is not bool:
            raise ValueError("invalid_device_observation")
        metadata[key] = source[key]
    for key in ("reset_sequence_issued", "capture_gap"):
        value = source.get(key, False)
        if type(value) is not bool:
            raise ValueError("invalid_device_observation")
        metadata[key] = value
    if type(source.get("stop_reason")) is not str or source["stop_reason"] not in (
        "finished",
        "receive_limit",
        "line_limit",
        "retained_line_limit",
        "rendered_text_limit",
    ):
        raise ValueError("invalid_device_observation")
    metadata["stop_reason"] = source["stop_reason"]
    metadata["redaction_limitations"] = REDACTION_LIMITATIONS
    firmware = source.get("firmware")
    if (
        type(firmware) is not dict
        or type(firmware.get("status")) is not str
        or firmware["status"] not in ("observed", "partial", "unavailable", "ambiguous")
        or type(firmware.get("reason")) is not str
        or firmware["reason"]
        not in (
            "complete_startup_block",
            "invalid_startup_field",
            "incomplete_startup_block",
            "no_startup_block",
            "multiple_startup_blocks",
            "contradictory_startup_fields",
            "duplicate_startup_fields",
        )
        or firmware.get("source_verified") is not False
        or firmware.get("binary_sha256_verified") is not False
    ):
        raise ValueError("invalid_device_observation")
    identity: dict[str, Any] = {
        "status": firmware["status"],
        "reason": firmware["reason"],
        "source_verified": False,
        "binary_sha256_verified": False,
        "limitations": FIRMWARE_LIMITATIONS,
    }
    for key, pattern in (
        ("project_name", r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,30}"),
        ("app_version", r"[A-Za-z0-9][A-Za-z0-9_.+-]{0,30}"),
        ("compile_time", r"[A-Za-z]{3} [ 0-9][0-9] [0-9]{4} [0-9]{2}:[0-9]{2}:[0-9]{2}"),
        ("elf_sha256_prefix", r"[a-f0-9]{8,16}"),
        ("idf_version", r"[A-Za-z0-9][A-Za-z0-9_.+-]{0,30}"),
    ):
        value = firmware.get(key)
        if value is not None and not _matches(value, pattern):
            raise ValueError("invalid_device_observation")
        identity[key] = value
    secure = firmware.get("secure_version")
    if secure is not None and (type(secure) is not int or not 0 <= secure <= 0xFFFFFFFF):
        raise ValueError("invalid_device_observation")
    identity["secure_version"] = secure
    metadata["firmware"] = identity
    return {
        "job_id": job_id,
        "sha256": data["sha256"],
        "recipient_principal": data["recipient_principal"],
        "remaining_seconds": float(remaining),
        "metadata": metadata,
    }


class OperatorTimeline:
    def __init__(self, broker: Broker, plans: PlanApprovals) -> None:
        self.broker, self.plans = broker, plans

    def _plan(self, plan_hash: object) -> dict[str, Any] | None:
        if not _matches(plan_hash, r"[a-f0-9]{64}"):
            return None
        try:
            # This checks bounded durable JSON, its digest, and the existing
            # public schema. It does not prepare/open a hardware backend.
            return self.plans._plan(str(plan_hash))
        except Exception:
            try:
                # Preserve the existing 16 KiB read bound for profile display too.
                db = self.broker._store().db
                bounded = db.execute(
                    "SELECT 1 FROM plans WHERE hash=? AND length(CAST(payload AS BLOB))<=?",
                    (str(plan_hash), 16 * 1024),
                ).fetchone()
                if bounded is None:
                    return None
                raw = self.broker._store().plan(str(plan_hash))
                if raw is None:
                    return None
                plan = self.broker.profile_jobs._plan(
                    str(plan_hash), raw.get("principal_id", ""), raw.get("origin", "")
                )
                # Display projection only: this never authorizes execution or approval.
                return {**plan, "backend": "registered_project", "session_id": ""}
            except Exception:
                return None

    def _images(self) -> dict[str, dict[str, Any]]:
        images: dict[str, dict[str, Any]] = {}
        try:
            broker = self.broker
            broker._prune_captures()
            if broker.closing or len(broker.captures) > 8:
                return images
            for job_id, retained in tuple(broker.captures.items()):
                if not _matches(job_id, r"[a-zA-Z0-9_-]{1,64}"):
                    continue
                if (
                    not broker._store()
                    .db.execute(
                        "SELECT 1 FROM jobs WHERE id=? AND state='succeeded' LIMIT 1", (job_id,)
                    )
                    .fetchone()
                ):
                    continue
                # Match operator_images using its existing projection without
                # Store.job's unrelated, potentially corrupt diagnostic JSON.
                row = broker._image_summary(job_id, retained)
                if type(row) is not dict:
                    continue
                remaining = row.get("remaining_seconds")
                born = _utc(row.get("born_utc"))
                if (
                    not _matches(row.get("job_id"), r"[a-zA-Z0-9_-]{1,64}")
                    or not _matches(row.get("sha256"), r"[a-f0-9]{64}")
                    or row.get("source") not in ("synthetic_fixture", "render_buffer")
                    or not _matches(row.get("recipient_principal"), r"[A-Za-z0-9_:.+-]{1,128}")
                    or born is None
                    or not isinstance(remaining, (float, int))
                    or isinstance(remaining, bool)
                    or not math.isfinite(remaining)
                    or not 0 < remaining <= 300
                    or row.get("delivery_state") not in ("pending", "approved", "rejected")
                    or type(row.get("policy_allowed")) is not bool
                ):
                    continue
                # Project only the existing image-summary schema. Never include
                # pixels, a delivery revision, or unexpected extension fields.
                images[row["job_id"]] = {
                    key: row[key]
                    for key in (
                        "job_id",
                        "sha256",
                        "source",
                        "recipient_principal",
                        "born_utc",
                        "remaining_seconds",
                        "delivery_state",
                        "policy_allowed",
                    )
                }
        except Exception:
            # A disappearing image or unavailable policy cannot suppress the
            # independently readable durable job history.
            return {}
        return images

    def _pending(self) -> dict[str, Any] | None:
        try:
            pending = self.plans.list_pending()
            if (
                pending.get("broker_epoch") != self.broker.epoch
                or type(pending.get("plans")) is not list
                or len(pending["plans"]) > 64
                or type(pending.get("has_more")) is not bool
                or any(
                    type(row) is not dict or not _matches(row.get("plan_hash"), r"[a-f0-9]{64}")
                    for row in pending["plans"]
                )
            ):
                return None
            return pending
        except Exception:
            return None

    def _job_rows(self, where: str, args: tuple[Any, ...] = ()) -> list[Any]:
        # Bound scalar fields before transfer, and never select diagnostic/raw
        # payload columns. A corrupted historical row is not trusted UI text.
        return (
            self.broker._store()
            .db.execute(
                f"""SELECT j.id,
                CASE WHEN length(j.plan_hash)=64 THEN j.plan_hash END AS plan_hash,
                CASE WHEN length(j.state)<=32 THEN j.state END AS state,
                CASE WHEN length(j.stage)<=128 THEN j.stage END AS stage,
                CASE WHEN length(j.result)<=128 THEN j.result END AS result,
                CASE WHEN length(j.created)<=40 THEN j.created END AS created,
                CASE WHEN length(j.finished)<=40 THEN j.finished END AS finished,
                CASE WHEN typeof(j.reviewed)='integer' AND j.reviewed=1
                    THEN 1 ELSE 0 END AS reviewed
                FROM jobs j WHERE length(j.id) BETWEEN 1 AND 64 AND ({where})
                ORDER BY j.rowid DESC LIMIT ?""",
                (*args, MAX_CARDS + 1),
            )
            .fetchall()
        )

    def _plan_rows(self, where: str, args: tuple[Any, ...] = ()) -> list[Any]:
        return (
            self.broker._store()
            .db.execute(
                f"""SELECT p.hash, EXISTS(SELECT 1 FROM approvals a WHERE a.hash=p.hash) approved
                FROM plans p WHERE length(p.hash)=64 AND {_UNEXECUTED} AND ({where})
                ORDER BY p.rowid DESC LIMIT ?""",
                (_MAX_PLAN_BYTES, *args, MAX_CARDS + 1),
            )
            .fetchall()
        )

    @staticmethod
    def _card(job_id: str, plan_hash: str | None, plan: dict[str, Any] | None) -> dict[str, Any]:
        return {
            "job_id": job_id,
            "plan_hash": plan_hash,
            "test": plan["test"] if plan else None,
            "backend": plan["backend"] if plan else None,
            "state": "unknown",
            "stage": None,
            "result": None,
            "created_utc": None,
            "finished_utc": None,
            "reviewed": False,
            "review": None,
            "image": None,
        }

    def snapshot(self) -> dict[str, Any]:
        try:
            return self._snapshot()
        except Exception:
            # Even SQLite exceptions can contain local paths or stored data.
            raise BrokerError("operator_timeline_unavailable") from None

    def _snapshot(self) -> dict[str, Any]:
        broker = self.broker
        store = broker._store()
        broker.maintenance_tick()
        if not _matches(broker.epoch, r"[a-f0-9]{32}"):
            raise ValueError
        epoch = broker.epoch
        images = self._images()
        pending = self._pending()
        pending_hashes = {row["plan_hash"] for row in pending["plans"]} if pending else set()
        lease = broker.session
        lease_id = lease.id if lease is not None else ""
        current = """CASE WHEN json_valid(p.payload) THEN
            json_extract(p.payload,'$.broker_epoch')=?
            AND json_extract(p.payload,'$.session_id')=? ELSE 0 END"""
        groups: list[tuple[str, list[Any]]] = [("job", self._job_rows(_ACTIVE))]
        if pending_hashes:
            marks = ",".join("?" for _ in pending_hashes)
            groups.append(("plan", self._plan_rows(f"p.hash IN ({marks})", tuple(pending_hashes))))
        if images:
            marks = ",".join("?" for _ in images)
            groups.append(("job", self._job_rows(f"j.id IN ({marks})", tuple(images))))
        groups.extend(
            [
                ("plan", self._plan_rows(current, (epoch, lease_id))),
                ("job", self._job_rows(f"NOT {_ACTIVE}")),
                ("plan", self._plan_rows(f"NOT ({current})", (epoch, lease_id))),
            ]
        )
        cards: dict[str, dict[str, Any]] = {}
        has_more = bool(pending and pending["has_more"])
        for kind, rows in groups:
            has_more = has_more or len(rows) > MAX_CARDS
            for row in rows:
                if kind == "job":
                    if not _matches(row["id"], r"[a-zA-Z0-9_-]{1,64}"):
                        continue
                    plan_hash = row["plan_hash"]
                    plan = self._plan(plan_hash)
                    if plan is not None and plan["job_id"] != row["id"]:
                        plan = None
                    card = self._card(
                        row["id"], plan_hash if _matches(plan_hash, r"[a-f0-9]{64}") else None, plan
                    )
                    card.update(
                        state=_enum(row["state"], _JOB_STATES) or "unknown",
                        stage=_enum(row["stage"], STAGES),
                        result=_enum(row["result"], RESULTS),
                        created_utc=_utc(row["created"]),
                        finished_utc=_utc(row["finished"]),
                        reviewed=row["reviewed"] == 1,
                    )
                    if card["state"] == "succeeded":
                        card["image"] = images.get(row["id"])
                else:
                    plan = self._plan(row["hash"])
                    if plan is None:
                        continue
                    card = self._card(plan["job_id"], row["hash"], plan)
                    live = (
                        lease is not None
                        and broker.session is lease
                        and math.isfinite(lease.deadline)
                        and broker.clock() < lease.deadline
                        and plan["broker_epoch"] == epoch
                        and plan["session_id"] == lease.id
                    )
                    card["state"] = "prepared" if live else "expired"
                    if live and lease is not None and pending is not None:
                        try:
                            self.plans._validate(row["hash"], lease)
                        except Exception:
                            pass
                        else:
                            if row["approved"]:
                                card["state"] = "approved"
                            elif row["hash"] in pending_hashes:
                                card["state"] = "approval_required"
                            elif (
                                broker.backend.name == "mock"
                                and broker.settings.allow_mock_execution
                            ) or (plan["test"] == "capture_home" and not pending["has_more"]):
                                card["state"] = "ready"
                cards.setdefault(card["job_id"], card)
        has_more = has_more or len(cards) > MAX_CARDS
        selected = list(cards.values())[:MAX_CARDS]
        available = pending is not None
        revision = store.db.total_changes
        try:
            for card in selected:
                if card["state"] not in ("approval_required", "awaiting_approval"):
                    continue
                if card["plan_hash"] not in pending_hashes:
                    continue
                detail = self.plans.detail(card["plan_hash"])
                fields = {
                    "plan_hash",
                    "plan",
                    "review_revision",
                    "lease_remaining_seconds",
                    "attestation",
                }
                optional = {
                    "execution_submitted",
                    "intent_remaining_seconds",
                    "attestation_text",
                    "image_delivery_recipient",
                }
                submitted = detail.get("execution_submitted", False)
                intent_remaining: Any = detail.get("intent_remaining_seconds")
                if (
                    not fields <= set(detail) <= fields | optional
                    or detail["plan_hash"] != card["plan_hash"]
                    or detail["plan"] != self._plan(card["plan_hash"])
                    or not _matches(detail["review_revision"], r"[a-f0-9]{64}")
                    or type(detail["lease_remaining_seconds"]) not in (float, int)
                    or not math.isfinite(detail["lease_remaining_seconds"])
                    or not 0 < detail["lease_remaining_seconds"] <= 300
                    or detail["attestation"]
                    not in (
                        "none",
                        "device_free_firmware",
                        "device_home_firmware",
                        "flash_device_read_verified",
                        "flash_device_backup_verified",
                        "firmware_recovery_verified",
                    )
                    or type(submitted) is not bool
                    or submitted != (card["state"] == "awaiting_approval")
                    or (
                        submitted
                        and (
                            type(intent_remaining) not in (float, int)
                            or not math.isfinite(intent_remaining)
                            or not 0 < intent_remaining <= detail["lease_remaining_seconds"]
                        )
                    )
                    or (not submitted and intent_remaining is not None)
                    or (
                        "attestation_text" in detail
                        and (
                            type(detail["attestation_text"]) is not str
                            or detail["attestation_text"] != _ATTESTATIONS[detail["attestation"]]
                        )
                    )
                    or (
                        "image_delivery_recipient" in detail
                        and (
                            type(detail["image_delivery_recipient"]) not in (str, type(None))
                            or detail["image_delivery_recipient"]
                            != (
                                lease.principal_id
                                if lease is not None and detail["plan"]["test"] == "capture_home"
                                else None
                            )
                        )
                    )
                ):
                    raise ValueError
                card["review"] = {key: detail[key] for key in fields | optional if key in detail}
            if available and pending is not None:
                # Callbacks used by policy/principal validation can expire the
                # lease or change policy while rendering. Recheck the authority
                # last, then reject the entire review set if context moved.
                final = self._pending()
                if final is None or revision != store.db.total_changes:
                    raise ValueError
                final_hashes = {row["plan_hash"] for row in final["plans"]}
                if final_hashes != pending_hashes or final["has_more"] != pending["has_more"]:
                    raise ValueError
                for card in selected:
                    if card["review"] is not None:
                        if lease is None:
                            raise ValueError
                        plan = self.plans._validate(card["plan_hash"], lease)
                        if card["review"]["review_revision"] != self.plans._revision(plan, lease):
                            raise ValueError
        except Exception:
            available = False
        broker._prune_captures()
        for card in selected:
            image = card["image"]
            if image is None:
                continue
            retained = broker.captures.get(card["job_id"])
            if (
                retained is None
                or retained.image.sha256 != image["sha256"]
                or retained.principal_id != image["recipient_principal"]
                or retained.born_utc != image["born_utc"]
                or retained.delivery_state != image["delivery_state"]
            ):
                card["image"] = None
        context_expired = (
            broker.epoch != epoch
            or broker.session is not lease
            or (
                lease is not None
                and (not math.isfinite(lease.deadline) or broker.clock() >= lease.deadline)
            )
        )
        if context_expired:
            available = False
        if not available:
            for card in selected:
                card["review"] = None
                if card["state"] in ("prepared", "approval_required", "approved", "ready"):
                    card["state"] = "expired" if context_expired else "prepared"
            # A validation callback can expire/cancel an intent while rendering.
            # Retain the resulting durable terminal state on the same job card.
            waiting = {
                card["job_id"]: card for card in selected if card["state"] == "awaiting_approval"
            }
            if waiting:
                marks = ",".join("?" for _ in waiting)
                for row in self._job_rows(f"j.id IN ({marks})", tuple(waiting)):
                    current_card = waiting[row["id"]]
                    current_card.update(
                        state=_enum(row["state"], _JOB_STATES) or "unknown",
                        stage=_enum(row["stage"], STAGES),
                        result=_enum(row["result"], RESULTS),
                        finished_utc=_utc(row["finished"]),
                    )
        for card in selected:
            if (card["backend"], card["state"]) == ("serial", "succeeded") and (
                card["test"],
                card["result"],
            ) in (
                ("firmware_observe", "firmware_observation_completed_no_commands"),
                ("restart_and_observe", "restart_observation_completed_unverified"),
            ):
                try:
                    observation = observation_summary(
                        broker.operator_device_observation_metadata(card["job_id"]), card["job_id"]
                    )
                    plan = self._plan(card["plan_hash"])
                    if (
                        plan is not None
                        and plan["log_delivery"]["principal_id"]
                        == observation["recipient_principal"]
                    ):
                        card["device_observation"] = observation
                except Exception:
                    # Cache expiry or invalid metadata cannot hide job history.
                    pass
            if card["plan_hash"] is not None:
                home_line = broker.operator_home_line_metadata(card["job_id"], card["plan_hash"])
                if home_line is not None and home_line["broker_epoch"] == epoch:
                    card["local_home_line"] = home_line
        return {
            "broker_epoch": epoch,
            "cards": selected,
            "has_more": has_more,
            "plans_available": available,
        }
