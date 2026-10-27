"""Fixed vocabulary of ValueError reasons that tool responses may return.

Only exact, reviewed code strings are returned. Any other ValueError (a pydantic
validation message, an f-string, a path or a native exception text) stays
``invalid_request`` so no value, path or exception text reaches a client.
"""

from __future__ import annotations

PUBLIC_REASON_CODES: frozenset[str] = frozenset(
    {
        # Request shape (models.py).
        "app_options_required_only_for_app_session",
        "backup_has_no_build_or_backup_input",
        "choose_one_recovery_source",
        "device_session_required",
        "diagnostic_dma_requires_explicit_source_axes",
        "diagnostic_mode_requires_source_build",
        "flash_job_id_only_for_fixed_collector",
        "flash_references_only_for_flash_operations",
        "project_id_not_allowed_for_flash_operations",
        "registered_flash_device_required",
        "restore_uses_backup_only",
        "source_commit_only_for_fetch",
        "source_fetch_requires_registered_project_and_full_commit",
        "source_receipt_only_for_registered_build",
        "after_only_for_registered_build_without_receipt",
        "session_not_allowed_with_after",
        "lease_seconds_only_with_after",
        "successful_build_job_required",
        "project_only_for_build",
        "test_not_available_for_backend",
        "explicit_request_invalid",
        "approval_interface_retired",
        "request_does_not_need_review",
        # Cursors and log reads.
        "cursor_ahead",
        "build_log_not_available",
        # Source cache and shared resources.
        "source_fetch_busy",
        "source_cache_owner_conflict",
        "source_linked_path_rejected",
        "build_process_busy",
        # Registered build and flash prerequisites.
        "artifact_count_limit",
        "build_artifacts_missing",
        "ordinary_dma_output_invalid",
        "build_inputs_changed",
        "build_authority_check_timeout",
        "build_target_mismatch",
        "idf_project_required",
        "linked_file_not_supported",
        "linked_project_not_supported",
        "profile_missing_environment",
        "profile_output_limit",
        "project_must_not_be_inside_broker_state",
        "project_snapshot_limit",
        "registered_build_project_required",
        "registered_git_version_required",
        "registered_node_16_required_for_source_project",
        "registered_node_version_required",
        "registered_tool_not_verified",
        "external_windows_idf_required",
        "absolute_registered_directory_required",
        "registered_external_python_required",
        "registered_github_repository_required",
        "invalid_flash_plan",
        "owned_build_artifact_not_available",
        # Outcomes that need offline review or a changed plan.
        "build_interrupted_needs_review",
        "build_output_release_unconfirmed",
        "idf_build_failed_needs_review",
        "build_tree_stop_unconfirmed",
        "collector_binding_changed",
        "fixed_trial_result_invalid",
        "app_plan_invalid",
        "profile_job_stopped",
        # Final-commit authority and recovery history.
        "build_final_commit_not_authorized",
        "build_security_configuration_not_authorized",
        "firmware_final_commit_not_authorized",
        "home_recovery_final_commit_not_authorized",
        "home_recovery_history_full",
        "home_recovery_source_changed",
        "invalid_fixed_trial_commit",
        "fixed_trial_job_binding_changed",
    }
)

# Fixed codes deliberately kept as invalid_request: they describe internal storage,
# environment or receipt structure that a client cannot act on.
SUPPRESSED_REASON_CODES: frozenset[str] = frozenset(
    {
        "invalid_table",
        "invalid_session_limit",
        "invalid_recovery_limit",
        "invalid_stdout_line_limit",
        "invalid_internal_artifact_receipt",
        "invalid_build_diagnostic_code",
        "invalid_profile_environment",
        "external_idf_environment_invalid",
        "trusted_powershell_missing",
        "profile_or_principal_no_longer_valid",
        "firmware_summary_invalid",
        "source_publication_conflict",
        "source_publication_invalid",
        "source_publication_not_authorized",
        # Offline `cardputer archive` only (Store.archive_rows); never a request path.
        "history_archive_unavailable",
        "history_archive_verification_failed",
    }
)


def public_reason(exc: ValueError) -> str:
    """Return the fixed reason carried by ``exc``, else ``invalid_request``."""
    reason = exc.args[0] if len(exc.args) == 1 else None
    if isinstance(reason, str) and reason in PUBLIC_REASON_CODES:
        return reason
    return "invalid_request"


