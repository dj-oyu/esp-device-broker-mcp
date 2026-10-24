"""Read-only job timeline and retained preview; explicit cancellation is separate.

Worker reads never prepare, execute, approve or recapture. The broker retains
all ownership decisions. Closing this view never releases physical ownership.
"""

from __future__ import annotations

import math
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

from .gui_images import ImagePreview, ImageRow
from .gui_job_status import JobPresentation, job_presentation
from .job_failure_reason import REASON_LABELS
from .models import TestName
from .operator_timeline import RESULTS, STAGES, STATES, observation_summary

MAX_CARDS = 32
# The broker admits at most 10,000 durable jobs. Keep bounded identity/deadline
# tombstones for this epoch so a disappearing row cannot renew its retention.
MAX_RETENTION_IDENTITIES = 10000
INVALID_TIMELINE = "The broker returned an invalid timeline. Refresh status."
EXPIRED_IMAGE = "Retained image expired; no capture was requested."
STALE_TIMELINE = "Timeline update failed. Showing previous status; refresh before cancelling."
PREVIEW_FAILED = "Image preview unavailable; no capture or retry was requested."
CANCEL_UNCERTAIN = "Cancellation outcome unknown. Refresh status; no retry or release was sent."
CANCEL_REQUESTED = "Cancellation requested; worker cleanup and device release are not confirmed."
CancelKey = tuple[str, str, str]


class TimelineAPI(Protocol):
    def timeline(self) -> dict[str, Any]: ...
    def image(self, job_id: str) -> dict[str, Any]: ...
    def cancel_job(self, job_id: str, plan_hash: str, broker_epoch: str) -> dict[str, Any]: ...


def _hex(value: Any, length: int) -> str:
    if type(value) is not str or re.fullmatch(r"[a-f0-9]{" + str(length) + "}", value) is None:
        raise ValueError(INVALID_TIMELINE)
    return value


def _utc(value: Any) -> str | None:
    if value is None:
        return None
    if type(value) is not str or len(value) > 40:
        raise ValueError(INVALID_TIMELINE)
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() != UTC.utcoffset(None):
        raise ValueError(INVALID_TIMELINE)
    return value


@dataclass(frozen=True)
class DeviceObservationRow:
    job_id: str
    sha256: str
    recipient_principal: str
    expires_at: float
    metadata: dict[str, Any] = field(repr=False)

    @property
    def key(self) -> tuple[str, str, str]:
        return self.job_id, self.sha256, self.recipient_principal

    @classmethod
    def parse(cls, data: Any, job_id: str, started_at: float) -> DeviceObservationRow:
        summary = observation_summary(data, job_id)
        return cls(
            job_id,
            summary["sha256"],
            summary["recipient_principal"],
            started_at + summary["remaining_seconds"],
            summary["metadata"],
        )


@dataclass(frozen=True)
class TimelineCard:
    job_id: str
    plan_hash: str | None
    test: str | None
    backend: str | None
    state: str
    stage: str | None
    result: str | None
    created_utc: str | None
    finished_utc: str | None
    review: None = None
    image: ImageRow | None = None
    reviewed: bool = False
    device_observation: DeviceObservationRow | None = None
    cancel_available: bool = False
    failure_reason: str | None = None

    @property
    def presentation(self) -> JobPresentation:
        # Display only: cancellation, ownership and recovery still use broker data.
        return job_presentation(self.state, result=self.result, reviewed=self.reviewed)

    @property
    def needs_device_review(self) -> bool:
        return self.state == "needs_review" and not self.reviewed

    @classmethod
    def parse(cls, data: dict[str, Any], epoch: str, started_at: float) -> TimelineCard:
        job = data["job_id"]
        if type(job) is not str or re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", job) is None:
            raise ValueError(INVALID_TIMELINE)
        state, stage, result = data["state"], data["stage"], data["result"]
        if state not in STATES or stage is not None and stage not in STAGES:
            raise ValueError(INVALID_TIMELINE)
        if result is not None and result not in RESULTS:
            raise ValueError(INVALID_TIMELINE)
        plan = _hex(data["plan_hash"], 64) if data["plan_hash"] is not None else None
        test, backend = data["test"], data["backend"]
        if test is not None:
            test = TestName(test).value
            if plan is None or backend not in ("serial", "mock", "registered_project"):
                raise ValueError(INVALID_TIMELINE)
        elif backend is not None:
            raise ValueError(INVALID_TIMELINE)
        if data.get("review") is not None:
            raise ValueError(INVALID_TIMELINE)
        cancel = data.get("cancel_available", False)
        reviewed = data.get("reviewed", False)
        if type(cancel) is not bool or type(reviewed) is not bool:
            raise ValueError(INVALID_TIMELINE)
        if cancel and (
            state not in ("queued", "running")
            or backend != "registered_project"
            or test not in ("firmware_build", "firmware_fetch_source")
        ):
            raise ValueError(INVALID_TIMELINE)
        reason = data.get("failure_reason")
        if reason is not None and (
            type(reason) is not str
            or reason not in REASON_LABELS
            or state not in ("failed", "needs_review")
        ):
            raise ValueError(INVALID_TIMELINE)
        image = ImageRow.parse(data["image"], started_at) if data.get("image") else None
        if image and (
            image.job_id != job
            or state != "succeeded"
            or test not in ("capture_home", "derby_smoke")
            or image.source != ("synthetic_fixture" if backend == "mock" else "render_buffer")
        ):
            raise ValueError(INVALID_TIMELINE)
        observation = None
        if data.get("device_observation") is not None:
            if (backend, state) != ("serial", "succeeded") or test not in (
                "firmware_observe",
                "restart_and_observe",
            ):
                raise ValueError(INVALID_TIMELINE)
            observation = DeviceObservationRow.parse(data["device_observation"], job, started_at)
            if (
                result
                != {
                    "firmware_observe": "firmware_observation_completed_no_commands",
                    "restart_and_observe": "restart_observation_completed_unverified",
                }[test]
            ):
                raise ValueError(INVALID_TIMELINE)
            restart = test == "restart_and_observe"
            if observation.metadata["reset_sequence_issued"] is not restart or (
                not restart and observation.metadata["capture_gap"]
            ):
                raise ValueError(INVALID_TIMELINE)
        return cls(
            job,
            plan,
            test,
            backend,
            state,
            stage,
            result,
            _utc(data["created_utc"]),
            _utc(data["finished_utc"]),
            image=image,
            reviewed=reviewed,
            device_observation=observation,
            cancel_available=cancel,
            failure_reason=reason,
        )


