"""The shared (any read-scoped principal) view of one persisted job."""

import copy
from typing import Any

# Rows written by the removed DERBY diagnostics may still carry these private
# extras. They are read-only history and are never exposed through the shared view.
_LEGACY_PROVENANCE_KEY = "derby_diagnostics_provenance"


def shared_job(job: dict[str, Any]) -> dict[str, Any]:
    """Copy a job row for sharing, dropping private details left by legacy rows."""
    result = copy.deepcopy(job)
    diagnostic = result.get("diagnostic")
    if isinstance(diagnostic, dict):
        diagnostic.pop(_LEGACY_PROVENANCE_KEY, None)
        observation = diagnostic.get("derby_observation")
        if isinstance(observation, dict):
            observation.pop("diagnostics", None)
    result.pop("derby_diagnostics", None)
    result.pop("fixed_trial", None)
    return result
