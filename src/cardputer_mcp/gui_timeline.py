"""Read-only job timeline and retained preview; explicit cancellation is separate.

Worker reads never prepare, execute, approve or recapture. The broker retains
all ownership decisions. Closing this view never releases physical ownership.
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import queue
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal, Protocol

from .gui_images import SOURCES, ImagePreview, ImageRow
from .job_failure_reason import REASON_LABELS
from .models import TestName
from .operator_timeline import RESULTS, STAGES, STATES, observation_summary

if TYPE_CHECKING:
    import tkinter as tk
    from tkinter import ttk

MAX_CARDS = 32
MAX_PREVIEWS = 8
# The broker admits at most 10,000 durable jobs. Keep bounded identity/deadline
# tombstones for this epoch so a disappearing row cannot renew its retention.
MAX_RETENTION_IDENTITIES = 10000
INVALID_TIMELINE = "The broker returned an invalid timeline. Refresh status."
EXPIRED_OBSERVATION = "Device report expired; no observation was requested."
EXPIRED_IMAGE = "Retained image expired; no capture was requested."
STALE_TIMELINE = "Timeline update failed. Showing previous status; refresh before cancelling."
PREVIEW_FAILED = "Image preview unavailable; no capture or retry was requested."
CANCEL_UNCERTAIN = "Cancellation outcome unknown. Refresh status; no retry or release was sent."
CANCEL_REQUESTED = "Cancellation requested; worker cleanup and device release are not confirmed."
REQUESTS = {test.value: test.value.replace("_", " ") for test in TestName}
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
    review: None = None
    image: ImageRow | None = None
    reviewed: bool = False
    device_observation: DeviceObservationRow | None = None
    cancel_available: bool = False
    failure_reason: str | None = None

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


class TimelineController:
    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self.clock = clock
        self.snapshot: TimelineSnapshot | None = None
        self.sequence = self.generation = 0
        self.pending: TimelineOperation | None = None
        self.preview: ImagePreview | None = None
        self.selected_job = ""
        self.fresh = False
        self.closed = False
        self.message = "Waiting for job history"
        self._retention: dict[tuple[str, ...], tuple[float, str]] = {}

    def _clamp_retention(
        self, snapshot: TimelineSnapshot
    ) -> tuple[TimelineSnapshot, dict[tuple[str, ...], tuple[float, str]]]:
        restarted = self.snapshot is not None and (
            self.snapshot.broker_epoch != snapshot.broker_epoch
        )
        retention = {} if restarted else self._retention.copy()
        cards = []
        key: tuple[str, ...]
        for card in snapshot.cards:
            image = card.image
            if image is not None:
                key = (
                    "image",
                    image.job_id,
                    image.sha256,
                    image.source,
                    image.recipient_principal,
                    image.born_utc,
                )
                prior = retention.get(key)
                expiry = min(image.expires_at, prior[0]) if prior else image.expires_at
                retention[key] = (expiry, "")
                card = replace(card, image=replace(image, expires_at=expiry))
            report = card.device_observation
            if report is not None:
                key = ("report", *report.key)
                metadata_hash = hashlib.sha256(
                    json.dumps(report.metadata, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest()
                prior = retention.get(key)
                if prior is not None and prior[1] != metadata_hash:
                    raise ValueError(INVALID_TIMELINE)
                expiry = min(report.expires_at, prior[0]) if prior else report.expires_at
                retention[key] = (expiry, metadata_hash)
                card = replace(card, device_observation=replace(report, expires_at=expiry))
            cards.append(card)
        if len(retention) > MAX_RETENTION_IDENTITIES:
            raise ValueError(INVALID_TIMELINE)
        return replace(snapshot, cards=tuple(cards)), retention

    @property
    def busy(self) -> bool:
        return self.pending is not None

    def card(self, job_id: str) -> TimelineCard | None:
        if self.snapshot is None:
            return None
        return next((c for c in self.snapshot.cards if c.job_id == job_id), None)

    def select(self, job_id: str) -> None:
        if self.selected_job != job_id:
            self.selected_job = job_id
            self.preview = None

    def _begin(
        self, action: Literal["refresh", "preview", "cancel_job"], **fields: Any
    ) -> TimelineOperation | None:
        if self.closed or self.pending:
            return None
        self.sequence += 1
        self.pending = TimelineOperation(
            self.sequence, self.generation, action, self.clock(), **fields
        )
        return self.pending

    def refresh(self, *, retry_previews: bool = False) -> TimelineOperation | None:
        self.fresh = False
        return self._begin("refresh")

    def cancel_key(self, job_id: str) -> CancelKey | None:
        card = self.card(job_id)
        if card and card.plan_hash and self.snapshot:
            return (self.snapshot.broker_epoch, job_id, card.plan_hash)
        return None

    def can_cancel(self, job_id: str) -> bool:
        card = self.card(job_id)
        return bool(self.fresh and not self.busy and card and card.cancel_available)

    def cancel_job(self, job_id: str, expected: CancelKey) -> TimelineOperation | None:
        if not self.can_cancel(job_id) or self.cancel_key(job_id) != expected:
            return None
        self.fresh = False
        return self._begin(
            "cancel_job", broker_epoch=expected[0], job_id=job_id, plan_hash=expected[2]
        )

    def load_preview(self, job_id: str) -> TimelineOperation | None:
        card = self.card(job_id)
        if not self.fresh or card is None or card.image is None or not self.snapshot:
            return None
        if self.clock() >= card.image.expires_at:
            self.preview = None
            return None
        self.select(job_id)
        return self._begin(
            "preview", job_id=job_id, broker_epoch=self.snapshot.broker_epoch, row=card.image
        )

    def accept(self, outcome: TimelineOutcome) -> bool:
        if self.closed or self.pending != outcome.operation:
            return False
        self.pending = None
        self.message = outcome.error or outcome.message or "Timeline refreshed"
        if outcome.error:
            if outcome.operation.action == "refresh":
                self.fresh = False
            return True
        if outcome.snapshot:
            try:
                snapshot, retention = self._clamp_retention(outcome.snapshot)
            except (ValueError, TypeError):
                self.fresh = False
                self.preview = None
                self.message = STALE_TIMELINE
                return True
            if self.snapshot and self.snapshot.broker_epoch != outcome.snapshot.broker_epoch:
                self.generation += 1
                self.preview = None
            self.snapshot = snapshot
            self._retention = retention
            self.fresh = True
            if self.preview:
                card = self.card(self.preview.row.job_id)
                if (
                    card is None
                    or card.image is None
                    or not card.image.same_image(self.preview.row)
                ):
                    self.preview = None
                else:
                    self.preview = replace(
                        self.preview,
                        row=replace(
                            self.preview.row,
                            expires_at=min(self.preview.row.expires_at, card.image.expires_at),
                        ),
                    )
        if outcome.preview:
            self.preview = (
                outcome.preview
                if (
                    self.clock() < outcome.preview.row.expires_at
                    and self.selected_job == outcome.operation.job_id
                )
                else None
            )
        self.expire()
        return True

    def expire(self) -> None:
        if self.preview and self.clock() >= self.preview.row.expires_at:
            self.preview = None

    def can_close(self) -> bool:
        return not self.busy

    def stop(self) -> bool:
        if self.busy:
            return False
        self.closed = True
        self.fresh = False
        self.preview = None
        return True


class TimelinePanel:
    def __init__(self, root: tk.Tk, parent: ttk.Frame, api: TimelineAPI) -> None:
        import tkinter as tk
        from tkinter import ttk

        self.root, self.api = root, api
        self.controller = TimelineController()
        self.commands: queue.Queue[TimelineOperation | None] = queue.Queue()
        self.results: queue.Queue[TimelineOutcome] = queue.Queue()
        self.stopped = False
        self.photo: Any = None
        self.frame = ttk.Frame(parent)
        self.frame.pack(fill="both", expand=True)
        self.message = tk.StringVar(value="Waiting for job history")
        ttk.Label(self.frame, textvariable=self.message, wraplength=760).pack(anchor="w")
        self.tree = ttk.Treeview(
            self.frame, columns=("test", "state", "stage", "result"), show="tree headings"
        )
        self.tree.heading("#0", text="Job")
        for key in ("test", "state", "stage", "result"):
            self.tree.heading(key, text=key.title())
            self.tree.column(key, width=150)
        self.tree.pack(fill="both", expand=True, pady=8)
        controls = ttk.Frame(self.frame)
        controls.pack(fill="x")
        ttk.Button(controls, text="Refresh", command=self.refresh).pack(side="left")
        self.preview_button = ttk.Button(
            controls, text="Preview retained image", command=self._preview
        )
        self.preview_button.pack(side="left", padx=8)
        self.cancel_button = ttk.Button(controls, text="Cancel selected job", command=self._cancel)
        self.cancel_button.pack(side="left")
        self.image = ttk.Label(self.frame)
        self.image.pack(pady=8)
        self.tree.bind("<<TreeviewSelect>>", self._select)
        threading.Thread(target=self._worker, daemon=True).start()
        self.refresh()
        self.root.after(100, self._poll)
        self.root.after(2000, self._refresh_idle)

    def _selected(self) -> str:
        selected = self.tree.selection()
        return selected[0] if selected else ""

    def _select(self, _event: Any = None) -> None:
        self.controller.select(self._selected())
        if self.controller.preview is None:
            self.photo = None
            self.image.configure(image="", text="")
        self._controls()

    def _submit(self, operation: TimelineOperation | None) -> None:
        if operation:
            self.commands.put(operation)
        self._controls()

    def refresh(self) -> None:
        self._submit(self.controller.refresh())

    def _preview(self) -> None:
        self._submit(self.controller.load_preview(self._selected()))

    def _cancel(self) -> None:
        job = self._selected()
        key = self.controller.cancel_key(job)
        if key:
            self._submit(self.controller.cancel_job(job, key))

    def _controls(self) -> None:
        job = self._selected()
        card = self.controller.card(job)
        preview = bool(
            self.controller.fresh
            and not self.controller.busy
            and card
            and card.image
            and self.controller.clock() < card.image.expires_at
        )
        self.preview_button.configure(state="normal" if preview else "disabled")
        self.cancel_button.configure(
            state="normal" if self.controller.can_cancel(job) else "disabled"
        )

    def _worker(self) -> None:
        while True:
            operation = self.commands.get()
            if operation is None:
                return
            self.results.put(perform_timeline(self.api, operation))

    def _render(self) -> None:
        selected = self._selected()
        self.tree.delete(*self.tree.get_children())
        if self.controller.snapshot:
            for card in self.controller.snapshot.cards:
                self.tree.insert(
                    "",
                    "end",
                    iid=card.job_id,
                    text=card.job_id,
                    values=(card.test or "", card.state, card.stage or "", card.result or ""),
                )
        if self.tree.exists(selected):
            self.tree.selection_set(selected)
        preview = self.controller.preview
        if preview:
            import tkinter as tk

            try:
                self.photo = tk.PhotoImage(
                    master=self.root, data=base64.b64encode(preview.png).decode()
                )
                self.image.configure(image=self.photo, text=SOURCES[preview.row.source])
            except Exception:
                self.photo = None
                self.controller.preview = None
                self.controller.message = PREVIEW_FAILED
                self.image.configure(image="", text=PREVIEW_FAILED)
        else:
            self.photo = None
            self.image.configure(image="", text="")
        self.message.set(self.controller.message)
        self._controls()

    def _poll(self) -> None:
        if self.stopped:
            return
        try:
            while True:
                if self.controller.accept(self.results.get_nowait()):
                    self._render()
        except queue.Empty:
            pass
        previous = self.controller.preview
        self.controller.expire()
        if previous is not self.controller.preview:
            self._render()
        self.root.after(100, self._poll)

    def _refresh_idle(self) -> None:
        if self.stopped:
            return
        if not self.controller.busy:
            self.refresh()
        self.root.after(2000, self._refresh_idle)

    def can_close_window(self) -> bool:
        return self.controller.can_close()

    def stop(self) -> bool:
        if not self.controller.stop():
            return False
        self.stopped = True
        self.commands.put(None)
        self.photo = None
        return True
