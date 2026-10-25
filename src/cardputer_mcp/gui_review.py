"""Explicit operator acknowledgement and resource re-check for the GUI; no Tk.

Each function sends exactly one request and turns the reply into a fixed message.
Nothing is retried. Acknowledgement sets only the reviewed flag; the re-check
clears only latches whose broker-side check passed.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from .gui import QueueSnapshot
    from .gui_timeline import TimelineCard

ACK_DONE = "Review acknowledged. State and result are unchanged; nothing was retried."
ACK_ALREADY = "Already acknowledged. State and result are unchanged."
ACK_UNCERTAIN = "Acknowledgement outcome unknown. Refresh status; nothing was retried."
ACK_REFUSED = {
    "review_not_required": "This row no longer needs review. Refresh status.",
    "review_target_not_found": "This row was not found. Refresh status.",
    "review_target_in_use": "A HOME recovery for this job is in progress. Acknowledge it later.",
    "stale_operator_snapshot": "The broker restarted. Review the current rows first.",
    "broker_closing": "The broker is shutting down.",
}
RECHECK_UNCERTAIN = "Re-check outcome unknown. Refresh status; nothing was retried."
RECHECK_NOTHING = "Re-check: no uncertainty flag was set."
REVIEW_CONFIRM = (
    "Acknowledge this needs-review row?\n\n"
    "This only marks it reviewed. State and result stay as recorded, nothing is "
    "retried and the device is not touched. Acknowledging a capture failure also "
    "gives up its HOME recovery."
)
RECHECK_CONFIRM = (
    "Re-check owned resources?\n\n"
    "The broker checks each uncertainty flag (serial handle closed, owned processes "
    "exited, job state consistent) and clears only the flags whose check passes. "
    "A port is never opened and no job is retried."
)

FLAG_LABELS = {
    "serial_ownership": "Serial port close",
    "flash_observation_serial": "Flash boot-observation port close",
    "build_process": "Build process exit",
    "flash_process": "Flash tool process exit",
    "flash_shutdown": "Flash shutdown",
    "build_output": "Build output release",
    "source_cleanup": "Source cleanup",
    "profile_state": "Registered-project job state",
    "profile_worker": "Registered-project worker state",
    "execution": "Execution state",
}
_CODE = re.compile(r"[a-z][a-z0-9_]{1,80}")


class ReviewAPI(Protocol):
    def review_job(self, job_id: str, plan_hash: str, broker_epoch: str) -> dict[str, Any]: ...
    def review_request(self, request_id: str, broker_epoch: str) -> dict[str, Any]: ...
    def recheck(self, broker_epoch: str) -> dict[str, Any]: ...


def review_available(card: TimelineCard | None) -> bool:
    return bool(card is not None and card.needs_device_review and card.plan_hash)


def recheck_available(snapshot: QueueSnapshot | None) -> bool:
    return bool(
        snapshot is not None
        and (snapshot.uncertainty or snapshot.ownership_uncertain or snapshot.execution_uncertain)
    )


def flag_label(flag: str) -> str:
    return FLAG_LABELS.get(flag, flag)


def uncertainty_summary(snapshot: QueueSnapshot | None) -> str:
    if snapshot is None or not snapshot.uncertainty:
        return "Uncertainty flags: none reported"
    return "Uncertainty flags: " + "; ".join(
        f"{flag_label(row.flag)} ({row.reason})" for row in snapshot.uncertainty
    )


def _acknowledged(result: Any, key: str, value: str, epoch: str) -> str:
    if type(result) is not dict:
        return ACK_UNCERTAIN
    if result.get("ok") is False:
        return ACK_REFUSED.get(result.get("error", ""), ACK_UNCERTAIN)
    if (
        result.get("ok") is not True
        or result.get(key) != value
        or result.get("broker_epoch") != epoch
        or result.get("state") != "needs_review"
        or result.get("reviewed") is not True
        or result.get("replayed") is not False
        or type(result.get("already_reviewed")) is not bool
    ):
        return ACK_UNCERTAIN
    return ACK_ALREADY if result["already_reviewed"] else ACK_DONE


def perform_review_job(api: ReviewAPI, job_id: str, plan_hash: str, epoch: str) -> str:
    try:
        return _acknowledged(api.review_job(job_id, plan_hash, epoch), "job_id", job_id, epoch)
    except Exception:
        return ACK_UNCERTAIN


def perform_review_request(api: ReviewAPI, request_id: str, epoch: str) -> str:
    try:
        result = api.review_request(request_id, epoch)
        return _acknowledged(result, "request_id", request_id, epoch)
    except Exception:
        return ACK_UNCERTAIN


def perform_recheck(api: ReviewAPI, epoch: str) -> str:
    try:
        result = api.recheck(epoch)
    except Exception:
        return RECHECK_UNCERTAIN
    if type(result) is not dict:
        return RECHECK_UNCERTAIN
    if result.get("ok") is False:
        return ACK_REFUSED.get(result.get("error", ""), RECHECK_UNCERTAIN)
    checked = result.get("checked")
    if (
        result.get("ok") is not True
        or result.get("broker_epoch") != epoch
        or type(checked) is not list
        or len(checked) > 32
    ):
        return RECHECK_UNCERTAIN
    if not checked:
        return RECHECK_NOTHING
    cleared: list[str] = []
    kept: list[str] = []
    for row in checked:
        if type(row) is not dict:
            return RECHECK_UNCERTAIN
        flag, outcome, reason = row.get("flag"), row.get("outcome"), row.get("reason")
        if (
            type(flag) is not str
            or type(reason) is not str
            or _CODE.fullmatch(flag) is None
            or _CODE.fullmatch(reason) is None
            or outcome not in ("cleared", "kept")
        ):
            return RECHECK_UNCERTAIN
        (cleared if outcome == "cleared" else kept).append(f"{flag_label(flag)} ({reason})")
    parts = ["Re-check:"]
    parts.append("cleared " + ", ".join(cleared) + "." if cleared else "nothing cleared.")
    if kept:
        parts.append("Kept " + ", ".join(kept) + ".")
    return " ".join(parts)
