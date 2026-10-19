"""Explain persisted outcomes without rewriting failures or authorizing work."""

from typing import Any


def review_summary(state: str, reviewed: int) -> dict[str, Any]:
    historical_failure = state == "needs_review"
    resolved = historical_failure and reviewed == 1
    return {
        "historical_failure": historical_failure,
        "uncertainty_resolved": resolved,
        "failure_result_preserved": historical_failure,
        "required_action": (
            "inspect_job_and_use_explicit_recovery_if_eligible"
            if historical_failure and not resolved
            else "consult_current_resource_status"
        ),
        "physical_device_state": "not_inferred_from_job_history",
    }
