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
        "build_inputs_changed",
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
    }
)


def public_reason(exc: ValueError) -> str:
    """Return the fixed reason carried by ``exc``, else ``invalid_request``."""
    reason = exc.args[0] if len(exc.args) == 1 else None
    if isinstance(reason, str) and reason in PUBLIC_REASON_CODES:
        return reason
    return "invalid_request"
