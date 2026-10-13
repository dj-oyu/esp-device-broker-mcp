"""Manual, exact-plan operator review without device access or automatic retries.

The worker is the only caller of the loopback operator API. Tk callbacks own all
view and selection state; a result never substitutes a newer plan for a reviewed
one. Dismissing either window cannot cancel a request that has already been sent.
"""

from __future__ import annotations

import hashlib
import json
import math
import queue
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, Protocol

from .models import FLASH_TESTS, TestName

if TYPE_CHECKING:
    import tkinter as tk


MAX_PLANS = 64
MAX_PLAN_BYTES = 65536
REQUEST_FAILED = (
    "Plan request failed. Refresh and inspect the plan again; no automatic retry occurred."
)
APPROVAL_UNCERTAIN = (
    "Approval was not confirmed. It may have reached the broker. "
    "Refresh and inspect before making another decision; nothing was replayed."
)
PENDING_CLOSE = (
    "A plan request is still pending. Closing does not cancel it. "
    "Wait for its result, then close again."
)
APPROVAL_NOTICE = (
    "Approval does not execute the plan. The requesting client must execute separately."
)
EXECUTION_NOTICE = (
    "The client already requested this exact plan. Approval queues it immediately; "
    "physical start may wait for safe ownership of the device."
)
EXECUTION_QUEUED = "Approved and queued. Physical start may wait for safe ownership of the device."
CAPTURE_NOTICE = (
    "This approval permits capture only. Image transmission needs an applicable operator policy "
    "or a separate local preview and send approval."
)
FIRMWARE_NOTICE = (
    "Receives device serial output for at most 30 seconds. No writes, commands, reset command, "
    "firmware flash or automatic reconnect. Opening/closing serial may still reset the device "
    "due to OS/driver control-line glitches; close other serial tools. Device log text may "
    "include private data. Known credential patterns are redacted, but redaction is not "
    "guaranteed complete. "
)
RESTART_NOTICE = (
    "ESP32-S3 native USB Serial/JTAG ONLY (VID 303A / PID 1001 and a nonempty USB serial "
    "number). These VID/PID values are shared with other chips and do not prove ESP32-S3. "
    "One hardware reset can exit the running app and lose unsaved state. Uses one 100 ms "
    "RTS pulse with DTR cleared; no reset retry. Opening/closing serial can still glitch "
    "control lines and cause additional unintended resets. "
    "Discards buffered pre-reset serial input. "
    "Receives at most 30 seconds and 64 KiB of device console text, including reconnect wait. "
    "If disconnected, reopens at most once within 8 seconds after reset, using the same USB "
    "identity (VID, PID, serial number and location). Reopening may lose early boot logs. "
    "No serial command bytes, bootloader commands or firmware flash. Close other serial tools. "
    "Device console text may include private data. Known credential patterns are redacted, "
    "but redaction is not guaranteed complete. Control-line completion does not verify "
    "hardware reset or application boot; ROM download-mode straps may prevent the app booting. "
    "Missing firmware identity remains unknown. No automatic policy approval is allowed. "
)
ATTESTATIONS = {
    "none": "",
    "flash_device_read_verified": "REGISTERED DEVICE VERIFIED FOR THIS BACKUP READ",
    "firmware_recovery_verified": "DEVICE AND FIRMWARE RECOVERY VERIFIED FOR THIS EXACT PLAN",
    "flash_device_backup_verified": "DEVICE AND BACKUP VERIFIED FOR THIS EXACT FLASH PLAN",
    "device_free_firmware": "DEVICE FREE AND FIRMWARE VERIFIED",
    "device_home_firmware": "DEVICE HOME AND FIRMWARE VERIFIED",
}
Action = Literal["refresh", "inspect", "approve"]


class PlanAPI(Protocol):
    def plans(self) -> dict[str, Any]: ...
    def plan(self, plan_hash: str) -> dict[str, Any]: ...
    def approve(self, plan_hash: str, review_revision: str, attestation: str) -> dict[str, Any]: ...


def _hex(value: Any, length: int) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[a-f0-9]{" + str(length) + "}", value) is None:
        raise ValueError("invalid_plan_response")
    return value


