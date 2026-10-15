"""Bounded validation diagnostics: fixed reasons and schema-owned field names only."""

from typing import Any

from pydantic import BaseModel, ValidationError

from .models import (
    AcquireRequest,
    BuildLogRequest,
    CancelDeviceRequest,
    CancelRequest,
    CloseWorkSessionRequest,
    CreateWorkSessionRequest,
    ExecuteRequest,
    JobRequest,
    LeaseRequest,
    LogRequest,
    PrepareRequest,
    RenewRequest,
    RequestDeviceRequest,
    ResumeWorkSessionRequest,
)

REQUEST_MODELS: dict[str, type[BaseModel]] = {
    "acquire_session": AcquireRequest,
    "renew_session": RenewRequest,
    "release_session": LeaseRequest,
    "prepare_test": PrepareRequest,
    "execute_test": ExecuteRequest,
    "job_status": JobRequest,
    "read_logs": LogRequest,
    "read_build_logs": BuildLogRequest,
    "read_device_logs": JobRequest,
    "read_capture": JobRequest,
    "cancel_job": CancelRequest,
    "create_work_session": CreateWorkSessionRequest,
    "resume_work_session": ResumeWorkSessionRequest,
    "request_device": RequestDeviceRequest,
    "cancel_device_request": CancelDeviceRequest,
    "close_work_session": CloseWorkSessionRequest,
}

# These are model-validator constants, not error messages derived from input.
MODEL_REASONS = {
    "device_session_required": ["session"],
    "source_fetch_requires_registered_project_and_full_commit": ["project_id", "source_commit"],
    "source_commit_only_for_fetch": ["source_commit"],
    "source_receipt_only_for_registered_build": ["source_receipt", "project_id"],
    "flash_references_only_for_flash_operations": [
        "device_id", "build_job_id", "backup_job_id", "lkg_receipt"
    ],
    "registered_flash_device_required": ["device_id"],
    "project_id_not_allowed_for_flash_operations": ["project_id"],
    "backup_has_no_build_or_backup_input": ["build_job_id", "backup_job_id", "lkg_receipt"],
    "choose_one_recovery_source": ["backup_job_id", "lkg_receipt"],
    "successful_build_job_required": ["build_job_id"],
    "restore_uses_backup_only": ["build_job_id"],
}
TYPE_REASONS = {
    "missing": "required_field",
    "extra_forbidden": "unexpected_field",
    "string_pattern_mismatch": "invalid_field_format",
    "enum": "invalid_field_value",
    "literal_error": "invalid_field_value",
    "greater_than_equal": "field_out_of_range",
    "less_than_equal": "field_out_of_range",
    "too_long": "field_limit_exceeded",
}
SAFE_FIELDS = frozenset(
    {"request", "epoch", "sequence"}
    | {field for model in REQUEST_MODELS.values() for field in model.model_fields}
)


def invalid_arguments(reason: str = "invalid_arguments", fields: list[str] | None = None) -> dict[str, Any]:
    return {
        "ok": False, "error": "invalid_tool_request",
        "reason": reason, "fields": fields or [],
    }


def validate_arguments(name: str, arguments: dict[str, Any]) -> dict[str, Any] | None:
    model = REQUEST_MODELS.get(name)
    if model is None:
        return None
    if not isinstance(arguments, dict):
        return invalid_arguments("invalid_field_type", ["request"])
    if "request" not in arguments:
        return invalid_arguments("required_field", ["request"])
    try:
        # Diagnostic prevalidation only. The SDK still performs its normal parsing
        # and the handler still runs all existing authorization/admission checks.
        model.model_validate(arguments["request"])
    except ValidationError as error:
        issue = error.errors(include_input=False, include_url=False)[0]
        context_error = issue.get("ctx", {}).get("error")
        fixed = (
            context_error.args[0]
            if type(context_error) is ValueError and len(context_error.args) == 1
            else None
        )
        if type(fixed) is str and fixed in MODEL_REASONS:
            return invalid_arguments(fixed, list(MODEL_REASONS[fixed]))
        kind = issue.get("type", "")
        reason = TYPE_REASONS.get(kind, "invalid_field")
        if kind.endswith("_type") or kind.endswith("_parsing"):
            reason = "invalid_field_type"
        # Unknown extra keys and caller-chosen dictionary names are never echoed.
        fields = [part for part in issue.get("loc", ()) if type(part) is str and part in SAFE_FIELDS]
        return invalid_arguments(reason, fields[:4] or ["request"])
    except (TypeError, ValueError):
        return invalid_arguments()
    return None