# Fixed codes that reach clients as BrokerError text, either raised directly or
# re-wrapped from TicketError, app session/wire/event errors, build log reads,
# build locations and flash admission. Any other BrokerError text (for example
# a re-wrapped free-form ValueError) is returned as ``invalid_request``.
# tests/test_public_reasons.py regenerates this set from the source.
BROKER_REASON_CODES: frozenset[str] = frozenset(
    {
        "accepted",
        "action_not_in_plan",
        "action_only_for_tap",
        "active_job_cannot_release",
        "app_authority_lost",
        "app_events_not_available",
        "app_input_stopped",
        "app_session_not_available",
        "app_teardown_unconfirmed",
        "app_transport_busy",
        "app_wire_invalid",
        "authenticated_owner_mismatch",
        "authentication_required",
        "backend_configuration_mismatch",
        "backend_not_available",
        "backend_preflight_failed",
        "backup_registered_device_mismatch",
        "blocked",
        "broker_already_running",
        "broker_closing",
        "broker_not_running",
        "build_manifest_resolver_unavailable",
        "build_output_identity_invalid",
        "build_output_identity_mismatch",
        "build_output_in_use",
        "build_output_insufficient_space",
        "build_output_linked",
        "build_output_path_too_long",
        "build_output_root_invalid",
        "build_output_storage_unavailable",
        "build_principal_mismatch",
        "busy",
        "cannot_resolve_os_user",
        "capture_not_available",
        "capture_result_invalid",
        "catalog_changed",
        "chain_broken",
        "command_identity_required",
        "conflict",
        "consumed",
        "continuation_not_supported",
        "device_busy",
        "device_close_unconfirmed",
        "device_log_result_invalid",
        "device_logs_not_available",
        "device_security_or_identity_not_verified",
        "duplicate_action",
        "execution_intent_not_available",
        "expired",
        "external_idf_environment_invalid",
        "external_python_diagnostic_failed",
        "external_toolchain_missing",
        "external_toolchain_resolution_changed",
        "flash_app_elf_receipt_required",
        "flash_app_receipt_required",
        "flash_authenticated_principal_required",
        "flash_authority_changed_needs_review",
        "flash_backup_incomplete_needs_review",
        "flash_backup_stale_needs_review",
        "flash_binding_invalid",
        "flash_boot_unverified_needs_review",
        "flash_build_project_mismatch",
        "flash_cancelled_needs_review",
        "flash_cancelled_or_authority_changed_needs_review",
        "flash_device_busy_or_uncertain",
        "flash_expected_elf_receipt_required",
        "flash_failed_needs_review",
        "flash_inputs_changed_needs_review",
        "flash_log_not_available",
        "flash_manifest_changed_needs_review",
        "flash_plan_or_handoff_required",
        "flash_principal_mismatch",
        "flash_readback_mismatch_needs_review",
        "flash_registration_selector_mismatch",
        "flash_serial_opt_in_required",
        "flash_shutdown_uncertain_needs_review",
        "flash_timeout_needs_review",
        "fresh_requires_run_zero",
        "history_full_archive_offline",
        "hold_expired",
        "home_recovery_already_requested",
        "home_recovery_authority_changed",
        "home_recovery_close_unconfirmed",
        "home_recovery_not_available",
        "home_recovery_other_review_or_work",
        "home_recovery_reentrant_check",
        "home_recovery_scope_mismatch",
        "home_recovery_source_not_eligible",
        "home_recovery_target_changed",
        "home_recovery_unavailable",
        "idf_version_diagnostic_failed",
        "idf_version_mismatch_no_fallback",
        "insufficient_scope",
        "invalid",
        "invalid_build_diagnostic_code",
        "invalid_internal_build_manifest_context",
        "invalid_principal",
        "invalid_profile_environment",
        "invalid_session",
        "invalid_session_scopes",
        "invalid_wait_position",
        "invalid_work_session",
        "job_id_conflict",
        "job_not_active",
        "job_not_found",
        "job_session_mismatch",
        "lease_expired_active_job_retains_ownership",
        "lease_not_found",
        "limit",
        "lkg_bytes_changed",
        "lkg_current_firmware_changed_needs_review",
        "lkg_device_binding_changed",
        "lkg_promotion_cancelled_needs_review",
        "lkg_receipt_kind_changed",
        "lkg_restore_layout_evidence_required",
        "lkg_restore_partition_changed_needs_review",
        "local_operator_required",
        "node_requires_explicit_local_registration",
        "not_authorized",
        "ok",
        "only_queued_jobs_can_be_cancelled",
        "only_waiting_requests_can_be_cancelled",
        "only_waiting_requests_can_be_reordered",
        "operation_interrupted_review_required",
        "operator_stop_not_available",
        "operator_stop_not_supported",
        "operator_stop_refused_device_write",
        "operator_timeline_unavailable",
        "out_of_order",
        "outcome_unknown",
        "owned_home_recovery_required",
        "owned_registered_project_job_required",
        "owned_registered_project_plan_required",
        "plan_backend_mismatch",
        "plan_job_already_exists",
        "plan_not_found_or_changed",
        "plan_session_mismatch",
        "plan_validation_unavailable",
        "principal_not_authorized",
        "profile_or_principal_no_longer_valid",
        "queue_full",
        "rate_limit",
        "recovery_review_required",
        "registered_flash_operation_not_enabled",
        "registered_flash_tools_changed",
        "registered_project_cancel_required",
        "registered_tool_diagnostic_failed",
        "registered_tool_resolution_ambiguous",
        "registered_tool_version_mismatch",
        "request_not_found",
        "request_session_mismatch",
        "reserved_cleanup_command",
        "restore_original_firmware_receipt_required",
        "resume_requires_boot_and_run",
        "review_not_required",
        "review_target_in_use",
        "review_target_not_found",
        "serial_close_unconfirmed",
        "serial_runtime_opt_in_required",
        "source_log_not_available",
        "stale_boot",
        "stale_image_preview",
        "stale_lease",
        "stale_operator_snapshot",
        "stale_run",
        "stale_session",
        "stop_target_not_found",
        "successful_owned_backup_required",
        "successful_owned_build_required",
        "teardown_failed",
        "ticket_consumed",
        "ticket_invalid",
        "ticket_limit",
        "ticket_void",
        "trusted_powershell_missing",
        "unavailable",
        "unknown_app",
        "unsafe_lock_directory",
        "unsupported",
        "unsupported_auth_source",
        "verified_device_backup_required",
        "verified_device_recovery_required",
        "verified_firmware_recovery_required",
        "verified_lkg_or_initial_backup_required",
        "wait_queue_full",
        "work_session_limit",
        "work_session_not_open",
    }
)