def _remaining(value: Any) -> float:
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 300:
        raise ValueError("invalid_plan_response")
    return float(value)


def _execution_context(data: dict[str, Any], remaining: float) -> tuple[bool, float | None]:
    """Absent fields keep old brokers safely in prepare-only mode."""
    submitted = data.get("execution_submitted", False)
    intent = data.get("intent_remaining_seconds")
    if type(submitted) is not bool:
        raise ValueError("invalid_plan_response")
    if submitted:
        intent = _remaining(intent)
        if not 0 < intent <= remaining:
            raise ValueError("invalid_plan_response")
    elif intent is not None:
        raise ValueError("invalid_plan_response")
    return submitted, intent


def approval_result(
    response: dict[str, Any], plan_hash: str, execution_submitted: bool
) -> tuple[bool, str | None]:
    submitted = response.get("execution_submitted", False)
    state = response.get("job_state")
    if (
        response.get("ok") is not True
        or response.get("approved") is not True
        or response.get("plan_hash") != plan_hash
        or type(response.get("already_approved")) is not bool
        or type(submitted) is not bool
        or submitted != execution_submitted
        or (submitted and state != "queued")
        or (not submitted and state is not None)
    ):
        raise ValueError("invalid_plan_response")
    return submitted, state


def approval_button_text(review: PlanReview | None) -> str:
    if review and review.execution_submitted:
        return "Approve & start"
    if review and review.test == "restart_and_observe":
        return "Approve reset, observation and delivery"
    if review and review.test == "firmware_observe":
        return "Approve observation and delivery"
    return "Approve plan"


@dataclass(frozen=True)
class PlanRow:
    plan_hash: str
    job_id: str
    test: str
    backend: str
    expires_at: float
    execution_submitted: bool = False

    @classmethod
    def parse(cls, data: dict[str, Any], started_at: float) -> PlanRow:
        identifier = data["job_id"]
        if (
            not isinstance(identifier, str)
            or re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", identifier) is None
        ):
            raise ValueError("invalid_plan_response")
        test, backend = TestName(data["test"]).value, data["backend"]
        if backend not in ("mock", "serial"):
            raise ValueError("invalid_plan_response")
        remaining = _remaining(data["lease_remaining_seconds"])
        submitted, intent = _execution_context(data, remaining)
        return cls(
            _hex(data["plan_hash"], 64),
            identifier,
            test,
            backend,
            started_at + (min(remaining, intent) if intent is not None else remaining),
            submitted,
        )


@dataclass(frozen=True)
class PlanList:
    broker_epoch: str
    plans: tuple[PlanRow, ...]
    has_more: bool

    @classmethod
    def parse(cls, data: dict[str, Any], started_at: float) -> PlanList:
        rows = data["plans"]
        if (
            data["ok"] is not True
            or not isinstance(rows, list)
            or len(rows) > MAX_PLANS
            or type(data["has_more"]) is not bool
        ):
            raise ValueError("invalid_plan_response")
        plans = tuple(PlanRow.parse(row, started_at) for row in rows)
        if len({row.plan_hash for row in plans}) != len(plans):
            raise ValueError("invalid_plan_response")
        return cls(_hex(data["broker_epoch"], 32), plans, data["has_more"])


def _bounded_json(data: Any) -> None:
    pending = [(data, 0)]
    nodes = size = 0
    while pending:
        value, depth = pending.pop()
        nodes += 1
        if depth > 12 or nodes > 4096:
            raise ValueError("invalid_plan_response")
        if isinstance(value, dict):
            if len(value) > 128:
                raise ValueError("invalid_plan_response")
            for key, child in value.items():
                if not isinstance(key, str) or len(key) > 1024:
                    raise ValueError("invalid_plan_response")
                size += len(key)
                pending.append((child, depth + 1))
        elif isinstance(value, list):
            if len(value) > 256:
                raise ValueError("invalid_plan_response")
            pending.extend((child, depth + 1) for child in value)
        elif isinstance(value, str):
            size += len(value)
        elif value is not None and type(value) not in (bool, int, float):
            raise ValueError("invalid_plan_response")
        if size > MAX_PLAN_BYTES:
            raise ValueError("invalid_plan_response")