@dataclass(frozen=True)
class TimelineSnapshot:
    broker_epoch: str
    cards: tuple[TimelineCard, ...]
    has_more: bool
    plans_available: bool = False

    @classmethod
    def parse(cls, data: dict[str, Any], started_at: float) -> TimelineSnapshot:
        try:
            if (
                data["ok"] is not True
                or type(data["cards"]) is not list
                or len(data["cards"]) > MAX_CARDS
            ):
                raise ValueError(INVALID_TIMELINE)
            if type(data["has_more"]) is not bool:
                raise ValueError(INVALID_TIMELINE)
            epoch = _hex(data["broker_epoch"], 32)
            cards = tuple(TimelineCard.parse(card, epoch, started_at) for card in data["cards"])
            if len({card.job_id for card in cards}) != len(cards):
                raise ValueError(INVALID_TIMELINE)
            return cls(epoch, cards, data["has_more"])
        except (KeyError, TypeError, ValueError, OverflowError):
            raise ValueError(INVALID_TIMELINE) from None


@dataclass(frozen=True)
class TimelineOperation:
    sequence: int
    generation: int
    action: Literal["refresh", "preview", "cancel_job"]
    started_at: float
    job_id: str = ""
    plan_hash: str = ""
    broker_epoch: str = ""
    row: ImageRow | None = None


@dataclass(frozen=True)
class TimelineOutcome:
    operation: TimelineOperation
    snapshot: TimelineSnapshot | None = None
    preview: ImagePreview | None = field(default=None, repr=False)
    message: str = ""
    error: str = ""


def perform_timeline(
    api: TimelineAPI,
    operation: TimelineOperation,
    clock: Callable[[], float] = time.monotonic,
) -> TimelineOutcome:
    try:
        if operation.action == "refresh":
            snapshot = TimelineSnapshot.parse(api.timeline(), operation.started_at)
            return TimelineOutcome(operation, snapshot=snapshot)
        if operation.action == "preview":
            if operation.row is None or clock() >= operation.row.expires_at:
                raise ValueError(EXPIRED_IMAGE)
            response = api.image(operation.job_id)
            if response.get("broker_epoch") != operation.broker_epoch:
                raise ValueError(EXPIRED_IMAGE)
            preview = ImagePreview.parse(response, operation.row, operation.started_at)
            if clock() >= preview.row.expires_at:
                raise ValueError(EXPIRED_IMAGE)
            return TimelineOutcome(operation, preview=preview)
        _hex(operation.broker_epoch, 32)
        _hex(operation.plan_hash, 64)
        now = clock()
        if (
            re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", operation.job_id) is None
            or not math.isfinite(operation.started_at)
            or not math.isfinite(now)
            or now >= operation.started_at + 10
        ):
            raise ValueError(CANCEL_UNCERTAIN)
        result = api.cancel_job(operation.job_id, operation.plan_hash, operation.broker_epoch)
        if (
            result.get("ok") is not True
            or result.get("job_id") != operation.job_id
            or result.get("plan_hash") != operation.plan_hash
            or result.get("broker_epoch") != operation.broker_epoch
            or result.get("state")
            not in ("queued", "running", "succeeded", "failed", "cancelled", "needs_review")
            or type(result.get("stop_requested")) is not bool
        ):
            raise ValueError(CANCEL_UNCERTAIN)
        return TimelineOutcome(operation, message=CANCEL_REQUESTED)
    except Exception:
        error = {
            "refresh": STALE_TIMELINE,
            "preview": PREVIEW_FAILED,
            "cancel_job": CANCEL_UNCERTAIN,
        }[operation.action]
        return TimelineOutcome(operation, error=error)
