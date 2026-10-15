"""Embedded, exact-target job review from request through retained image delivery.

Only the worker calls HTTP and validates PNG bytes. Tk owns the rendered review,
attestation and PhotoImage objects. Refreshes never submit approvals, recapture,
execute jobs, replay decisions, or write pixels to disk.
"""

from __future__ import annotations

import base64
import json
import math
import queue
import re
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal, Protocol

from .gui_approvals import (
    APPROVAL_NOTICE,
    APPROVAL_UNCERTAIN,
    ATTESTATIONS,
    EXECUTION_NOTICE,
    EXECUTION_QUEUED,
    PlanOperation,
    PlanReview,
    approval_button_text,
    approval_result,
    perform_plan,
    review_warning,
)
from .gui_home_line import (
    HOME_LINE_EXPIRED,
    HOME_LINE_FAILED,
    HOME_LINE_OPT_IN,
    HomeLineKey,
    HomeLinePreview,
    HomeLineRow,
)
from .gui_images import (
    DECISION_FAILED,
    SOURCES,
    Decision,
    ImageOperation,
    ImagePreview,
    ImageRow,
    perform_image,
)
from .models import TestName
from .operator_timeline import RESULTS, STAGES, STATES, observation_summary

if TYPE_CHECKING:
    import tkinter as tk
    from tkinter import ttk


MAX_CARDS = 32
MAX_PREVIEWS = 8
PREVIEWS_PER_CYCLE = 2
REFRESH_SECONDS = 2.0
INVALID_TIMELINE = "The broker returned an invalid timeline. Decisions are disabled."
STALE_TIMELINE = "Timeline update failed. Showing the last snapshot; decisions are disabled."
INITIAL_FAILED = "Timeline unavailable. No job or capture history has been loaded."
PENDING_CLOSE = "A decision is pending. Closing does not cancel it; wait for its result."
PREVIEW_FAILED = "Local preview unavailable. Image delivery is disabled; no capture was requested."
EXPIRED_REVIEW = "This exact plan review has expired. No approval was sent."
EXPIRED_IMAGE = "Retained image unavailable or expired. Captures are retained for 5 minutes."
PLAN_APPROVED = "Approved. " + APPROVAL_NOTICE
EXPIRED_OBSERVATION = (
    "Device log report and firmware metadata unavailable or expired. "
    "Reports are retained in RAM for at most 5 minutes."
)
REQUESTS = {
    "diagnostics": "Run diagnostics",
    "firmware_workflow": "Run firmware workflow",
    "passive_observe": "Observe device serial output",
    "firmware_observe": "Observe firmware and device logs",
    "restart_and_observe": "Restart and observe boot logs",
    "hello": "Run HELLO navigation",
    "capture_home": "Capture the HOME screen",
    "firmware_fetch_source": "Fetch registered source",
    "firmware_build": "Build registered firmware",
    "firmware_backup": "Back up device flash",
    "firmware_flash": "Flash verified firmware",
    "firmware_restore": "Restore verified recovery firmware",
}
STATE_LABELS = {
    "prepared": "Prepared",
    "approval_required": "Waiting for approval",
    "awaiting_approval": "Submitted · waiting for approval",
    "approved": "Approved · waiting for client",
    "ready": "Ready · waiting for client",
    "queued": "Queued",
    "running": "Running",
    "succeeded": "Completed",
    "failed": "Failed",
    "cancelled": "Cancelled",
    "needs_review": "Review required",
    "expired": "Expired",
    "unknown": "State unavailable",
}
Action = Literal[
    "refresh", "preview", "home_line", "approve_plan", "approve_image", "reject_image", "cancel_job"
]
CancelKey = tuple[str, str, str]
CANCEL_PENDING = "Cancellation request pending; cleanup and device release are not confirmed."
CANCEL_REQUESTED = "Cancellation requested; waiting for worker/process cleanup. Device release is not confirmed."
CANCEL_UNCERTAIN = "Cancellation outcome unknown. Refresh status; no automatic retry or device release."
CANCEL_REJECTED = "Cancellation was rejected. Existing owner authorization is required; refresh status."
CANCEL_REVIEW = "Cleanup or device state requires review. Stop completion and device release are not confirmed."
ReviewKey = tuple[str, str, str, str, str, str, str, str, bool]
ImageKey = tuple[str, str, str, str, str, str, bool]


class TimelineAPI(Protocol):
    def timeline(self) -> dict[str, Any]: ...
    def cancel_job(self, job_id: str, plan_hash: str, broker_epoch: str) -> dict[str, Any]: ...
    def approve(
        self,
        plan_hash: str,
        review_revision: str,
        attestation: str,
        retain_local_home_line: bool = False,
    ) -> dict[str, Any]: ...
    def local_home_line(
        self, job_id: str, plan_hash: str, diagnostic_id: str
    ) -> dict[str, Any]: ...
    def image(self, job_id: str) -> dict[str, Any]: ...
    def decide_image(
        self, job_id: str, sha256: str, delivery_revision: str, decision: Decision
    ) -> dict[str, Any]: ...


def _hex(value: Any, length: int) -> str:
    if not isinstance(value, str) or re.fullmatch(rf"[a-f0-9]{{{length}}}", value) is None:
        raise ValueError(INVALID_TIMELINE)
    return value