@dataclass(frozen=True)
class PlanReview:
    plan_hash: str
    broker_epoch: str
    job_id: str
    test: str
    backend: str
    review_revision: str
    expires_at: float
    attestation: str
    json_text: str
    log_recipient: str | None = None
    execution_submitted: bool = False
    profile_execution_policy: str = "inherit"

    @classmethod
    def parse(cls, data: dict[str, Any], started_at: float) -> PlanReview:
        plan = data["plan"]
        if data["ok"] is not True or not isinstance(plan, dict):
            raise ValueError("invalid_plan_response")
        _bounded_json(plan)
        # ASCII escapes preserve all data while preventing control/bidi strings
        # from impersonating other fields in the native text widget.
        canonical = json.dumps(plan, sort_keys=True, separators=(",", ":"), allow_nan=False)
        rendered = json.dumps(plan, indent=2, sort_keys=True, ensure_ascii=True, allow_nan=False)
        if len(rendered) > MAX_PLAN_BYTES:
            raise ValueError("invalid_plan_response")
        plan_hash = _hex(data["plan_hash"], 64)
        if hashlib.sha256(canonical.encode()).hexdigest() != plan_hash:
            raise ValueError("invalid_plan_response")
        row = PlanRow.parse(
            {
                **plan,
                "plan_hash": plan_hash,
                "lease_remaining_seconds": data["lease_remaining_seconds"],
                "execution_submitted": data.get("execution_submitted", False),
                "intent_remaining_seconds": data.get("intent_remaining_seconds"),
            },
            started_at,
        )
        expected = "none"
        if row.backend == "serial":
            if row.test not in (
                "passive_observe",
                "firmware_build",
                "firmware_backup",
                "firmware_flash",
                "firmware_restore",
                "firmware_observe",
                "restart_and_observe",
                "hello",
                "capture_home",
            ):
                raise ValueError("invalid_plan_response")
            if row.test == "hello":
                expected = "device_free_firmware"
            elif row.test == "capture_home":
                expected = "device_home_firmware"
        if row.test == TestName.BACKUP.value:
            expected = "flash_device_read_verified"
        elif row.test in (TestName.FLASH.value, TestName.RESTORE.value):
            expected = (
                "firmware_recovery_verified"
                if plan["recovery_kind"] == "artifact_bundle"
                else "flash_device_backup_verified"
            )
        if data["attestation"] != expected:
            raise ValueError("invalid_plan_response")
        log_recipient = None
        if row.test in {t.value for t in FLASH_TESTS}:
            from .firmware_flash import validate_flash_plan

            validate_flash_plan(plan)
            log_recipient = plan["flash_principal"]
        if row.test == "firmware_build":
            recipient = plan.get("build_principal")
            if (
                not isinstance(recipient, str)
                or re.fullmatch(r"[A-Za-z0-9_.:@-]{1,128}", recipient) is None
                or plan.get("profile_execution_policy") not in ("inherit", "RemoteSigned")
                or plan.get("expected_idf") != "6.0.1"
                or plan.get("target") != "esp32s3"
                or plan.get("stages") != ["idf_profile", "idf_diagnostics", "idf_build"]
            ):
                raise ValueError("invalid_plan_response")
            _hex(plan.get("fingerprint"), 64)
            log_recipient = recipient
        if row.test in ("firmware_observe", "restart_and_observe"):
            delivery = plan.get("log_delivery")
            limits = {
                "retention_seconds": 300,
                "max_received_bytes": 65536,
                "max_lines": 256,
                "max_line_bytes": 1024,
                "max_rendered_bytes": 32768,
            }
            if (
                row.backend != "serial"
                or type(plan.get("observe_seconds")) not in (int, float)
                or plan["observe_seconds"] != 30
                or type(delivery) is not dict
                or set(delivery) != {"principal_id", "origin"} | set(limits)
                or not isinstance(delivery.get("principal_id"), str)
                or re.fullmatch(r"[A-Za-z0-9_.:@-]{1,128}", delivery["principal_id"]) is None
                or delivery.get("origin") not in ("local_cli", "remote_mcp")
                or any(
                    type(delivery[key]) is not int or delivery[key] != value
                    for key, value in limits.items()
                )
            ):
                raise ValueError("invalid_plan_response")
            log_recipient = delivery["principal_id"]
        if row.test == "restart_and_observe":
            restart = plan.get("restart")
            fixed = {
                "method": "esp32s3_usb_serial_jtag_rts_v1",
                "attempts": 1,
                "rts_hold_milliseconds": 100,
                "reconnect_seconds": 8,
                "max_reopens": 1,
                "buffer_policy": "discard_before_reset",
            }
            if (
                type(restart) is not dict
                or set(restart) != set(fixed) | {"stable_usb_fingerprint"}
                or any(
                    type(restart[key]) is not type(value) or restart[key] != value
                    for key, value in fixed.items()
                )
                or plan.get("protocol") != "esp-idf-startup-log-v1"
                or plan.get("firmware_verification") != "unverified_device_report"
                or type(plan["observe_seconds"]) is not int
            ):
                raise ValueError("invalid_plan_response")
            _hex(restart["stable_usb_fingerprint"], 64)
        return cls(
            plan_hash,
            _hex(plan["broker_epoch"], 32),
            row.job_id,
            row.test,
            row.backend,
            _hex(data["review_revision"], 64),
            row.expires_at,
            expected,
            rendered,
            log_recipient,
            row.execution_submitted,
            plan.get("profile_execution_policy", "inherit"),
        )