def public_broker_reason(exc: BaseException) -> str:
    """Return BrokerError text only when it is a reviewed fixed code."""
    text = str(exc)
    if text in BROKER_REASON_CODES or text in PUBLIC_REASON_CODES:
        return text
    return "invalid_request"


# A flash/restore refused for its recovery point carries the device's
# `flash_baseline` and `lkg_recorded` (the values status(view=targets) reports)
# and one fixed next step. Nothing else: no job ids, principals or paths.
RECOVERY_POINT_REFUSAL_CODES: frozenset[str] = frozenset(
    {
        "backup_registered_device_mismatch",
        "successful_owned_backup_required",
        "verified_lkg_or_initial_backup_required",
    }
)
FLASH_BASELINES: frozenset[str] = frozenset({"not_required", "flash_not_enabled"})
RECOVERY_POINT_NEXT_STEPS: dict[str, str] = {
    "choose_lkg_receipt_from_status_recovery": (
        "lkg_receipt is not a verified LKG recorded for this device; read "
        "status(view=recovery) for this device's receipts. A flash may instead omit "
        "both lkg_receipt and backup_job_id to flash without a recovery point"
    ),
    "run_firmware_backup_then_pass_backup_job_id": (
        "No verified LKG is recorded for this device; run firmware_backup on it, "
        "then pass that job's id as backup_job_id"
    ),
    "name_own_succeeded_backup_of_this_device": (
        "backup_job_id must name a succeeded firmware_backup job that the same "
        "principal ran on this device; status(view=recovery) lists backups. Or run "
        "firmware_backup on this device and pass that job's id"
    ),
    "backup_not_enabled_in_local_registration": (
        "Backup is not enabled for this device in the local registration; an "
        "operator must set allow_backup before a backup of it can be taken. A flash "
        "may omit both lkg_receipt and backup_job_id to flash without a recovery point"
    ),
}


def recovery_point_details(code: str, details: object) -> dict[str, object]:
    """The reviewed public details of a recovery-point refusal, else nothing."""
    if (
        code not in RECOVERY_POINT_REFUSAL_CODES
        or not isinstance(details, dict)
        or set(details) != {"flash_baseline", "lkg_recorded", "next_step"}
        or details["flash_baseline"] not in FLASH_BASELINES
        or type(details["lkg_recorded"]) is not bool
        or details["next_step"] not in RECOVERY_POINT_NEXT_STEPS
    ):
        return {}
    return {
        "flash_baseline": details["flash_baseline"],
        "lkg_recorded": details["lkg_recorded"],
        "next_step": details["next_step"],
        "explanation": RECOVERY_POINT_NEXT_STEPS[details["next_step"]],
    }
