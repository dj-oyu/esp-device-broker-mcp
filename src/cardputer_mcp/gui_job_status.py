"""Quiet job badges; visual emphasis never changes history or authority."""

import os
from dataclasses import dataclass
from typing import Any, Literal

LABELS = {
    "prepared": "[ ] 準備済・未実行",
    "queued": "[..] 実行待ち",
    "running": "[>] 実行中",
    "succeeded": "[OK] 成功",
    "failed": "[X] 失敗",
    "cancelled": "[-] 取消済",
    "expired": "[-] 準備期限切れ",
    "unknown": "[?] 結果不明",
    "approval_required": "[..] 旧承認待ち",
    "awaiting_approval": "[..] 旧承認待ち",
    "approved": "[ ] 旧承認済",
    "ready": "[ ] 旧実行準備済",
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
        labels = {"pending": "[未確認]", "recorded": "[確認済み]", "not_applicable": "-"}
        return labels[self.acknowledgement]

    @property
    def lifecycle_label(self) -> str:
        return {
            "prepared": "準備済・未実行",
            "queued": "実行待ち",
            "running": "実行中",
            "expired": "期限切れ・未実行",
            "legacy_pending": "旧形式・未実行",
            "terminal_record": "終了記録",
            "unknown": "記録不明",
        }[self.lifecycle]

    @property
    def outcome_label(self) -> str:
        return {
            "not_started": "未実行",
            "in_progress": "未確定",
            "succeeded": "成功",
            "failed": "失敗",
            "cancelled": "取消済",
            "unknown": "結果不明",
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
    # Leave native selected/focus colors intact. ASCII shape badges and Japanese
    # text stay visible when the selection theme overrides the row background.