def review_warning(review: PlanReview) -> str:
    notice = EXECUTION_NOTICE if review.execution_submitted else APPROVAL_NOTICE
    if review.test == "restart_and_observe":
        return (
            RESTART_NOTICE + "This exact approval click permits the reset and report delivery "
            f"to requesting principal: {review.log_recipient}. "
            "No additional send approval is required. The bounded report is retained in RAM "
            "for at most five minutes. Firmware identity is device-reported and unverified; "
            "configured firmware hashes are not evidence of running firmware.\n\n" + notice
        )
    if review.test in {t.value for t in FLASH_TESTS}:
        return (
            "BACKUP enters ROM download mode and reads full plaintext flash into "
            "private local storage. "
            "FLASH/RESTORE write the exact displayed artifact/backup and erase affected sectors. "
            "Verify the registered device, build security policy and recovery "
            "backup before this exact approval. "
            "Reset and passive boot checks may exit the current app and lose unsaved state. "
            "Cancellation or failure does not undo partial writes; needs_review "
            "blocks further work. "
            f"Bounded redacted boot/progress logs go to principal: {review.log_recipient}. "
            "Boot text is unverified; BIN readback and ELF prefix are separate "
            "evidence. No automatic restore.\n\n" + notice
        )
    if review.test == "firmware_build":
        return (
            "BUILD executes the registered trusted PowerShell export profile and project "
            "build scripts as your OS user. This exact approval permits bounded build log "
            f"delivery to requesting principal: {review.log_recipient}. "
            "Output may contain private data; redaction cannot recognize every secret. "
            "Logs remain in RAM for five minutes after the latest output, capped at "
            "256 KiB / 2,048 lines per job. External ESP-IDF must be exactly 6.0.1. "
            f"Profile execution policy: {review.profile_execution_policy} "
            "(local registration only). No flash, USB, reset, install or version fallback.\n\n"
            + notice
        )
    if review.test == "firmware_observe":
        return (
            FIRMWARE_NOTICE + "This click permits report delivery to requesting principal: "
            f"{review.log_recipient}. "
            "No additional send approval is required. The bounded report is retained in RAM "
            "for at most five minutes. Firmware identity is device-reported and unverified; "
            "configured firmware hashes are not evidence of running firmware.\n\n" + notice
        )
    if review.test == "capture_home":
        notice += "\n\n" + CAPTURE_NOTICE
    if review.attestation == "none":
        return notice
    effects = (
        "CAPTURE requires the device already be at HOME. It sends one Esc to confirm HOME, "
        "then one s key and receives rendered pixels. In another screen Esc may exit an "
        "app/editor and lose unsaved state. Once image transmission is authorized, the requesting "
        "authenticated client may retrieve the image for five minutes and transmit it to its "
        "AI service. Pixels do not prove "
        "LCD transfer or frame coherence. "
        if review.attestation == "device_home_firmware"
        else "HELLO exits the current app and sends q/a/u/e navigation keys. "
    )
    return (
        effects + "Device opening/closing may reset it due to driver control-line glitches. "
        "Verify the configured firmware is running and other serial tools are closed.\n\n" + notice
    )


