"""Quiet job badges; visual emphasis never changes history or authority."""

import os
from dataclasses import dataclass
from typing import Any, Literal

LABELS = {
    "prepared": "⏸ Prepared",
    "queued": "⌛ Queued",
    "running": "⏳ Running",
    "succeeded": "✅ Succeeded",
    "failed": "❌ Failed",
    "cancelled": "⏹ Cancelled",
    "expired": "⌛ Expired",
    "unknown": "❔ Unknown",
    "approval_required": "⏸ Legacy pending",
    "awaiting_approval": "⏸ Legacy pending",
    "approved": "☑ Legacy approved",
    "ready": "▶ Legacy ready",
}


# Conservative evidence list, not a new backend terminal-state decision.
# Missing acknowledgements, restart records and uncertain writes are not failures.
DEFINITE_FAILURE_RESULTS = frozenset(
    {
        "serial_fault_marker",
        "derby_fixed_trial_invalid",
        "serial_capture_invalid",
        "serial_capture_timeout",
        "source_cache_scan_limit",
    }
)


@dataclass(frozen=True)
class JobPresentation:
    lifecycle: str
    outcome: str
    acknowledgement: Literal["pending", "recorded", "not_applicable"]
    label: str
    tag: str

    @property
    def review_label(self) -> str:
        labels = {"pending": "Not reviewed", "recorded": "Reviewed", "not_applicable": "-"}
        return labels[self.acknowledgement]

    @property
    def lifecycle_label(self) -> str:
        return {
            "prepared": "Prepared, not run",
            "queued": "Queued",
            "running": "Running",
            "expired": "Expired, not run",
            "legacy_pending": "Legacy, not run",
            "terminal_record": "Finished record",
            "unknown": "Unknown record",
        }[self.lifecycle]

    @property
    def outcome_label(self) -> str:
        return {
            "not_started": "Not started",
            "in_progress": "Not final",
            "succeeded": "Succeeded",
            "failed": "Failed",
            "cancelled": "Cancelled",
            "unknown": "Unknown",
        }[self.outcome]


def job_presentation(
    state: str, *, result: str | None = None, reviewed: bool = False
) -> JobPresentation:
    acknowledgement: Literal["pending", "recorded", "not_applicable"] = "not_applicable"
    if state == "needs_review":
        outcome = "failed" if result in DEFINITE_FAILURE_RESULTS else "unknown"
        acknowledgement = "recorded" if reviewed else "pending"
        lifecycle = "terminal_record"
    elif state in ("succeeded", "failed", "cancelled"):
        outcome, lifecycle = state, "terminal_record"
    elif state == "running":
        outcome, lifecycle = "in_progress", "running"
    elif state in ("prepared", "queued", "expired"):
        outcome, lifecycle = "not_started", state
    elif state in ("approval_required", "awaiting_approval", "approved", "ready"):
        outcome, lifecycle = "not_started", "legacy_pending"
    else:
        outcome, lifecycle = "unknown", "unknown"
    label = LABELS.get(state, LABELS["unknown"])
    if state == "needs_review":
        label = LABELS[outcome]
    tag = "job_failed" if outcome == "failed" else "job_unknown" if outcome == "unknown" else ""
    return JobPresentation(lifecycle, outcome, acknowledgement, label, tag)


def job_status_label(state: str, reviewed: bool = False, *, result: str | None = None) -> str:
    return job_presentation(state, result=result, reviewed=reviewed).label


def job_status_tag(state: str, *, result: str | None = None) -> str:
    return job_presentation(state, result=result).tag


def high_contrast_enabled() -> bool:
    if os.name != "nt":
        return False
    import ctypes
    from ctypes import wintypes

    class HighContrast(ctypes.Structure):
        _fields_ = [("size", wintypes.UINT), ("flags", wintypes.DWORD), ("scheme", wintypes.LPWSTR)]

    try:
        query = vars(ctypes)["WinDLL"]("user32", use_last_error=True).SystemParametersInfoW
        query.argtypes = [wintypes.UINT, wintypes.UINT, ctypes.c_void_p, wintypes.UINT]
        query.restype = wintypes.BOOL
        value = HighContrast()
        value.size = ctypes.sizeof(value)
        if not query(0x0042, value.size, ctypes.byref(value), 0):
            return True  # Unknown Windows accessibility settings: keep system colors.
        return bool(value.flags & 1)
    except (AttributeError, KeyError, OSError):
        return True


def configure_job_status_tags(tree: Any) -> None:
    if high_contrast_enabled():
        failed = unknown = {"background": "SystemWindow", "foreground": "SystemWindowText"}
    else:
        failed = {"background": "#FDE8E7", "foreground": "#202020"}
        unknown = {"background": "#FFF0CC", "foreground": "#202020"}
    tree.tag_configure("job_failed", **failed)
    tree.tag_configure("job_unknown", **unknown)
    # Leave native selected/focus colors intact. Emoji and English
    # labels stay visible when the selection theme overrides the row background.
