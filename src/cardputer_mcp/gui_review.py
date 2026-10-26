"""Explicit operator acknowledgement, re-check and device-job stop for the GUI; no Tk.

Each function sends exactly one request and turns the reply into a fixed message.
Nothing is retried. Acknowledgement sets only the reviewed flag; the re-check
clears only latches whose broker-side check passed; a stop only signals the job,
which keeps the device until its own confirmed close.
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
    "A finished job still recorded as active is dropped only when no device work, "
    "registered-project job or recovery is running. "
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


STOP_CONFIRM = (
    "Stop this device job?\n\n"
    "It was started by another client. A queued job is cancelled; a running job is "
    "asked to stop and keeps the device until its port close is confirmed. Firmware "
    "flash, restore and backup are never interrupted from here. Nothing is retried."
)
STOP_QUEUED = "Queued device job cancelled by the local operator. Nothing was retried."
STOP_REQUESTED = (
    "Stop requested by the local operator; the job keeps the device until its close "
    "is confirmed. Refresh status."
)
STOP_NOT_ACCEPTED = "The job did not accept the stop request. Refresh status."
STOP_UNCERTAIN = "Stop outcome unknown. Refresh status; nothing was retried."
STOP_REFUSED = {
    "operator_stop_refused_device_write": (
        "Firmware flash, restore and backup cannot be interrupted from here."
    ),
    "operator_stop_not_supported": "This running job kind cannot be stopped.",
    "operator_stop_not_available": "This job is not a stoppable device job. Refresh status.",
    "stop_target_not_found": "This job was not found. Refresh status.",
    "job_not_active": "This job already finished. Refresh status.",
    "app_session_not_available": "The app session is no longer running. Refresh status.",
    "backend_not_available": "This job's stop is not available on this backend.",
    "lease_expired_active_job_retains_ownership": "The device lease ended. Refresh status.",
    "stale_operator_snapshot": "The broker restarted. Review the current jobs first.",
    "broker_closing": "The broker is shutting down.",
    "local_operator_required": "Local operator authority is required.",
}


class ReviewAPI(Protocol):
    def review_job(self, job_id: str, plan_hash: str, broker_epoch: str) -> dict[str, Any]: ...
    def review_request(self, request_id: str, broker_epoch: str) -> dict[str, Any]: ...
    def recheck(self, broker_epoch: str) -> dict[str, Any]: ...


class StopAPI(Protocol):
    def stop_job(self, job_id: str, plan_hash: str, broker_epoch: str) -> dict[str, Any]: ...


def review_available(card: TimelineCard | None) -> bool:
    return bool(card is not None and card.needs_device_review and card.plan_hash)


def stop_available(card: TimelineCard | None) -> bool:
    return bool(
        card is not None
        and card.stop_available
        and card.plan_hash
        and card.state in ("queued", "running")
    )


def perform_stop_job(api: StopAPI, job_id: str, plan_hash: str, epoch: str) -> str:
    try:
        result = api.stop_job(job_id, plan_hash, epoch)
    except Exception:
        return STOP_UNCERTAIN
    if type(result) is not dict:
        return STOP_UNCERTAIN
    if result.get("ok") is False:
        return STOP_REFUSED.get(result.get("error", ""), STOP_UNCERTAIN)
    if (
        result.get("ok") is not True
        or result.get("job_id") != job_id
        or result.get("plan_hash") != plan_hash
        or result.get("broker_epoch") != epoch
        or result.get("stopped_by") != "local_operator"
        or type(result.get("stop_requested")) is not bool
    ):
        return STOP_UNCERTAIN
    if result.get("state") == "cancelled":
        return STOP_QUEUED
    if result.get("state") == "running":
        return STOP_REQUESTED if result["stop_requested"] else STOP_NOT_ACCEPTED
    return STOP_UNCERTAIN


def recheck_available(snapshot: QueueSnapshot | None) -> bool:
    return bool(
        snapshot is not None
        and (
            snapshot.uncertainty
            or snapshot.ownership_uncertain
            or snapshot.execution_uncertain
            # A finished job can stay recorded as active; the broker drops it only
            # when nothing holds the lane.
            or snapshot.active_job
        )
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