@dataclass(frozen=True)
class PlanOperation:
    sequence: int
    action: Action
    started_at: float
    plan_hash: str = ""
    review_revision: str = ""
    attestation: str = ""
    selection_revision: int = 0
    execution_submitted: bool = False


@dataclass(frozen=True)
class PlanOutcome:
    operation: PlanOperation
    listing: PlanList | None = None
    review: PlanReview | None = None
    approved: bool = False
    already_approved: bool = False
    failed: bool = False
    execution_submitted: bool = False
    job_state: str | None = None


def perform_plan(api: PlanAPI, operation: PlanOperation) -> PlanOutcome:
    try:
        if operation.action == "refresh":
            return PlanOutcome(operation, listing=PlanList.parse(api.plans(), operation.started_at))
        if operation.action == "inspect":
            review = PlanReview.parse(api.plan(operation.plan_hash), operation.started_at)
            if review.plan_hash != operation.plan_hash:
                raise ValueError("invalid_plan_response")
            return PlanOutcome(operation, review=review)
        response = api.approve(
            operation.plan_hash, operation.review_revision, operation.attestation
        )
        submitted, state = approval_result(
            response, operation.plan_hash, operation.execution_submitted
        )
        return PlanOutcome(
            operation,
            approved=True,
            already_approved=response["already_approved"],
            execution_submitted=submitted,
            job_state=state,
        )
    except Exception:
        # Never show server bodies, exception text, URL strings, or credentials.
        return PlanOutcome(operation, failed=True)