def _utc(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > 40:
        raise ValueError(INVALID_TIMELINE)
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() != UTC.utcoffset(None):
        raise ValueError(INVALID_TIMELINE)
    return parsed.astimezone(UTC).isoformat()


def review_key(review: PlanReview) -> ReviewKey:
    """Deadline changes cannot silently substitute a different visible authority."""
    return (
        review.broker_epoch,
        review.plan_hash,
        review.review_revision,
        review.job_id,
        review.test,
        review.backend,
        review.attestation,
        review.json_text,
        review.execution_submitted,
    )


def image_key(row: ImageRow) -> ImageKey:
    return (
        row.job_id,
        row.sha256,
        row.source,
        row.recipient_principal,
        row.born_utc,
        row.delivery_state,
        row.policy_allowed,
    )


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


def restart_stage_text(card: TimelineCard) -> str:
    """Control stages are not proof of a physical reset or a successful app boot."""
    if card.state in ("approved", "ready", "queued"):
        return "Reset controls and serial receive have not started. Waiting for execution."
    return {
        "restart_and_observe": "Preparing the one-shot reset · Serial receive is not active",
        "serial_restart_requested": (
            "Reset control sequence requested · Serial receive is not active · No reset retry"
        ),
        "serial_restart_complete": (
            "Reset controls completed · Serial receive is not active · Application boot unverified"
        ),
        "serial_reconnect_wait": (
            "Waiting for the same USB identity · At most one reopen within 8 seconds after reset "
            "· Serial receive is not active · Early logs may be lost"
        ),
        "serial_reopened": (
            "Same USB identity reopened once · Serial receive is not active yet "
            "· Early logs may be lost"
        ),
        "firmware_observe": (
            "Reset controls completed · Serial receive started · At most 30 seconds and 64 KiB "
            "· Application boot unverified"
        ),
    }.get(card.stage or "", "Preparing or closing serial · Receive is not active")


def firmware_observation_text(card: TimelineCard, now: float, *, fresh: bool = True) -> str:
    row = card.device_observation
    restart = card.test == "restart_and_observe"
    if not fresh:
        return "Awaiting a verified update; report availability is unconfirmed."
    if row is None or now >= row.expires_at:
        if card.state == "succeeded":
            return EXPIRED_OBSERVATION
        if restart and card.state in ("running", "approved", "ready", "queued"):
            return restart_stage_text(card)
        if card.state == "running" and card.stage == "firmware_observe":
            return "Serial receive started · At most 30 seconds · No commands sent"
        if card.state == "running":
            return "Serial receive is not active. Waiting for the current serial stage to finish."
        if card.state in ("approved", "ready", "queued"):
            return "Serial receive has not started. Waiting for the device and receive stage."
        if card.state in ("failed", "needs_review", "unknown"):
            if restart:
                return (
                    "Restart/observation did not complete; reset and device state are unknown. "
                    "Device log report and firmware metadata unavailable. No reset was retried."
                )
            return (
                "Observation did not complete; device log report and firmware metadata unavailable."
            )
        return "No device log report or firmware metadata is available."
    metadata, firmware = row.metadata, row.metadata["firmware"]
    status = {
        "observed": "observed (unverified device report)",
        "partial": "partial (incomplete device report)",
        "unavailable": "unavailable (no recognized startup block)",
        "ambiguous": "unavailable (ambiguous startup evidence)",
    }[firmware["status"]]
    if restart and firmware["status"] in ("unavailable", "ambiguous"):
        status = "unknown" + status[len("unavailable") :]
    text = (
        f"Firmware identity: {status}\n"
        f"Device logs: {metadata['bytes_received']} bytes inspected · "
        f"{metadata['retained_lines']} lines retained · {metadata['redacted_lines']} redacted · "
        f"{metadata['omitted_lines']} omitted\n"
        f"Report: {'partial' if metadata['partial'] else 'bounded observation complete'} · "
        f"Metadata available for {max(0, math.ceil(row.expires_at - now))} s\n"
        f"Delivery authorized for requesting principal: {row.recipient_principal}. "
        "Retrieval by the client is not confirmed."
    )
    fields = (
        ("project_name", "Project"),
        ("app_version", "App version"),
        ("idf_version", "ESP-IDF"),
        ("compile_time", "Compile time"),
        ("secure_version", "Secure version"),
        ("elf_sha256_prefix", "ELF SHA-256 prefix (not a .bin hash)"),
    )
    for key, label in fields:
        if firmware[key] is not None:
            text += f"\n{label}: {firmware[key]}"
    if restart:
        text = (
            "Reset control sequence issued: "
            + ("yes" if metadata["reset_sequence_issued"] else "unconfirmed")
            + ". Hardware reset and application boot remain unverified; ROM download-mode "
            "straps may prevent the app booting.\n" + text
        )
        if metadata["capture_gap"]:
            text += "\nCapture gap: USB was reopened; early boot logs may have been lost."
        else:
            text += "\nNo reopen gap reported; this does not guarantee complete boot logs."
    return text + "\nRunning binary identity is unverified; configured SHA-256 is not evidence."


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
    review: PlanReview | None
    image: ImageRow | None
    reviewed: bool = False
    local_home_line: HomeLineRow | None = None
    device_observation: DeviceObservationRow | None = None
    cancel_available: bool = False

    @property
    def needs_device_review(self) -> bool:
        # Historical acknowledgement is display metadata, never plan authority.
        return self.state == "needs_review" and not self.reviewed

    @classmethod
    def parse(cls, data: dict[str, Any], epoch: str, started_at: float) -> TimelineCard:
        job_id = data["job_id"]
        if not isinstance(job_id, str) or re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", job_id) is None:
            raise ValueError(INVALID_TIMELINE)
        state, stage, result = data["state"], data["stage"], data["result"]
        if state not in STATES or (stage is not None and stage not in STAGES):
            raise ValueError(INVALID_TIMELINE)
        if result is not None and result not in RESULTS:
            raise ValueError(INVALID_TIMELINE)
        # Older snapshots omit this field: retain the conservative review prompt.
        reviewed = data.get("reviewed", False)
        if type(reviewed) is not bool:
            raise ValueError(INVALID_TIMELINE)
        cancel_available = data.get("cancel_available", False)
        if type(cancel_available) is not bool or (
            cancel_available and state not in ("queued", "running")
        ):
            raise ValueError(INVALID_TIMELINE)
        plan_hash, test, backend = data["plan_hash"], data["test"], data["backend"]
        if test is None and backend is None:
            if plan_hash is not None:
                plan_hash = _hex(plan_hash, 64)
            if data["review"] is not None:
                raise ValueError(INVALID_TIMELINE)
        else:
            plan_hash = _hex(plan_hash, 64)
            test = TestName(test).value
            if backend not in ("mock", "serial", "registered_project"):
                raise ValueError(INVALID_TIMELINE)
            if backend == "registered_project" and test not in (
                "firmware_build",
                "firmware_fetch_source",
            ):
                raise ValueError(INVALID_TIMELINE)
        if cancel_available and (
            backend != "registered_project" or test not in ("firmware_build", "firmware_fetch_source")
        ):
            raise ValueError(INVALID_TIMELINE)
        review = None
        if data["review"] is not None:
            review = PlanReview.parse({**data["review"], "ok": True}, started_at)
            if state not in ("approval_required", "awaiting_approval") or (
                review.broker_epoch,
                review.job_id,
                review.plan_hash,
                review.test,
                review.backend,
            ) != (epoch, job_id, plan_hash, test, backend):
                raise ValueError(INVALID_TIMELINE)
            if review.execution_submitted != (state == "awaiting_approval"):
                raise ValueError(INVALID_TIMELINE)
        image = None
        if data["image"] is not None:
            image = ImageRow.parse(data["image"], started_at)
            if (
                image.job_id != job_id
                or (test is not None and test != "capture_home")
                or (
                    backend is not None
                    and image.source
                    != ("synthetic_fixture" if backend == "mock" else "render_buffer")
                )
            ):
                raise ValueError(INVALID_TIMELINE)
        local_home_line = None
        if data.get("local_home_line") is not None:
            local_home_line = HomeLineRow.parse(data["local_home_line"], started_at)
            if (
                (local_home_line.broker_epoch, local_home_line.job_id, local_home_line.plan_hash)
                != (epoch, job_id, plan_hash)
                or (test, backend) != ("capture_home", "serial")
                or (image and image.recipient_principal != local_home_line.principal_id)
            ):
                raise ValueError(INVALID_TIMELINE)
        observation = None
        if data.get("device_observation") is not None:
            if (backend, state) != ("serial", "succeeded") or (test, result) not in (
                ("firmware_observe", "firmware_observation_completed_no_commands"),
                ("restart_and_observe", "restart_observation_completed_unverified"),
            ):
                raise ValueError(INVALID_TIMELINE)
            observation = DeviceObservationRow.parse(data["device_observation"], job_id, started_at)
        return cls(
            job_id,
            plan_hash,
            test,
            backend,
            state,
            stage,
            result,
            _utc(data["created_utc"]),
            _utc(data["finished_utc"]),
            review,
            image,
            reviewed,
            local_home_line,
            observation,
            cancel_available,
        )


@dataclass(frozen=True)
class TimelineSnapshot:
    broker_epoch: str
    cards: tuple[TimelineCard, ...]
    has_more: bool
    plans_available: bool

    @classmethod
    def parse(cls, data: dict[str, Any], started_at: float) -> TimelineSnapshot:
        try:
            if (
                data["ok"] is not True
                or not isinstance(data["cards"], list)
                or len(data["cards"]) > MAX_CARDS
                or type(data["has_more"]) is not bool
                or type(data["plans_available"]) is not bool
            ):
                raise ValueError(INVALID_TIMELINE)
            epoch = _hex(data["broker_epoch"], 32)
            cards = tuple(TimelineCard.parse(row, epoch, started_at) for row in data["cards"])
            if (
                len({card.job_id for card in cards}) != len(cards)
                or sum(card.image is not None for card in cards) > MAX_PREVIEWS
                or (not data["plans_available"] and any(card.review for card in cards))
            ):
                raise ValueError(INVALID_TIMELINE)
            return cls(epoch, cards, data["has_more"], data["plans_available"])
        except (KeyError, TypeError, ValueError, OverflowError):
            raise ValueError(INVALID_TIMELINE) from None


@dataclass(frozen=True)
class TimelineOperation:
    sequence: int
    generation: int
    action: Action
    started_at: float
    broker_epoch: str = ""
    job_id: str = ""
    review: PlanReview | None = field(default=None, repr=False)
    row: ImageRow | None = None
    delivery_revision: str = ""
    attestation: str = ""
    retain_local_home_line: bool = False
    home_line_row: HomeLineRow | None = None
    plan_hash: str = ""

    @property
    def mutates(self) -> bool:
        return self.action in ("approve_plan", "approve_image", "reject_image", "cancel_job")


@dataclass(frozen=True)
class TimelineOutcome:
    operation: TimelineOperation
    snapshot: TimelineSnapshot | None = None
    preview: ImagePreview | None = field(default=None, repr=False)
    approved: bool = False
    delivery_state: Literal["approved", "rejected"] | None = None
    error: str = ""
    home_line_preview: HomeLinePreview | None = field(default=None, repr=False)
    execution_submitted: bool = False
    cancel_state: str = ""
    stop_requested: bool = False


def perform_timeline(
    api: TimelineAPI, operation: TimelineOperation, clock: Callable[[], float] = time.monotonic
) -> TimelineOutcome:
    """One request per operation; validation and deadlines precede any mutation."""
    try:
        if operation.action == "refresh":
            return TimelineOutcome(
                operation, snapshot=TimelineSnapshot.parse(api.timeline(), operation.started_at)
            )
        if operation.action == "cancel_job":
            _hex(operation.broker_epoch, 32)
            _hex(operation.plan_hash, 64)
            if re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", operation.job_id) is None:
                raise ValueError(CANCEL_UNCERTAIN)
            if clock() >= operation.started_at + 10:
                return TimelineOutcome(operation, error=CANCEL_UNCERTAIN)
            response = api.cancel_job(operation.job_id, operation.plan_hash, operation.broker_epoch)
            if response.get("ok") is False:
                return TimelineOutcome(operation, error=CANCEL_REJECTED)
            if (
                response.get("ok") is not True
                or response.get("job_id") != operation.job_id
                or response.get("plan_hash") != operation.plan_hash
                or response.get("broker_epoch") != operation.broker_epoch
                or response.get("state") not in (
                    "queued", "running", "succeeded", "failed", "cancelled", "needs_review"
                )
                or type(response.get("stop_requested")) is not bool
            ):
                raise ValueError(CANCEL_UNCERTAIN)
            return TimelineOutcome(
                operation, cancel_state=response["state"], stop_requested=response["stop_requested"]
            )
        if operation.action == "approve_plan":
            review = operation.review
            if (
                review is None
                or review.broker_epoch != operation.broker_epoch
                or review.job_id != operation.job_id
                or operation.attestation != ATTESTATIONS[review.attestation]
                or type(operation.retain_local_home_line) is not bool
                or (
                    operation.retain_local_home_line
                    and (review.backend, review.test) != ("serial", "capture_home")
                )
            ):
                raise ValueError(INVALID_TIMELINE)
            if clock() >= review.expires_at:
                return TimelineOutcome(operation, error=EXPIRED_REVIEW)
            if operation.retain_local_home_line:
                response = api.approve(
                    review.plan_hash,
                    review.review_revision,
                    operation.attestation,
                    retain_local_home_line=True,
                )
                submitted, _state = approval_result(
                    response, review.plan_hash, review.execution_submitted
                )
                if response.get("local_home_line_opt_in") is not True:
                    raise ValueError(APPROVAL_UNCERTAIN)
                return TimelineOutcome(operation, approved=True, execution_submitted=submitted)
            result = perform_plan(
                api,  # type: ignore[arg-type]  # This operation calls only approve().
                PlanOperation(
                    operation.sequence,
                    "approve",
                    operation.started_at,
                    review.plan_hash,
                    review.review_revision,
                    operation.attestation,
                    execution_submitted=review.execution_submitted,
                ),
            )
            return TimelineOutcome(
                operation,
                approved=result.approved,
                execution_submitted=result.execution_submitted,
                error=APPROVAL_UNCERTAIN if result.failed else "",
            )
        if operation.action == "home_line":
            home_row = operation.home_line_row
            if (
                home_row is None
                or home_row.job_id != operation.job_id
                or home_row.broker_epoch != operation.broker_epoch
                or home_row.state != "available"
            ):
                raise ValueError(HOME_LINE_FAILED)
            if clock() >= home_row.expires_at:
                return TimelineOutcome(operation, error=HOME_LINE_EXPIRED)
            local_preview = HomeLinePreview.parse(
                api.local_home_line(home_row.job_id, home_row.plan_hash, home_row.diagnostic_id),
                home_row,
                operation.started_at,
            )
            if clock() >= min(home_row.expires_at, local_preview.row.expires_at):
                return TimelineOutcome(operation, error=HOME_LINE_EXPIRED)
            return TimelineOutcome(operation, home_line_preview=local_preview)
        if operation.action not in ("preview", "approve_image", "reject_image"):
            raise ValueError(INVALID_TIMELINE)
        row = operation.row
        if row is None or row.job_id != operation.job_id:
            raise ValueError(INVALID_TIMELINE)
        action = {
            "preview": "preview",
            "approve_image": "approve",
            "reject_image": "reject",
        }[operation.action]
        result_image = perform_image(
            api,  # type: ignore[arg-type]  # This operation never calls images().
            ImageOperation(
                operation.sequence,
                operation.generation,
                action,  # type: ignore[arg-type]
                operation.started_at,
                row,
                operation.delivery_revision,
            ),
            clock,
        )
        state = result_image.delivery_state
        return TimelineOutcome(
            operation,
            preview=result_image.preview,
            delivery_state=state if state in ("approved", "rejected") else None,
            error=result_image.error,
        )
    except Exception:
        error = (
            CANCEL_UNCERTAIN
            if operation.action == "cancel_job"
            else APPROVAL_UNCERTAIN
            if operation.action == "approve_plan"
            else DECISION_FAILED
            if operation.mutates
            else PREVIEW_FAILED
            if operation.action == "preview"
            else HOME_LINE_FAILED
            if operation.action == "home_line"
            else STALE_TIMELINE
        )
        return TimelineOutcome(operation, error=error)


class TimelineController:
    """Headless authority, refresh, bounded preview and late-result policy."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self.clock = clock
        self.snapshot: TimelineSnapshot | None = None
        self.pending: TimelineOperation | None = None
        self.sequence = 0
        self.generation = 0
        self.closed = False
        self.fresh = False
        self.failed = False
        self.next_refresh = 0.0
        self.preview_budget = 0
        self.previews: OrderedDict[str, ImagePreview] = OrderedDict()
        self.home_line_preview: HomeLinePreview | None = None
        self.home_line_attempts: set[HomeLineKey] = set()
        self.displayed: dict[str, ImagePreview] = {}
        self.displayed_reviews: dict[str, ReviewKey] = {}
        self.preview_attempts: set[ImageKey] = set()
        self.plan_attempts: dict[str, ReviewKey] = {}
        self.decision_attempts: dict[str, tuple[ImageKey, str]] = {}
        self.cancel_attempts: dict[str, CancelKey] = {}
        self.cancel_messages: dict[str, str] = {}
        self.card_messages: dict[str, str] = {}
        self.message = "Loading the job timeline..."

    @property
    def busy(self) -> bool:
        return self.pending is not None

    def card(self, job_id: str) -> TimelineCard | None:
        return (
            next((card for card in self.snapshot.cards if card.job_id == job_id), None)
            if self.snapshot
            else None
        )

    def _begin(self, action: Action, **fields: Any) -> TimelineOperation | None:
        if self.closed or self.busy:
            return None
        self.sequence += 1
        self.pending = TimelineOperation(
            self.sequence,
            self.generation,
            action,
            self.clock(),
            self.snapshot.broker_epoch if self.snapshot else "",
            **fields,
        )
        return self.pending

    def refresh(self, *, retry_previews: bool = False) -> TimelineOperation | None:
        operation = self._begin("refresh")
        if operation is not None:
            if retry_previews:
                self.preview_attempts = {image_key(item.row) for item in self.previews.values()}
            self.preview_budget = 0
            self.next_refresh = self.clock() + REFRESH_SECONDS
        return operation

    def mark_review_displayed(self, job_id: str, review: PlanReview) -> bool:
        card = self.card(job_id)
        if self.closed or card is None or card.review is not review:
            return False
        self.displayed_reviews[job_id] = review_key(review)
        return True

    def can_approve(self, job_id: str, attestation: str = "") -> bool:
        card = self.card(job_id)
        review = card.review if card else None
        return bool(
            not (self.closed or self.busy)
            and self.fresh
            and review is not None
            and self.displayed_reviews.get(job_id) == review_key(review)
            and self.plan_attempts.get(job_id) != review_key(review)
            and self.clock() < review.expires_at
            and attestation == ATTESTATIONS[review.attestation]
        )

    def approve(
        self,
        job_id: str,
        expected: ReviewKey,
        attestation: str,
        retain_local_home_line: bool = False,
    ) -> TimelineOperation | None:
        card = self.card(job_id)
        if not self.can_approve(job_id, attestation) or card is None or card.review is None:
            return None
        if review_key(card.review) != expected:
            return None
        if type(retain_local_home_line) is not bool or (
            retain_local_home_line
            and (card.review.backend, card.review.test) != ("serial", "capture_home")
        ):
            return None
        operation = self._begin(
            "approve_plan",
            job_id=job_id,
            review=card.review,
            attestation=attestation,
            retain_local_home_line=retain_local_home_line,
        )
        if operation is not None:
            self.plan_attempts[job_id] = expected
            self.card_messages[job_id] = (
                "Approval pending; this click queues the already submitted exact plan."
                if card.review.execution_submitted
                else "Plan approval pending; this click will not execute it."
            )
        return operation

    def cancel_key(self, job_id: str) -> CancelKey | None:
        card = self.card(job_id)
        if self.snapshot is None or card is None or card.plan_hash is None:
            return None
        return self.snapshot.broker_epoch, job_id, card.plan_hash

    def can_cancel(self, job_id: str) -> bool:
        card, key = self.card(job_id), self.cancel_key(job_id)
        return bool(
            not (self.closed or self.busy) and self.fresh
            and card is not None and card.cancel_available
            and card.state in ("queued", "running") and key is not None
            and self.cancel_attempts.get(job_id) != key
        )

    def cancel_job(self, job_id: str, expected: CancelKey) -> TimelineOperation | None:
        if not self.can_cancel(job_id) or self.cancel_key(job_id) != expected:
            return None
        operation = self._begin("cancel_job", job_id=job_id, plan_hash=expected[2])
        if operation is not None:
            self.cancel_attempts[job_id] = expected
            self.cancel_messages[job_id] = CANCEL_PENDING
        return operation

    def next_home_line(self) -> TimelineOperation | None:
        self.expire()
        if self.closed or self.busy or not self.fresh:
            return None
        for card in self.snapshot.cards if self.snapshot else ():
            row = card.local_home_line
            if (
                row is not None
                and row.state == "available"
                and self.clock() < row.expires_at
                and row.key not in self.home_line_attempts
            ):
                operation = self._begin("home_line", job_id=card.job_id, home_line_row=row)
                if operation is not None:
                    self.home_line_attempts.add(row.key)
                    # Evict before fetching so only one diagnostic preview is retained.
                    self.home_line_preview = None
                return operation
        return None

    def next_preview(self) -> TimelineOperation | None:
        self.expire()
        if self.closed or self.busy or not self.fresh or self.preview_budget <= 0:
            return None
        for card in self.snapshot.cards if self.snapshot else ():
            row = card.image
            if (
                row
                and self.clock() < row.expires_at
                and image_key(row) not in self.preview_attempts
            ):
                operation = self._begin("preview", job_id=card.job_id, row=row)
                if operation is not None:
                    self.preview_attempts.add(image_key(row))
                    self.preview_budget -= 1
                return operation
        return None

    def mark_displayed(self, job_id: str, preview: ImagePreview) -> bool:
        card = self.card(job_id)
        if (
            self.closed
            or not self.fresh
            or self.previews.get(job_id) is not preview
            or card is None
            or card.image is None
            or not card.image.same_image(preview.row)
            or self.clock() >= min(card.image.expires_at, preview.row.expires_at)
        ):
            return False
        self.displayed[job_id] = preview
        return True

    def can_decide(self, job_id: str) -> bool:
        card, preview = self.card(job_id), self.previews.get(job_id)
        return bool(
            not (self.closed or self.busy)
            and self.fresh
            and preview is not None
            and self.displayed.get(job_id) is preview
            and card is not None
            and card.image is not None
            and card.image.same_image(preview.row)
            and preview.row.delivery_state == "pending"
            and self.decision_attempts.get(job_id)
            != (image_key(preview.row), preview.delivery_revision)
            and self.clock() < min(card.image.expires_at, preview.row.expires_at)
        )

    def decide(
        self, job_id: str, preview: ImagePreview, decision: Decision
    ) -> TimelineOperation | None:
        if (
            decision not in ("approve", "reject")
            or not self.can_decide(job_id)
            or self.previews.get(job_id) is not preview
        ):
            return None
        operation = self._begin(
            "approve_image" if decision == "approve" else "reject_image",
            job_id=job_id,
            row=preview.row,
            delivery_revision=preview.delivery_revision,
        )
        if operation is not None:
            self.decision_attempts[job_id] = (image_key(preview.row), preview.delivery_revision)
            self.displayed.pop(job_id, None)
            self.card_messages[job_id] = "Image decision pending; no retry will be sent."
        return operation

    def _clear_previews(self) -> None:
        self.previews.clear()
        self.displayed.clear()

    def _invalidate_home_line_preview(self) -> None:
        """Drop private text, permitting one new read after authority is refreshed."""
        preview, self.home_line_preview = self.home_line_preview, None
        if preview is None:
            return
        # Only a successful cached read can be retried. Failed reads and normal
        # single-preview eviction keep their attempt markers to avoid loops.
        self.home_line_attempts.discard(preview.row.key)
        # Keep the shortest known deadline even after the cached value is gone.
        if self.snapshot is not None:
            self.snapshot = replace(
                self.snapshot,
                cards=tuple(
                    replace(
                        card,
                        local_home_line=replace(
                            card.local_home_line,
                            expires_at=min(card.local_home_line.expires_at, preview.row.expires_at),
                        ),
                    )
                    if card.local_home_line is not None
                    and card.local_home_line.key == preview.row.key
                    else card
                    for card in self.snapshot.cards
                ),
            )

    def _replace_snapshot(self, snapshot: TimelineSnapshot) -> None:
        restarted = (
            self.snapshot is not None and self.snapshot.broker_epoch != snapshot.broker_epoch
        )
        if restarted:
            self.generation += 1
            self._clear_previews()
            self.displayed_reviews.clear()
            self.preview_attempts.clear()
            self.plan_attempts.clear()
            self.decision_attempts.clear()
            self.card_messages.clear()
            self.cancel_attempts.clear()
            self.cancel_messages.clear()
            self.home_line_preview = None
            self.home_line_attempts.clear()
        cards = []
        for card in snapshot.cards:
            prior = self.card(card.job_id) if not restarted else None
            if card.review and prior and prior.review:
                if review_key(card.review) == review_key(prior.review):
                    card = replace(
                        card,
                        review=replace(
                            card.review,
                            expires_at=min(card.review.expires_at, prior.review.expires_at),
                        ),
                    )
            if card.image and prior and prior.image and card.image.same_image(prior.image):
                card = replace(
                    card,
                    image=replace(
                        card.image, expires_at=min(card.image.expires_at, prior.image.expires_at)
                    ),
                )
            if (
                card.device_observation
                and prior
                and prior.device_observation
                and card.device_observation.key == prior.device_observation.key
            ):
                card = replace(
                    card,
                    device_observation=replace(
                        card.device_observation,
                        expires_at=min(
                            card.device_observation.expires_at,
                            prior.device_observation.expires_at,
                        ),
                    ),
                )
            if (
                card.local_home_line
                and prior
                and prior.local_home_line
                and card.local_home_line.key == prior.local_home_line.key
                # The armed approval lease precedes the separate capture TTL.
                and (
                    prior.local_home_line.state != "armed" or card.local_home_line.state == "armed"
                )
            ):
                card = replace(
                    card,
                    local_home_line=replace(
                        card.local_home_line,
                        expires_at=min(
                            card.local_home_line.expires_at, prior.local_home_line.expires_at
                        ),
                    ),
                )
            cards.append(card)
        self.snapshot = replace(snapshot, cards=tuple(cards))
        # A fresh authoritative pending state permits another *explicit* click
        # after an uncertain outcome. No mutation is ever queued by refresh.
        self.plan_attempts.clear()
        self.decision_attempts.clear()
        ids = {card.job_id for card in cards}
        for mapping in (
            self.displayed_reviews,
            self.plan_attempts,
            self.decision_attempts,
            self.card_messages,
            self.cancel_attempts,
            self.cancel_messages,
        ):
            for job_id in set(mapping) - ids:
                del mapping[job_id]
        live_keys = {image_key(card.image) for card in cards if card.image}
        self.preview_attempts.intersection_update(live_keys)
        self.home_line_attempts.intersection_update(
            card.local_home_line.key for card in cards if card.local_home_line
        )
        for job_id, preview in list(self.previews.items()):
            current_card = self.card(job_id)
            if (
                current_card is None
                or current_card.image is None
                or not current_card.image.same_image(preview.row)
            ):
                self.previews.pop(job_id, None)
                self.displayed.pop(job_id, None)
        for card in cards:
            if self.cancel_attempts.get(card.job_id) == self.cancel_key(card.job_id):
                if card.state == "needs_review":
                    self.cancel_messages[card.job_id] = CANCEL_REVIEW
                elif card.state == "cancelled":
                    self.cancel_messages[card.job_id] = (
                        "Broker reports this job cancelled. Device release is a separate state."
                    )
                elif card.state == "succeeded":
                    self.cancel_messages[card.job_id] = (
                        "Job completed before cancellation took effect. No rollback was requested."
                    )
                elif card.state == "failed":
                    self.cancel_messages[card.job_id] = (
                        "Job ended with failure; inspect its result. Device release is not confirmed."
                    )
            elif card.job_id in self.cancel_attempts:
                self.cancel_attempts.pop(card.job_id, None)
                self.cancel_messages.pop(card.job_id, None)
            if self.card_messages.get(card.job_id) == PLAN_APPROVED and card.state != "approved":
                self.card_messages.pop(card.job_id, None)
            if self.card_messages.get(card.job_id) == EXECUTION_QUEUED and card.state != "queued":
                self.card_messages.pop(card.job_id, None)
            if card.review is None or self.displayed_reviews.get(card.job_id) != review_key(
                card.review
            ):
                self.displayed_reviews.pop(card.job_id, None)
        self.fresh, self.failed = True, False
        self.preview_budget = PREVIEWS_PER_CYCLE
        self.next_refresh = self.clock() + REFRESH_SECONDS
        self.message = (
            "Broker restarted. Review the current job cards."
            if restarted
            else (
                "No jobs in this timeline yet. Retained captures expire after 5 minutes."
                if not cards
                else "Request, approval, execution and image delivery stay in each job card."
            )
        )
        if snapshot.has_more:
            self.message += " Showing the latest 32 jobs; older jobs are omitted."
        if not snapshot.plans_available:
            self.message += " Plan approvals are currently unavailable."
        self.expire()

    def accept(self, outcome: TimelineOutcome) -> bool:
        operation = outcome.operation
        if self.closed or self.pending != operation:
            return False
        self.pending = None
        if operation.generation != self.generation or (
            operation.action != "refresh"
            and (self.snapshot is None or operation.broker_epoch != self.snapshot.broker_epoch)
        ):
            self.fresh = False
            self._clear_previews()
            self._invalidate_home_line_preview()
            self.displayed_reviews.clear()
            self.message = "The broker or review changed. Waiting for a fresh timeline."
            return True
        if outcome.error:
            if operation.action == "cancel_job":
                self.cancel_messages[operation.job_id] = outcome.error
            self.failed = True
            if operation.action == "preview":
                self.card_messages[operation.job_id] = PREVIEW_FAILED
            elif operation.action == "home_line":
                self.home_line_preview = None
                self.card_messages[operation.job_id] = (
                    HOME_LINE_EXPIRED if outcome.error == HOME_LINE_EXPIRED else HOME_LINE_FAILED
                )
            else:
                self.fresh = False
                self._invalidate_home_line_preview()
                self.displayed.clear()
                self.displayed_reviews.clear()
                self.message = (
                    (STALE_TIMELINE if self.snapshot else INITIAL_FAILED)
                    if operation.action == "refresh"
                    else outcome.error
                )
                if operation.mutates:
                    self.card_messages[operation.job_id] = outcome.error
                self.next_refresh = self.clock() + REFRESH_SECONDS
            return True
        if outcome.snapshot is not None and operation.action == "refresh":
            self._replace_snapshot(outcome.snapshot)
        elif outcome.home_line_preview is not None and operation.action == "home_line":
            local_preview, card = outcome.home_line_preview, self.card(operation.job_id)
            row = card.local_home_line if card else None
            self.home_line_preview = None
            if (
                row is None
                or operation.home_line_row is None
                or row.key != local_preview.row.key
                or operation.home_line_row.key != local_preview.row.key
                or row.state != "available"
                or local_preview.row.state != "available"
                or self.clock() >= min(row.expires_at, local_preview.row.expires_at)
            ):
                self.card_messages[operation.job_id] = HOME_LINE_EXPIRED
            else:
                self.home_line_preview = local_preview
                self.card_messages.pop(operation.job_id, None)
        elif outcome.preview is not None and operation.action == "preview":
            preview, card = outcome.preview, self.card(operation.job_id)
            if (
                card is None
                or card.image is None
                or operation.row is None
                or not card.image.same_image(preview.row)
                or not operation.row.same_image(preview.row)
                or self.clock() >= min(card.image.expires_at, preview.row.expires_at)
            ):
                self.card_messages[operation.job_id] = EXPIRED_IMAGE
            else:
                while len(self.previews) >= MAX_PREVIEWS:
                    job_id, _ = self.previews.popitem(last=False)
                    self.displayed.pop(job_id, None)
                self.previews[operation.job_id] = preview
                self.card_messages.pop(operation.job_id, None)
        elif operation.mutates:
            self.fresh = False
            self._invalidate_home_line_preview()
            self.displayed_reviews.clear()
            self.displayed.clear()
            self.next_refresh = self.clock()
            if operation.action == "cancel_job":
                self.cancel_messages[operation.job_id] = (
                    CANCEL_REQUESTED if outcome.stop_requested else
                    "Cancellation was not accepted or the job already ended. Refresh status."
                )
            elif outcome.approved and operation.action == "approve_plan":
                self.card_messages[operation.job_id] = (
                    EXECUTION_QUEUED if outcome.execution_submitted else PLAN_APPROVED
                )
            elif outcome.delivery_state is not None:
                self.card_messages[operation.job_id] = (
                    "Image delivery approved; the requesting client retrieves it separately."
                    if outcome.delivery_state == "approved"
                    else "Rejected. This retained image cannot be sent."
                )
                # Drop authority immediately; a fresh row can show the final state.
                self.previews.pop(operation.job_id, None)
            else:
                self.card_messages[operation.job_id] = (
                    APPROVAL_UNCERTAIN if operation.action == "approve_plan" else DECISION_FAILED
                )
        else:
            self.fresh = False
            self._clear_previews()
            self._invalidate_home_line_preview()
            self.message = STALE_TIMELINE
        return True

    def expire(self) -> None:
        local_preview = self.home_line_preview
        if local_preview is not None:
            card = self.card(local_preview.row.job_id)
            row = card.local_home_line if card else None
            if (
                row is None
                or row.key != local_preview.row.key
                or row.state != "available"
                or self.clock() >= min(row.expires_at, local_preview.row.expires_at)
            ):
                self.home_line_preview = None
        for job_id, preview in list(self.previews.items()):
            card = self.card(job_id)
            if (
                card is None
                or card.image is None
                or self.clock() >= min(card.image.expires_at, preview.row.expires_at)
            ):
                self.previews.pop(job_id, None)
                self.displayed.pop(job_id, None)

    def can_close(self) -> bool:
        if self.pending is not None and self.pending.mutates:
            self.message = PENDING_CLOSE
            return False
        return True

    def stop(self) -> bool:
        if not self.can_close():
            return False
        self.closed = True
        self.generation += 1
        self.fresh = False
        self.pending = None
        self._clear_previews()
        self.home_line_preview = None
        self.home_line_attempts.clear()
        self.displayed_reviews.clear()
        self.snapshot = None
        self.preview_attempts.clear()
        self.plan_attempts.clear()
        self.decision_attempts.clear()
        self.card_messages.clear()
        return True


class TimelineWorker:
    def __init__(self, api: TimelineAPI) -> None:
        self.api = api
        self.commands: queue.Queue[TimelineOperation | None] = queue.Queue()
        self.results: queue.Queue[TimelineOutcome] = queue.Queue()
        self.stopped = threading.Event()
        self.result_lock = threading.Lock()
        self.thread = threading.Thread(target=self.run, name="cardputer-timeline", daemon=True)
        self.thread.start()

    def run(self) -> None:
        while not self.stopped.is_set():
            operation = self.commands.get()
            if operation is None or self.stopped.is_set():
                return
            outcome = perform_timeline(self.api, operation)
            with self.result_lock:
                if not self.stopped.is_set():
                    self.results.put(outcome)
            del outcome, operation

    def stop(self) -> None:
        with self.result_lock:
            self.stopped.set()
            self.commands.put(None)
            # Release any already-completed pixels after root shutdown. The
            # shared lock also excludes a result arriving just after this drain.
            try:
                while True:
                    self.results.get_nowait()
            except queue.Empty:
                pass


class _CardView:
    """Stable accordion widgets; only visible reviews confer decision capability."""

    BACKGROUND = "#ffffff"
    MUTED = "#475569"

    def __init__(self, panel: TimelinePanel, job_id: str) -> None:
        import tkinter as tk
        from tkinter import ttk

        self.panel, self.job_id = panel, job_id
        self.review_identity: ReviewKey | None = None
        self.plan_activation: tuple[ReviewKey, bool] | None = None
        self.image_activation: dict[Decision, ImagePreview] = {}
        self.cancel_activation: CancelKey | None = None
        self.last_review: PlanReview | None = None
        self.rendered: ImagePreview | None = None
        self.photo: tk.PhotoImage | None = None
        self.thumbnail: tk.PhotoImage | None = None
        self.open_override: bool | None = None
        self.expanded = False
        self.details_expanded = False
        self.large_preview = False
        self.rendered_home_line: HomeLinePreview | None = None
        self.frame = tk.Frame(
            panel.cards_frame,
            background=self.BACKGROUND,
            highlightbackground="#64748b",
            highlightthickness=2,
            borderwidth=0,
            padx=12,
            pady=10,
        )
        self.frame.columnconfigure(0, weight=1)
        self.header = tk.Frame(self.frame, background=self.BACKGROUND)
        self.header.grid(row=0, column=0, sticky="ew")
        self.header.columnconfigure(0, weight=1)
        self.title_label = tk.Label(
            self.header,
            background=self.BACKGROUND,
            foreground="#0f172a",
            font=("TkDefaultFont", 11, "bold"),
            anchor="w",
            justify="left",
            wraplength=560,
        )
        self.title_label.grid(row=0, column=0, sticky="ew")
        self.toggle_button = ttk.Button(self.header, width=9, command=self._toggle_card)
        self.toggle_button.grid(row=0, column=1, rowspan=2, sticky="ne", padx=(8, 0))
        self.state_var, self.time_var, self.next_var = (
            tk.StringVar(),
            tk.StringVar(),
            tk.StringVar(),
        )
        self.status_row = tk.Frame(self.header, background=self.BACKGROUND)
        self.status_row.grid(row=1, column=0, sticky="ew", pady=(6, 0))
        self.state_label = tk.Label(
            self.status_row, textvariable=self.state_var, padx=6, pady=2, anchor="w"
        )
        self.state_label.pack(side="left")
        self.time_label = tk.Label(
            self.status_row,
            textvariable=self.time_var,
            background=self.BACKGROUND,
            foreground=self.MUTED,
        )
        self.time_label.pack(side="left", padx=8)
        self.next_label = tk.Label(
            self.header,
            textvariable=self.next_var,
            background=self.BACKGROUND,
            foreground=self.MUTED,
            anchor="w",
            justify="left",
            wraplength=720,
        )
        self.next_label.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        self.cancel_button = ttk.Button(self.header, command=self._cancel_job)
        self.cancel_button.grid(row=3, column=0, columnspan=2, sticky="w", pady=(6, 0))
        for event in ("<ButtonPress-1>", "<KeyPress-space>", "<KeyPress-Return>", "<<Invoke>>"):
            self.cancel_button.bind(event, self._arm_cancel)
        self.body = tk.Frame(self.frame, background=self.BACKGROUND)
        self.body.grid(row=1, column=0, sticky="ew", pady=(10, 0))
        self.body.columnconfigure(0, weight=1)
        self.summary_var, self.detail_var, self.image_var, self.message_var = (
            tk.StringVar(),
            tk.StringVar(),
            tk.StringVar(),
            tk.StringVar(),
        )
        self.summary_label = self._label(self.body, self.summary_var)
        self.summary_label.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        self.plan_actions = tk.Frame(self.body, background=self.BACKGROUND)
        self.plan_actions.grid(row=1, column=0, sticky="ew", pady=(0, 8))
        self.attestation_var = tk.StringVar()
        self.phrase = self._label(self.plan_actions)
        self.phrase.pack(anchor="w")
        self.attestation = ttk.Entry(self.plan_actions, textvariable=self.attestation_var)
        self.approve_button = ttk.Button(
            self.plan_actions, text="Approve plan", command=self._approve
        )
        self.approve_button.pack(anchor="w", pady=(4, 0))
        self.home_line_opt_in_var = tk.StringVar(value="")
        self.home_line_opt_in = tk.Checkbutton(
            self.plan_actions,
            text=HOME_LINE_OPT_IN,
            variable=self.home_line_opt_in_var,
            onvalue="1",
            offvalue="",
            background=self.BACKGROUND,
            anchor="w",
            justify="left",
            wraplength=660,
        )
        self.home_line_opt_in_var.trace_add("write", self._home_line_opt_in_changed)
        for event in ("<ButtonPress-1>", "<KeyPress-space>", "<KeyPress-Return>", "<<Invoke>>"):
            self.approve_button.bind(event, self._arm_plan)
        self.attestation_var.trace_add("write", self._attestation_changed)
        self.image_panel = tk.Frame(self.body, background=self.BACKGROUND)
        self.image_panel.grid(row=2, column=0, sticky="ew", pady=(0, 6))
        self.image_panel.columnconfigure(1, weight=1)
        self.image_label = tk.Label(self.image_panel, background=self.BACKGROUND, anchor="nw")
        self.image_label.grid(row=0, column=0, sticky="nw", padx=(0, 10))
        self.image_details = self._label(self.image_panel, self.image_var)
        self.image_details.grid(row=0, column=1, sticky="new")
        self.image_size_button = ttk.Button(
            self.image_panel, text="View full size", command=self._toggle_image
        )
        self.image_size_button.grid(row=1, column=0, sticky="w", pady=(4, 0))
        self.image_actions = tk.Frame(self.body, background=self.BACKGROUND)
        self.image_actions.grid(row=3, column=0, sticky="w", pady=(0, 8))
        self.send_button = ttk.Button(
            self.image_actions, text="Allow image delivery", command=lambda: self._decide("approve")
        )
        self.send_button.pack(side="left")
        self.reject_button = ttk.Button(
            self.image_actions, text="Reject", command=lambda: self._decide("reject")
        )
        self.reject_button.pack(side="left", padx=8)
        for event in ("<ButtonPress-1>", "<KeyPress-space>", "<KeyPress-Return>", "<<Invoke>>"):
            self.send_button.bind(event, lambda _event: self._arm_image("approve"))
            self.reject_button.bind(event, lambda _event: self._arm_image("reject"))
        self.message_label = self._label(self.body, self.message_var)
        self.message_label.grid(row=4, column=0, sticky="ew", pady=(0, 6))
        self.details_button = ttk.Button(
            self.body, text="Show technical details", command=self._toggle_details
        )
        self.details_button.grid(row=5, column=0, sticky="w")
        self.details_frame = tk.Frame(self.body, background="#f1f5f9", padx=8, pady=8)
        self.details_frame.columnconfigure(0, weight=1)
        self.detail_label = self._label(self.details_frame, self.detail_var)
        self.detail_label.configure(background="#f1f5f9", foreground=self.MUTED)
        self.detail_label.grid(row=0, column=0, sticky="ew")
        self.json_text = tk.Text(self.details_frame, height=10, wrap="word", state="disabled")
        self.home_line_panel = tk.Frame(self.body, background=self.BACKGROUND)
        self.home_line_panel.columnconfigure(0, weight=1)
        self.home_line_var = tk.StringVar()
        self.home_line_label = self._label(self.home_line_panel, self.home_line_var)
        self.home_line_label.grid(row=0, column=0, columnspan=2, sticky="ew")
        self.home_line_text = tk.Text(
            self.home_line_panel,
            height=4,
            width=1,
            wrap="char",
            state="disabled",
            exportselection=False,
            undo=False,
        )
        self.home_line_text.grid(row=1, column=0, sticky="ew", pady=(4, 0))
        self.home_line_scroll = ttk.Scrollbar(
            self.home_line_panel, orient="vertical", command=self.home_line_text.yview
        )
        self.home_line_scroll.grid(row=1, column=1, sticky="ns")
        self.home_line_text.configure(yscrollcommand=self.home_line_scroll.set)

    def _label(self, parent: Any, variable: Any = None) -> tk.Label:
        import tkinter as tk

        return tk.Label(
            parent,
            textvariable=variable,
            background=self.BACKGROUND,
            foreground="#1e293b",
            anchor="w",
            justify="left",
            wraplength=720,
        )

    def _hide_authority(self) -> None:
        self.panel.controller.displayed_reviews.pop(self.job_id, None)
        self.panel.controller.displayed.pop(self.job_id, None)
        self.plan_activation = None
        self.image_activation.clear()

    def _attestation_changed(self, *_args: Any) -> None:
        self.approve_button.configure(
            state="normal"
            if self.expanded
            and self.panel.controller.can_approve(self.job_id, self.attestation_var.get())
            else "disabled"
        )

    def _arm_plan(self, _event: Any = None) -> None:
        self.plan_activation = (
            (self.review_identity, self.home_line_opt_in_var.get() == "1")
            if self.expanded and self.review_identity is not None
            else None
        )

    def _home_line_opt_in_changed(self, *_args: Any) -> None:
        self.plan_activation = None

    def _arm_image(self, decision: Decision) -> None:
        if self.expanded and self.rendered is not None:
            self.image_activation[decision] = self.rendered

    def _arm_cancel(self, _event: Any = None) -> None:
        model = self.panel.controller
        self.cancel_activation = model.cancel_key(self.job_id) if model.can_cancel(self.job_id) else None

    def _cancel_job(self) -> None:
        expected, self.cancel_activation = self.cancel_activation, None
        if expected is not None:
            self.panel._submit(self.panel.controller.cancel_job(self.job_id, expected))

    def _approve(self) -> None:
        activation, self.plan_activation = self.plan_activation, None
        if (
            self.expanded
            and activation is not None
            and activation[1] == (self.home_line_opt_in_var.get() == "1")
        ):
            self.panel._submit(
                self.panel.controller.approve(
                    self.job_id,
                    activation[0],
                    self.attestation_var.get(),
                    retain_local_home_line=activation[1],
                )
            )

    def _decide(self, decision: Decision) -> None:
        activation = self.image_activation.pop(decision, None)
        if self.expanded and activation is not None:
            self.panel._submit(self.panel.controller.decide(self.job_id, activation, decision))

    def _toggle_card(self) -> None:
        self.open_override = not self.expanded
        if not self.open_override:
            self._hide_authority()
        self.panel._render()

    def _toggle_details(self) -> None:
        self.details_expanded = not self.details_expanded
        self._show_details()

    def _show_details(self) -> None:
        if self.details_expanded:
            self.details_frame.grid(row=6, column=0, sticky="ew", pady=(6, 0))
        else:
            self.details_frame.grid_remove()
        self.details_button.configure(
            text="Hide technical details" if self.details_expanded else "Show technical details"
        )
        if self.last_review is not None:
            self.json_text.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        else:
            self.json_text.grid_remove()

    def _toggle_image(self) -> None:
        if self.expanded and self.rendered is not None:
            self.large_preview = not self.large_preview
            self.panel._render()

    def clear_image(self) -> None:
        self.image_label.configure(image="", text="")
        self.image_activation.clear()
        self.photo = self.thumbnail = self.rendered = None
        self.large_preview = False
        self.panel.controller.displayed.pop(self.job_id, None)

    def clear_home_line(self) -> None:
        self.home_line_text.configure(state="normal")
        self.home_line_text.delete("1.0", "end")
        self.home_line_text.configure(state="disabled")
        self.rendered_home_line = None

    def _render_home_line(self, card: TimelineCard, now: float) -> None:
        row = card.local_home_line
        preview = self.panel.controller.home_line_preview
        if (
            row is None
            or not self.panel.controller.fresh
            or row.state != "available"
            or not self.expanded
            or preview is None
            or preview.row.key != row.key
            or now >= min(row.expires_at, preview.row.expires_at)
        ):
            preview = None
        if preview is not self.rendered_home_line:
            self.clear_home_line()
            if preview is not None:
                self.home_line_text.configure(state="normal")
                self.home_line_text.insert("1.0", preview.escaped_line or "")
                self.home_line_text.configure(state="disabled")
                self.rendered_home_line = preview
        if row is None:
            self.home_line_panel.grid_remove()
            self.home_line_var.set("")
            return
        self.home_line_panel.grid(row=7, column=0, sticky="ew", pady=(8, 0))
        seconds = max(0, math.ceil(row.expires_at - now))
        status = {
            "armed": "Armed for this job",
            "capturing": "Waiting for the HOME diagnostic line",
            "available": "Escaped received line" if preview else "Local text unavailable",
            "no_matching_line": "No matching HOME line received",
            "line_too_long": "HOME line exceeded the local byte limit",
            "line_rejected": "HOME line rejected",
            "expired": "Expired; local text cleared",
            "cleared": "Local text cleared",
        }[row.state]
        if not seconds:
            status = "Expired; local text cleared"
        self.home_line_var.set(
            f"Local HOME diagnostic · PC memory only · Never sent to AI\n"
            f"{status} · {seconds} s remaining · {row.wire_bytes}/{row.limit_bytes} bytes\n"
            "Whitespace and control bytes are escaped; text is never unescaped."
        )
        if preview is not None:
            self.home_line_text.grid()
            self.home_line_scroll.grid()
        else:
            self.home_line_text.grid_remove()
            self.home_line_scroll.grid_remove()

    @staticmethod
    def _effects(review: PlanReview) -> str:
        # Keep every consequential effect beside approval; longer explanation and
        # exact payload remain inspectable in the nested technical details.
        if review.test in (
            "firmware_observe",
            "restart_and_observe",
            "firmware_build",
            "firmware_backup",
            "firmware_flash",
            "firmware_restore",
        ):
            return review_warning(review)
        if review.backend == "mock":
            effects = "Synthetic test only; no physical device is connected."
        elif review.test == "capture_home":
            effects = (
                "At HOME, sends Esc then s to capture. Elsewhere, Esc can exit an app/editor "
                "and lose unsaved changes. Opening/closing serial may reset the device. "
                "Verify configured firmware and close other serial tools."
            )
        elif review.test == "hello":
            effects = (
                "Exits the current app and sends q/a/u/e navigation keys. "
                "Opening/closing serial may reset the device. "
                "Verify configured firmware and close other serial tools."
            )
        else:
            effects = (
                "Observes serial output without sending commands; "
                "opening/closing serial may reset it."
            )
        effects += (
            " " + EXECUTION_NOTICE
            if review.execution_submitted
            else " Approval does not run the plan; the client starts it separately."
        )
        if review.test == "capture_home":
            effects += (
                " Capture only; image delivery needs separate permission or an existing policy."
            )
        return effects

    @staticmethod
    def _missing_capture(card: TimelineCard) -> str:
        if card.state == "succeeded":
            return EXPIRED_IMAGE
        if (
            card.state == "unknown"
            or card.result
            in (
                "broker_restart_no_replay",
                "serial_operation_uncertain",
                "operation_interrupted_review_required",
                "serial_close_unconfirmed",
                "backend_error_no_retry",
                "device_disconnected_no_retry",
            )
            or (card.state in ("failed", "needs_review") and card.result is None)
        ):
            detail = (
                "The job was reviewed; technical details retain the reported result."
                if card.state == "needs_review" and card.reviewed
                else "Inspect the device and technical details before continuing."
            )
            return "Capture outcome is unknown; no image is available. " + detail
        if card.state in ("failed", "needs_review"):
            return (
                "No usable image was acquired. Capture did not complete successfully; "
                "see technical details for the reported result."
            )
        if card.state == "cancelled":
            return "Capture was cancelled; no image was acquired."
        if card.state == "expired":
            return "The plan expired before capture; no image was acquired."
        return "The retained image will appear here after capture completes."

    @staticmethod
    def _next_action(card: TimelineCard, now: float) -> str:
        if card.review:
            seconds = max(0, math.ceil(card.review.expires_at - now))
            return (
                f"Review and {'approve & start' if card.review.execution_submitted else 'approve'} "
                f"this plan · {seconds} s remaining"
                if seconds
                else "Plan review expired · Refresh for current status"
            )
        if card.state == "awaiting_approval":
            return "Submitted execution is waiting for a current approval review · Refresh status"
        if card.image:
            row = card.image
            remaining = max(0, math.ceil(row.expires_at - now))
            delivery = {
                "pending": "Review image delivery",
                "approved": "Image delivery approved",
                "rejected": "Image delivery rejected",
            }[row.delivery_state]
            if not remaining:
                return (
                    f"{delivery if row.delivery_state != 'pending' else 'Image'} · Preview expired"
                )
            if row.policy_allowed and row.delivery_state == "pending":
                delivery = "Existing policy permits image delivery"
            return f"{delivery} · Preview expires in {remaining} s"
        if card.needs_device_review:
            return "Review device state before continuing"
        if card.test in ("firmware_observe", "restart_and_observe") and card.state == "succeeded":
            observation = card.device_observation
            restart = card.test == "restart_and_observe"
            prefix = (
                "Reset controls and observation completed · Boot unverified · " if restart else ""
            )
            if observation is None or now >= observation.expires_at:
                return (
                    prefix + "Report and firmware metadata unavailable or expired"
                    if restart
                    else "Observation completed · Report and firmware metadata "
                    "unavailable or expired"
                )
            status = observation.metadata["firmware"]["status"]
            status = "unavailable (ambiguous)" if status == "ambiguous" else status
            if restart and status.startswith("unavailable"):
                status = "unknown" + status[len("unavailable") :]
            return (
                prefix + f"Firmware {status} · "
                f"{observation.metadata['retained_lines']} log lines retained · "
                f"Metadata expires in {max(0, math.ceil(observation.expires_at - now))} s"
            )
        if card.test == "restart_and_observe" and card.state == "running":
            return restart_stage_text(card)
        if card.test == "firmware_observe" and card.state == "running":
            return (
                "Serial receive started · At most 30 seconds · No commands sent"
                if card.stage == "firmware_observe"
                else "Preparing or closing serial · Receive is not active"
            )
        if card.state == "needs_review" and card.reviewed:
            return "Failure reviewed · Open for details"
        if card.state in ("approved", "ready"):
            return "Waiting for the requesting client to start"
        if card.state == "queued":
            return (
                "Waiting for the registered project worker"
                if card.backend == "registered_project"
                else "Waiting for the device"
            )
        if card.state == "running":
            return "In progress · Results will appear here"
        if card.test == "capture_home" and card.state == "succeeded":
            return "Capture completed · Retained preview unavailable or expired"
        if card.state == "failed":
            return "Open the card to inspect the result"
        return "Open for details"

    def render(self, card: TimelineCard) -> None:
        import tkinter as tk

        model = self.panel.controller
        now = model.clock()
        review, row = card.review, card.image
        default_open = bool(
            (review and now < review.expires_at)
            or (
                row
                and row.delivery_state == "pending"
                and not row.policy_allowed
                and now < row.expires_at
            )
            or card.needs_device_review
            or (
                card.local_home_line is not None
                and card.local_home_line.state == "available"
                and now < card.local_home_line.expires_at
            )
        )
        self.expanded = default_open if self.open_override is None else self.open_override
        if self.expanded:
            self.body.grid()
        else:
            self.body.grid_remove()
            self._hide_authority()
        self.toggle_button.configure(text="Hide" if self.expanded else "Open")
        short_id = (
            card.job_id if len(card.job_id) <= 28 else card.job_id[:12] + "…" + card.job_id[-12:]
        )
        self.title_label.configure(
            text=REQUESTS.get(card.test or "", "Job details unavailable") + " · " + short_id
        )
        label = (
            "Capture completed"
            if card.state == "succeeded" and card.test == "capture_home"
            else "Failed · reviewed"
            if card.state == "needs_review" and card.reviewed
            else STATE_LABELS[card.state]
        )
        self.state_var.set(
            label
            + (
                " · MOCK"
                if card.backend == "mock"
                else " · SERIAL"
                if card.backend == "serial"
                else " · PROFILE"
                if card.backend == "registered_project"
                else ""
            )
        )
        color, background = (
            ("#92400e", "#fef3c7")
            if default_open
            else ("#166534", "#dcfce7")
            if card.state == "succeeded"
            else ("#991b1b", "#fee2e2")
            if card.state in ("failed", "needs_review")
            else ("#334155", "#e2e8f0")
        )
        self.frame.configure(highlightbackground=color)
        self.state_label.configure(foreground=color, background=background)
        stamp = card.finished_utc or card.created_utc
        self.time_var.set(
            datetime.fromisoformat(stamp).astimezone().strftime("%b %d, %H:%M local")
            if stamp
            else ""
        )
        key = model.cancel_key(card.job_id)
        if self.cancel_activation is not None and self.cancel_activation != key:
            self.cancel_activation = None
        self.cancel_button.configure(
            text=f"Cancel job {card.job_id}",
            state="normal" if model.can_cancel(card.job_id) else "disabled",
        )
        if card.cancel_available or card.job_id in model.cancel_attempts:
            self.cancel_button.grid()
        else:
            self.cancel_button.grid_remove()
        context = SOURCES[row.source] + " · " if row else ""
        self.next_var.set(
            (f"Job {card.job_id}: " + model.cancel_messages[card.job_id])
            if card.job_id in model.cancel_messages
            else context + self._next_action(card, now)
            if model.fresh
            else "Awaiting a verified update · Decisions disabled"
        )
        identity = review_key(review) if review else None
        if self.last_review is not None and self.last_review.plan_hash != card.plan_hash:
            self.last_review = None
            self.json_text.configure(state="normal")
            self.json_text.delete("1.0", "end")
            self.json_text.configure(state="disabled")
        if identity != self.review_identity:
            self.plan_activation = None
            self.review_identity = identity
            self.attestation_var.set("")
            self.home_line_opt_in_var.set("")
        if review is not None and (
            self.last_review is None or review_key(self.last_review) != identity
        ):
            self.last_review = review
            self.json_text.configure(state="normal")
            self.json_text.delete("1.0", "end")
            self.json_text.insert("1.0", review.json_text)
            self.json_text.configure(state="disabled")
        if review:
            # JSON escaping prevents control/bidi text from impersonating the
            # trusted effect/attestation labels; this is the exact reviewed target.
            plan = json.loads(review.json_text)
            target = json.dumps(plan.get("device", "unavailable"), ensure_ascii=True)
            if review.backend == "serial":
                target += " · Port: " + json.dumps(
                    plan.get("port", "unavailable"), ensure_ascii=True
                )
            self.summary_var.set(
                f"Job: {card.job_id} · {card.backend} target: {target}\n" + self._effects(review)
            )
            self.summary_label.grid()
            self.plan_actions.grid()
            if (review.backend, review.test) == ("serial", "capture_home"):
                self.home_line_opt_in.pack(anchor="w", before=self.approve_button, pady=(6, 0))
                self.home_line_opt_in.configure(
                    state="normal" if model.fresh and not model.busy else "disabled"
                )
            else:
                if self.home_line_opt_in_var.get():
                    self.home_line_opt_in_var.set("")
                self.home_line_opt_in.pack_forget()
            required = ATTESTATIONS[review.attestation]
            if required:
                self.phrase.configure(text="Type to confirm: " + required)
                self.attestation.pack(
                    before=self.home_line_opt_in
                    if (review.backend, review.test) == ("serial", "capture_home")
                    else self.approve_button,
                    fill="x",
                    pady=4,
                )
            else:
                self.phrase.configure(
                    text="Approve this exact reset, observation and report delivery"
                    if review.test == "restart_and_observe"
                    else "Approve observation and report delivery"
                    if review.test == "firmware_observe"
                    else "Approve and queue this exact submitted plan"
                    if review.execution_submitted
                    else "Approve this plan only"
                )
                self.attestation.pack_forget()
            self.approve_button.configure(text=approval_button_text(review))
            if self.expanded:
                model.mark_review_displayed(card.job_id, review)
        else:
            model.displayed_reviews.pop(card.job_id, None)
            if card.test in ("firmware_observe", "restart_and_observe"):
                observation_text = firmware_observation_text(card, now, fresh=model.fresh)
                if card.needs_device_review:
                    observation_text += " Inspect the device before continuing."
                self.summary_var.set(observation_text)
                self.summary_label.grid()
            elif card.needs_device_review:
                self.summary_var.set(
                    "Inspect the device before continuing. Technical details contain "
                    "the broker’s reported result."
                )
                self.summary_label.grid()
            else:
                self.summary_var.set("")
                self.summary_label.grid_remove()
            self.plan_actions.grid_remove()
            self.home_line_opt_in.pack_forget()
        self._attestation_changed()
        details = (
            f"Job: {card.job_id}\nBackend: {card.backend or 'unavailable'}\n"
            f"Plan SHA-256: {card.plan_hash or 'unavailable'}"
        )
        if card.stage:
            details += f"\nInternal stage: {card.stage}"
        if card.result:
            details += f"\nResult: {card.result}"
        if card.state == "needs_review":
            details += "\nDevice-state review: " + ("completed" if card.reviewed else "required")
        if review:
            details += f"\nApproval lease: {max(0, math.ceil(review.expires_at - now))} s"
        if card.created_utc:
            details += f"\nCreated (host UTC): {card.created_utc}"
        if card.finished_utc:
            details += f"\nFinished (host UTC): {card.finished_utc}"
        observation = card.device_observation
        if observation and model.fresh and now < observation.expires_at:
            details += (
                f"\nObservation report SHA-256: {observation.sha256}"
                f"\nLog collection stop reason: {observation.metadata['stop_reason']}"
                f"\nFirmware observation reason: {observation.metadata['firmware']['reason']}"
                "\nOnly metadata appears here; log text is retrieved by the authorized client."
            )
        if self.last_review:
            warning = review_warning(self.last_review)
            if card.state not in (
                "prepared",
                "approval_required",
                "awaiting_approval",
                "approved",
                "ready",
            ):
                warning = warning.replace(APPROVAL_NOTICE, "").replace(EXECUTION_NOTICE, "").strip()
            if warning:
                details += "\n\n" + warning
        preview = model.previews.get(card.job_id)
        if preview is not self.rendered:
            self.clear_image()
            if preview is not None and self.expanded:
                try:
                    photo = tk.PhotoImage(master=self.frame, data=base64.b64encode(preview.png))
                    thumbnail = photo.subsample(2)
                    self.photo, self.thumbnail, self.rendered = photo, thumbnail, preview
                except tk.TclError:
                    model.previews.pop(card.job_id, None)
                    model.card_messages[card.job_id] = PREVIEW_FAILED
        if (
            self.rendered is not None
            and self.expanded
            and self.photo is not None
            and self.thumbnail is not None
        ):
            self.image_label.configure(
                image=self.photo if self.large_preview else self.thumbnail, text=""
            )
            self.image_size_button.configure(
                text="Use thumbnail" if self.large_preview else "View full size"
            )
            self.image_size_button.grid()
            model.mark_displayed(card.job_id, self.rendered)
        else:
            model.displayed.pop(card.job_id, None)
            self.image_size_button.grid_remove()
        if row:
            details += (
                f"\n\nAI recipient: {row.recipient_principal}\nImage SHA-256: {row.sha256}"
                f"\nCompleted (host UTC): {row.born_utc}\nDelivery: {row.delivery_state}"
                "\nRetention: 5 minutes. Complete capture; frame coherence unknown; "
                "LCD transfer unverified."
            )
        if row and now < row.expires_at:
            self.image_panel.grid()
            self.image_label.grid()
            caption = (
                f"{SOURCES[row.source]} · Retained preview\n"
                f"Expires in {max(0, math.ceil(row.expires_at - now))} s · "
                f"Delivery {row.delivery_state}"
            )
            if row.delivery_state == "pending":
                caption += (
                    f"\nJob: {card.job_id} · AI recipient: {row.recipient_principal}"
                    "\nAllow lets this recipient retrieve and send the image to its AI service. "
                    "Reject is final."
                )
                if row.policy_allowed:
                    caption += " Existing key policy already permits sending."
                caption += " Frame coherence and LCD transfer are unverified."
                self.image_actions.grid()
            else:
                self.image_actions.grid_remove()
            self.image_var.set(caption)
            if self.rendered is None:
                self.image_label.configure(
                    text="Loading preview…"
                    if image_key(row) not in model.preview_attempts
                    or (
                        model.pending is not None
                        and model.pending.job_id == card.job_id
                        and model.pending.action == "preview"
                    )
                    else "Preview unavailable"
                )
        else:
            self.image_actions.grid_remove()
            self.image_label.grid_remove()
            if card.test == "capture_home":
                self.image_panel.grid()
                self.image_var.set(
                    EXPIRED_IMAGE if row is not None else self._missing_capture(card)
                )
            else:
                self.image_var.set("")
                self.image_panel.grid_remove()
        for button in (self.send_button, self.reject_button):
            button.configure(
                state="normal" if self.expanded and model.can_decide(card.job_id) else "disabled"
            )
        self.detail_var.set(details)
        self._render_home_line(card, now)
        self.resize(self.panel.card_width)
        self._show_details()
        self.message_var.set(model.card_messages.get(card.job_id, ""))
        if self.message_var.get():
            self.message_label.grid()
        else:
            self.message_label.grid_remove()

    def resize(self, width: int) -> None:
        self.title_label.configure(wraplength=max(160, width - 100))
        for label in (self.next_label, self.summary_label, self.message_label, self.phrase):
            label.configure(wraplength=width)
        self.detail_label.configure(wraplength=max(180, width - 18))
        self.home_line_opt_in.configure(wraplength=width)
        self.home_line_label.configure(wraplength=width)
        image_width = 240 if self.large_preview else 120
        if width >= image_width + 230:
            self.image_details.grid(row=0, column=1, columnspan=1, sticky="new")
            self.image_details.configure(wraplength=width - image_width - 14)
        else:
            # Full-size pixels stack above the caption when there is not enough
            # space for both. Enlarging never creates horizontal overflow.
            self.image_details.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(6, 0))
            self.image_details.configure(wraplength=width)

    def destroy(self) -> None:
        self._hide_authority()
        self.clear_image()
        self.clear_home_line()
        self.frame.destroy()


class TimelinePanel:
    """Main-window scrolling cards; no secondary review dialogs or confirmation boxes."""

    def __init__(self, root: tk.Tk, parent: ttk.Frame, api: TimelineAPI) -> None:
        import tkinter as tk
        from tkinter import ttk

        self.root = root
        self.controller = TimelineController()
        self.worker = TimelineWorker(api)
        self.views: dict[str, _CardView] = {}
        self.rendered_epoch: str | None = None
        self.card_width = 660
        self.frame = ttk.Frame(parent)
        self.frame.pack(fill="both", expand=True)
        self.frame.columnconfigure(0, weight=1)
        self.frame.rowconfigure(2, weight=1)
        header = ttk.Frame(self.frame)
        header.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        ttk.Label(header, text="Job timeline", font=("TkDefaultFont", 15, "bold")).pack(side="left")
        self.refresh_button = ttk.Button(header, text="Refresh", command=self.refresh)
        self.refresh_button.pack(side="right")
        self.status_var = tk.StringVar(value=self.controller.message)
        self.status_label = ttk.Label(self.frame, textvariable=self.status_var, wraplength=720)
        self.status_label.grid(row=1, column=0, sticky="ew", pady=(0, 8))
        scroll = ttk.Frame(self.frame)
        scroll.grid(row=2, column=0, sticky="nsew")
        scroll.columnconfigure(0, weight=1)
        scroll.rowconfigure(0, weight=1)
        self.canvas = tk.Canvas(scroll, background="#e7edf4", highlightthickness=0)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(scroll, orient="vertical", command=self.canvas.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.canvas.configure(yscrollcommand=scrollbar.set)
        self.cards_frame = tk.Frame(self.canvas, background="#e7edf4")
        self.cards_frame.columnconfigure(0, weight=1)
        self.canvas_window = self.canvas.create_window((0, 0), anchor="nw", window=self.cards_frame)
        self.cards_frame.bind("<Configure>", self._content_size)
        self.canvas.bind("<Configure>", self._canvas_size)
        # Wheel events over embedded labels do not bubble to their Canvas.
        self.wheel_bindings: list[tuple[str, str]] = []
        for event in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            binding = root.bind(event, self._wheel, add="+")
            if binding is not None:
                self.wheel_bindings.append((event, binding))
        self.refresh()
        root.after(75, self._drain)
        root.after(250, self._tick)

    def _content_size(self, _event: Any) -> None:
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _canvas_size(self, event: Any) -> None:
        self.canvas.itemconfigure(self.canvas_window, width=event.width)
        self.card_width = max(260, event.width - 38)
        self.status_label.configure(wraplength=self.card_width)
        for view in self.views.values():
            view.resize(self.card_width)

    def _wheel(self, event: Any) -> str | None:
        widget = event.widget
        if widget.winfo_class() in ("Text", "Entry", "TEntry", "Treeview", "Listbox"):
            return None
        while widget is not None and widget is not self.canvas:
            widget = getattr(widget, "master", None)
        if widget is None:
            return None
        direction = -1 if getattr(event, "num", None) == 4 or getattr(event, "delta", 0) > 0 else 1
        self.canvas.yview_scroll(direction * 3, "units")
        return "break"

    def _visible_anchor(self) -> tuple[str, float] | None:
        top = self.canvas.canvasy(0)
        if top <= 1:
            return None
        for job_id, view in self.views.items():
            y = view.frame.winfo_y()
            if y <= top < y + view.frame.winfo_height():
                return job_id, top - y
        return None

    def _submit(self, operation: TimelineOperation | None) -> None:
        if operation is not None:
            self.worker.commands.put(operation)
        self._render()

    def refresh(self) -> None:
        self._submit(self.controller.refresh(retry_previews=True))

    def _render(self) -> None:
        model = self.controller
        anchor = self._visible_anchor()
        model.expire()
        snapshot = model.snapshot
        epoch = snapshot.broker_epoch if snapshot else None
        if epoch != self.rendered_epoch:
            for view in self.views.values():
                view.destroy()
            self.views.clear()
            self.rendered_epoch = epoch
        cards = snapshot.cards if snapshot else ()
        ids = {card.job_id for card in cards}
        for job_id in set(self.views) - ids:
            self.views.pop(job_id).destroy()
        for index, card in enumerate(cards):
            if card.job_id not in self.views:
                self.views[card.job_id] = _CardView(self, card.job_id)
            view = self.views[card.job_id]
            view.frame.grid(row=index, column=0, sticky="ew", padx=(2, 8), pady=(2, 12))
            view.render(card)
        self.status_var.set(model.message)
        self.status_label.configure(foreground="#9b1c1c" if model.failed else "")
        self.refresh_button.configure(state="disabled" if model.busy or model.closed else "normal")
        if anchor and anchor[0] in self.views:
            self.root.update_idletasks()
            height = max(1, self.cards_frame.winfo_height())
            anchored = self.views[anchor[0]].frame
            offset = anchor[1] if anchor[1] < anchored.winfo_height() else 0
            self.canvas.yview_moveto((anchored.winfo_y() + offset) / height)

    def _drain(self) -> None:
        if self.worker.stopped.is_set():
            return
        try:
            while True:
                outcome = self.worker.results.get_nowait()
                if self.controller.accept(outcome):
                    self._render()
                del outcome
        except queue.Empty:
            pass
        if not self.controller.busy:
            self._submit(self.controller.next_home_line() or self.controller.next_preview())
        self.root.after(75, self._drain)

    def _tick(self) -> None:
        if self.worker.stopped.is_set():
            return
        self._render()
        if not self.controller.busy and self.controller.clock() >= self.controller.next_refresh:
            self._submit(self.controller.refresh())
        self.root.after(250, self._tick)

    def focus_pending(self) -> None:
        cards = self.controller.snapshot.cards if self.controller.snapshot else ()
        target = next(
            (
                card
                for card in cards
                if (card.review and self.controller.clock() < card.review.expires_at)
                or (
                    card.image
                    and card.image.delivery_state == "pending"
                    and not card.image.policy_allowed
                    and self.controller.clock() < card.image.expires_at
                )
            ),
            None,
        )
        if target and target.job_id in self.views:
            view = self.views[target.job_id]
            view.open_override = True
            self._render()
            self.root.update_idletasks()
            height = max(1, self.cards_frame.winfo_height())
            self.canvas.yview_moveto(view.frame.winfo_y() / height)
            if target.review:
                (
                    view.attestation if target.review.attestation != "none" else view.approve_button
                ).focus_set()
            else:
                view.send_button.focus_set()
        else:
            self.canvas.yview_moveto(0)

    def can_close_window(self) -> bool:
        allowed = self.controller.can_close()
        if not allowed:
            self._render()
        return allowed

    def stop(self) -> bool:
        if not self.controller.stop():
            self._render()
            return False
        self.worker.stop()
        for event, binding in self.wheel_bindings:
            self.root.unbind(event, binding)
        self.wheel_bindings.clear()
        for view in self.views.values():
            view.destroy()
        self.views.clear()
        return True
