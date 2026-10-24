"""Bounded validation diagnostics: fixed reasons and schema-owned field names only."""

from typing import Any

from pydantic import BaseModel, ValidationError

from .surface import (
    CancelToolRequest,
    ExecuteToolRequest,
    FlashToolRequest,
    SessionRequest,
    StatusRequest,
    SubmitRequest,
)

REQUEST_MODELS: dict[str, type[BaseModel]] = {
    "status": StatusRequest,
    "submit": SubmitRequest,
    "execute": ExecuteToolRequest,
    "flash": FlashToolRequest,
    "cancel": CancelToolRequest,
    "session": SessionRequest,
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


def invalid_arguments(
    reason: str = "invalid_arguments", fields: list[str] | None = None
) -> dict[str, Any]:
    return {
        "ok": False,
        "error": "invalid_tool_request",
        "reason": reason,
        "fields": fields or [],
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
        kind = issue.get("type", "")
        reason = TYPE_REASONS.get(kind, "invalid_field")
        if kind.endswith("_type") or kind.endswith("_parsing"):
            reason = "invalid_field_type"
        # Unknown extra keys and caller-chosen dictionary names are never echoed.
        fields = [
            part for part in issue.get("loc", ()) if type(part) is str and part in SAFE_FIELDS
        ]
        return invalid_arguments(reason, fields[:4] or ["request"])
    except (TypeError, ValueError):
        return invalid_arguments()
    return None