class PlanController:
    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self.clock = clock
        self.listing: PlanList | None = None
        self.review: PlanReview | None = None
        self.selected = ""
        self.selection_revision = 0
        self.sequence = 0
        self.pending: PlanOperation | None = None
        self.fresh = False
        self.review_valid = False
        self.closed = False
        self.message = "Refresh pending plans, select one, then inspect it."

    @property
    def busy(self) -> bool:
        return self.pending is not None

    def select(self, plan_hash: str) -> None:
        if self.selected != plan_hash:
            self.selected = plan_hash
            self.selection_revision += 1
            self.review = None
            self.review_valid = False

    def _begin(self, action: Action, **fields: Any) -> PlanOperation | None:
        if self.closed or self.busy:
            return None
        self.sequence += 1
        self.pending = PlanOperation(
            self.sequence,
            action,
            self.clock(),
            selection_revision=self.selection_revision,
            **fields,
        )
        self.message = {
            "refresh": "Loading pending plans...",
            "inspect": "Loading the exact plan for review...",
            "approve": "Approval pending. Closing this window will not cancel this request.",
        }[action]
        return self.pending

    def refresh(self) -> PlanOperation | None:
        operation = self._begin("refresh")
        if operation is not None:
            self.fresh = self.review_valid = False
        return operation

    def selected_row(self) -> PlanRow | None:
        if self.listing is None:
            return None
        return next((row for row in self.listing.plans if row.plan_hash == self.selected), None)

    def can_inspect(self) -> bool:
        row = self.selected_row()
        return bool(
            not self.closed
            and not self.busy
            and self.fresh
            and row is not None
            and self.clock() < row.expires_at
        )

    def inspect(self) -> PlanOperation | None:
        if not self.can_inspect():
            return None
        self.review_valid = False
        return self._begin("inspect", plan_hash=self.selected)

    def can_approve(self, attestation: str = "") -> bool:
        review = self.review
        return bool(
            not self.closed
            and not self.busy
            and self.fresh
            and self.review_valid
            and review is not None
            and self.selected == review.plan_hash
            and self.clock() < review.expires_at
            and attestation == ATTESTATIONS[review.attestation]
        )

    def approve(
        self, plan_hash: str, review_revision: str, attestation: str
    ) -> PlanOperation | None:
        review = self.review
        if (
            not self.can_approve(attestation)
            or review is None
            or review.plan_hash != plan_hash
            or review.review_revision != review_revision
        ):
            return None
        operation = self._begin(
            "approve",
            plan_hash=plan_hash,
            review_revision=review_revision,
            attestation=attestation,
            execution_submitted=review.execution_submitted,
        )
        self.review_valid = False
        return operation

    def accept(self, outcome: PlanOutcome) -> bool:
        operation = outcome.operation
        if self.closed or self.pending != operation:
            return False
        self.pending = None
        if outcome.failed:
            self.fresh = self.review_valid = False
            self.message = APPROVAL_UNCERTAIN if operation.action == "approve" else REQUEST_FAILED
        elif outcome.listing is not None:
            self.listing = outcome.listing
            self.fresh = True
            self.review_valid = False
            self.message = (
                "Showing up to 64 pending plans; more are available. Select one and inspect it."
                if self.listing.has_more
                else "Select one pending plan and inspect it."
            )
            if not self.listing.plans:
                self.message = "No pending plans in the current broker lease."
        elif outcome.review is not None:
            review = outcome.review
            row = self.selected_row()
            if (
                self.selection_revision != operation.selection_revision
                or row is None
                or self.listing is None
                or review.plan_hash != self.selected
                or review.broker_epoch != self.listing.broker_epoch
                or (review.job_id, review.test, review.backend)
                != (row.job_id, row.test, row.backend)
            ):
                self.review_valid = False
                self.message = "The selection changed. Inspect the selected plan again."
            else:
                self.review = review
                self.review_valid = self.clock() < review.expires_at
                self.message = (
                    "Review the full reset and report-delivery plan. Explicit approval of this "
                    "exact plan is required; no automatic policy approval is allowed."
                    if self.review_valid and review.test == "restart_and_observe"
                    else "Review the full plan. If an operator policy does not permit it, "
                    "explicitly approve this exact plan."
                    if self.review_valid
                    else "Review expired. Refresh and inspect the plan again."
                )
        elif outcome.approved:
            self.fresh = self.review_valid = False
            state = "was already approved" if outcome.already_approved else "approved"
            self.message = (
                EXECUTION_QUEUED
                if outcome.execution_submitted and outcome.job_state == "queued"
                else f"Approved. Submitted job state: {outcome.job_state}."
                if outcome.execution_submitted
                else f"Plan {operation.plan_hash} {state}. {APPROVAL_NOTICE}"
            )
        return True

    def close(self) -> bool:
        if self.busy:
            self.message = PENDING_CLOSE
            return False
        self.closed = True
        self.fresh = self.review_valid = False
        return True


class PlanWorker:
    def __init__(self, api: PlanAPI) -> None:
        self.api = api
        self.commands: queue.Queue[PlanOperation | None] = queue.Queue()
        self.results: queue.Queue[PlanOutcome] = queue.Queue()
        self.stopped = threading.Event()
        self.thread = threading.Thread(target=self.run, name="cardputer-plan-approval", daemon=True)
        self.thread.start()

    def run(self) -> None:
        while not self.stopped.is_set():
            operation = self.commands.get()
            if operation is None or self.stopped.is_set():
                return
            self.results.put(perform_plan(self.api, operation))

    def stop(self) -> None:
        self.stopped.set()
        self.commands.put(None)


