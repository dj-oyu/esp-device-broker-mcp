"""Quiet job badges; visual emphasis never changes history or authority."""

import os
from typing import Any

LABELS = {
    "prepared": "[ ] 準備済・未実行",
    "queued": "[..] 実行待ち",
    "running": "[>] 実行中",
    "succeeded": "[OK] 成功",
    "failed": "[X] 失敗",
    "cancelled": "[-] 取消済",
    "expired": "[-] 準備期限切れ",
    "unknown": "[?] 状態不明",
    "approval_required": "[..] 旧承認待ち",
    "awaiting_approval": "[..] 旧承認待ち",
    "approved": "[ ] 旧承認済",
    "ready": "[ ] 旧実行準備済",
}


def job_status_label(state: str, reviewed: bool = False) -> str:
    if state == "needs_review":
        return "[!] 要確認履歴・確認済み" if reviewed else "[!] 要確認・未確認"
    return LABELS.get(state, LABELS["unknown"])


def job_status_tag(state: str) -> str:
    return "job_failed" if state == "failed" else "job_review" if state == "needs_review" else ""


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
        failed = review = {"background": "SystemWindow", "foreground": "SystemWindowText"}
    else:
        failed = {"background": "#FDE8E7", "foreground": "#202020"}
        review = {"background": "#FFF0CC", "foreground": "#202020"}
    tree.tag_configure("job_failed", **failed)
    tree.tag_configure("job_review", **review)
    # Leave native selected/focus colors intact. ASCII shape badges and Japanese
    # text stay visible when the selection theme overrides the row background.