class PlanApprovalPanel:
    def __init__(self, root: tk.Tk, api: PlanAPI) -> None:
        self.root = root
        self.controller = PlanController()
        self.worker = PlanWorker(api)
        self.dialog: tk.Toplevel | None = None
        self.updating = False
        self.rendered_listing: PlanList | None = None
        self.rendered_review: PlanReview | None = None
        root.after(100, self._drain)

    def show(self) -> None:
        import tkinter as tk
        from tkinter import ttk

        if self.controller.closed:
            return
        if self.dialog is not None:
            self.dialog.deiconify()
            self.dialog.lift()
            if not self.controller.busy:
                self._refresh()
            return
        self.dialog = dialog = tk.Toplevel(self.root)
        dialog.title("Cardputer Plan Approval")
        dialog.transient(self.root)
        dialog.geometry("1050x850")
        dialog.minsize(760, 640)
        dialog.protocol("WM_DELETE_WINDOW", self._dismiss)
        outer = ttk.Frame(dialog, padding=14)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(3, weight=1)
        ttk.Label(
            outer,
            text="Inspect the exact plan and its approval effect before approving.",
            wraplength=980,
        ).grid(row=0, sticky="w")
        columns = ("job", "test", "backend", "hash")
        plan_list = ttk.Frame(outer)
        plan_list.grid(row=1, column=0, sticky="ew", pady=(10, 6))
        plan_list.columnconfigure(0, weight=1)
        self.tree = ttk.Treeview(
            plan_list, columns=columns, show="headings", selectmode="browse", height=5
        )
        for name, label, width in (
            ("job", "Job", 150),
            ("test", "Test", 130),
            ("backend", "Backend", 70),
            ("hash", "Plan SHA-256", 520),
        ):
            self.tree.heading(name, text=label)
            self.tree.column(name, width=width, minwidth=50)
        self.tree.grid(row=0, column=0, sticky="ew")
        list_scroll = ttk.Scrollbar(plan_list, orient="vertical", command=self.tree.yview)
        list_scroll.grid(row=0, column=1, sticky="ns")
        list_horizontal = ttk.Scrollbar(plan_list, orient="horizontal", command=self.tree.xview)
        list_horizontal.grid(row=1, column=0, sticky="ew")
        self.tree.configure(yscrollcommand=list_scroll.set, xscrollcommand=list_horizontal.set)
        self.tree.bind("<<TreeviewSelect>>", self._selection)
        bar = ttk.Frame(outer)
        bar.grid(row=2, column=0, sticky="ew", pady=(0, 8))
        self.refresh_button = ttk.Button(bar, text="Refresh plans", command=self._refresh)
        self.refresh_button.pack(side="left")
        self.inspect_button = ttk.Button(bar, text="Inspect selected plan", command=self._inspect)
        self.inspect_button.pack(side="left", padx=8)
        self.lease_var = tk.StringVar()
        ttk.Label(bar, textvariable=self.lease_var).pack(side="left", padx=8)
        review_box = ttk.LabelFrame(outer, text="Full immutable plan (JSON escaped)", padding=6)
        review_box.grid(row=3, column=0, sticky="nsew")
        review_box.rowconfigure(0, weight=1)
        review_box.columnconfigure(0, weight=1)
        self.plan_text = tk.Text(review_box, wrap="none", height=14, state="disabled")
        self.plan_text.grid(row=0, column=0, sticky="nsew")
        vertical = ttk.Scrollbar(review_box, orient="vertical", command=self.plan_text.yview)
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal = ttk.Scrollbar(review_box, orient="horizontal", command=self.plan_text.xview)
        horizontal.grid(row=1, column=0, sticky="ew")
        self.plan_text.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        self.warning_var = tk.StringVar(value=APPROVAL_NOTICE)
        ttk.Label(outer, textvariable=self.warning_var, wraplength=980).grid(
            row=4, column=0, sticky="w", pady=8
        )
        self.attestation_label = tk.StringVar()
        ttk.Label(outer, textvariable=self.attestation_label, wraplength=980).grid(
            row=5, sticky="w"
        )
        self.attestation_var = tk.StringVar()
        self.attestation_var.trace_add("write", self._attestation_changed)
        self.attestation_entry = ttk.Entry(outer, textvariable=self.attestation_var)
        self.attestation_entry.grid(row=6, sticky="ew", pady=(3, 8))
        self.status_var = tk.StringVar()
        ttk.Label(outer, textvariable=self.status_var, wraplength=980).grid(
            row=7, sticky="w", pady=6
        )
        actions = ttk.Frame(outer)
        actions.grid(row=8, sticky="e")
        self.approve_button = ttk.Button(
            actions, text="Approve selected plan", command=self._approve
        )
        self.approve_button.pack(side="left", padx=8)
        ttk.Button(actions, text="Close", command=self._dismiss).pack(side="left")
        self.rendered_listing = self.rendered_review = None
        self._render()
        self._refresh()

    def _submit(self, operation: PlanOperation | None) -> None:
        if operation is not None:
            self.worker.commands.put(operation)
        self._render()

    def _refresh(self) -> None:
        self._submit(self.controller.refresh())

    def _inspect(self) -> None:
        self._submit(self.controller.inspect())

    def _selection(self, _event: Any = None) -> None:
        if self.updating:
            return
        selected = self.tree.selection()
        self.controller.select(selected[0] if selected else "")
        self._render()

    def _attestation_changed(self, *_args: str) -> None:
        if not self.updating:
            self._controls()

    def _approve(self) -> None:
        review = self.rendered_review
        attestation = self.attestation_var.get()
        if (
            review is None
            or review is not self.controller.review
            or not self.controller.can_approve(attestation)
        ):
            return
        # This click confirms the displayed immutable review and its visible effects.
        # The controller rechecks the exact target, revision, attestation and lease,
        # and marks it pending before the worker can receive a second click.
        self._submit(self.controller.approve(review.plan_hash, review.review_revision, attestation))

    def _controls(self) -> None:
        if self.dialog is None:
            return
        model = self.controller
        self.refresh_button.configure(state="disabled" if model.busy else "normal")
        self.inspect_button.configure(state="normal" if model.can_inspect() else "disabled")
        review = model.review
        self.approve_button.configure(
            state="normal" if model.can_approve(self.attestation_var.get()) else "disabled",
            text=approval_button_text(review),
        )
        seconds = max(0, math.floor(review.expires_at - model.clock())) if review else None
        self.lease_var.set(f"Review lease: {seconds} s" if seconds is not None else "")
        expired = review is not None and model.clock() >= review.expires_at
        self.status_var.set(
            "Review expired. Refresh and inspect the plan again."
            if expired and model.review_valid and not model.busy
            else model.message
        )

    def _render(self) -> None:
        if self.dialog is None:
            return
        model = self.controller
        self.updating = True
        try:
            if model.listing is not self.rendered_listing:
                self.rendered_listing = model.listing
                children = self.tree.get_children()
                if children:
                    self.tree.delete(*children)
                for row in model.listing.plans if model.listing else ():
                    self.tree.insert(
                        "",
                        "end",
                        iid=row.plan_hash,
                        values=(row.job_id, row.test, row.backend, row.plan_hash),
                    )
                if model.selected and self.tree.exists(model.selected):
                    self.tree.selection_set(model.selected)
            if model.review is not self.rendered_review or model.review is None:
                self.rendered_review = model.review
                review = model.review
                self.plan_text.configure(state="normal")
                self.plan_text.delete("1.0", "end")
                self.plan_text.insert(
                    "1.0", review.json_text if review else "Select and inspect a plan."
                )
                self.plan_text.configure(state="disabled")
                self.warning_var.set(review_warning(review) if review else APPROVAL_NOTICE)
                phrase = ATTESTATIONS[review.attestation] if review else ""
                self.attestation_label.set(
                    f"Type exactly: {phrase}" if phrase else "No device attestation required."
                )
                self.attestation_var.set("")
                self.attestation_entry.configure(state="normal" if phrase else "disabled")
        finally:
            self.updating = False
        self._controls()

    def _drain(self) -> None:
        if self.worker.stopped.is_set():
            return
        try:
            while True:
                result = self.worker.results.get_nowait()
                if self.controller.accept(result):
                    self._render()
        except queue.Empty:
            pass
        self._controls()
        self.root.after(100, self._drain)

    def can_close_window(self) -> bool:
        if self.controller.busy:
            self.controller.message = PENDING_CLOSE
            self.show()
            self._render()
            return False
        return True

    def _dismiss(self) -> None:
        if self.dialog is not None and self.can_close_window():
            self.controller.review_valid = False
            self.dialog.destroy()
            self.dialog = None

    def stop(self) -> bool:
        if not self.controller.close():
            self._render()
            return False
        self.worker.stop()
        return True
