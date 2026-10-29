"""Single resident owner: independent of MCP connections and client lifetimes."""

import asyncio
import base64
import hashlib
import json
import math
import os
import re
import secrets
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

from filelock import FileLock, Timeout

from .app_models import AppCommandRequest, AppEventsRequest
from .app_sessions import AppSessions
from .backend import Backend, BackendResult, DeviceDisconnected, DeviceOperationFailure, MockBackend
from .build_locations import BuildLocationError
from .cancellation import DeviceStop, device_stop
from .capture import CapturedImage
from .capture_store import CaptureStore
from .collector_binding import prepare_collector
from .config import Settings
from .continuation import (
    HOLD_SECONDS,
    TICKET_TTL_SECONDS,
    Deferred,
    Hold,
    TicketError,
    Tickets,
)
from .derby_provenance import capture_provenance, finish_provenance
from .derby_reports import PROVENANCE_KEY
from .device_failure_outcome import FailureEvidence
from .device_failure_outcome import classify as classify_failure
from .device_observation import DeviceObservation
from .execution_authority import Checkpoint, ExecutionAuthority, Gate
from .firmware_build import BuildPreflightFailure
from .flash_binding import FlashBinding, prepare_binding, validate_binding
from .flash_lifecycle import FlashFailure, _bounded_read_diagnostic
from .flash_manifest import FlashAdmissionError
from .job_kinds import AdmittedJob, kind_for, sync_section
from .job_logs import DeviceLogTap, JobLog, JobLogStore
from .lease_manager import LeaseManager, ReleaseRefused
from .lease_manager import Session as Session  # re-exported: callers import it from here
from .logs import Event, LogBook
from .models import (
    FLASH_TESTS,
    MODULE_LOG_TESTS,
    AcquireRequest,
    CancelDeviceRequest,
    CancelRequest,
    CloseWorkSessionRequest,
    CreateWorkSessionRequest,
    ExecuteRequest,
    LeaseRequest,
    LogRequest,
    PrepareRequest,
    RenewRequest,
    RequestDeviceRequest,
    ResumeWorkSessionRequest,
    TestName,
)
from .offload_io import IOMemo, complete, offload
from .operator_notifications import OperatorNotifications
from .resource_recheck import uncertainty
from .source_snapshot import SourceError
from .store import Store

# Longest a pre-requested job may wait for the device once its parent has finished.
DEVICE_WAIT_SECONDS = 1800.0


SERIAL_PORT_OPEN_MEANING = (
    "The broker opens the serial port only while a job runs; false while idle is normal "
    "and does not mean the device is unplugged. hardware_connected is the same value."
)
USB_PRESENCE_MEANING = (
    "present: exactly one USB device matches the registered selector; missing: none; "
    "ambiguous: more than one. Enumeration only; it does not prove the firmware responds."
)

# Device jobs whose serial bytes the backend tees into the durable job log.
_NON_DEVICE_LOG_TESTS = frozenset(test.value for test in MODULE_LOG_TESTS)
_TERMINAL_LOG_STATES = frozenset({"succeeded", "failed", "needs_review", "cancelled"})


class BrokerError(ValueError):
    """Public, fixed-string errors. Do not include exceptions or request content."""


class RecoveryPointRefusal(BrokerError):
    """A fixed recovery-point refusal code plus details the public gate re-validates."""

    def __init__(self, code: str, details: dict[str, Any] | None) -> None:
        super().__init__(code)
        self.details = details


@dataclass
class WorkSession:
    resume_token: str = field(repr=False)
    id: str
    owner: str
    principal_id: str
    scopes: frozenset[str]
    deadline: float
    state: str = "open"
    request_id: str | None = None


@dataclass
class DeviceWait:
    id: str
    session_id: str
    lease_seconds: int
    deadline: float
    state: str = "waiting"
    lease_id: str | None = None
    ticket_id: str | None = None
    job_id: str | None = None
    principal_id: str | None = None
    origin: str | None = None


@dataclass(frozen=True, repr=False)
class ExecutionIntent:
    """Process-local request to run exactly one plan from an explicit authenticated request."""

    epoch: str
    plan_hash: str
    job_id: str
    lease_id: str
    principal_id: str | None
    owner: str
    work_session_id: str | None
    deadline: float
    lease_ref: Session | None = None
    work_ref: WorkSession | None = None


@dataclass(frozen=True)
class RetainedCapture:
    principal_id: str = field(repr=False)
    image: CapturedImage = field(repr=False)
    deadline: float
    origin: str
    born_utc: str
    delivery_state: str = "available"
    policy_target: str = ""


@dataclass(frozen=True, repr=False)
class RetainedDeviceObservation:
    principal_id: str
    origin: str
    observation: DeviceObservation
    deadline: float


def digest_plan(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(canonical).hexdigest()


def device_lock_path(backend: str = "mock") -> Path:
    # Stable across state directories, worktrees and localhost ports for this OS user.
    # There is ONE synthetic device. A real backend will need a physical-identity key.
    if hasattr(os, "getuid"):
        identity = str(os.getuid())
    else:  # Windows: query the OS, not mutable USERNAME/HOME environment variables.
        import ctypes
        from ctypes import wintypes

        size = wintypes.DWORD(257)
        buffer = ctypes.create_unicode_buffer(size.value)
        windows_api = cast(Any, ctypes).windll
        if not windows_api.advapi32.GetUserNameW(buffer, ctypes.byref(size)):
            raise BrokerError("cannot_resolve_os_user")
        identity = buffer.value
    user_key = hashlib.sha256(identity.encode()).hexdigest()[:16]
    directory = Path(tempfile.gettempdir()) / f"cardputer-mcp-{user_key}"
    directory.mkdir(mode=0o700, exist_ok=True)
    if directory.is_symlink():
        raise BrokerError("unsafe_lock_directory")
    return directory / f"{backend}-device.lockfile"


class Broker:
    def __init__(
        self,
        settings: Settings,
        *,
        backend: Backend | None = None,
        clock: Callable[[], float] = time.monotonic,
        lock_path: Path | None = None,
        serial_enabled: bool = False,
        principal_valid: Callable[[str, str, frozenset[str]], bool] | None = None,
    ) -> None:
        self.settings = settings
        if settings.backend == "serial" and not serial_enabled:
            raise BrokerError("serial_runtime_opt_in_required")
        if backend is not None:
            self.backend = backend
        elif settings.backend == "serial":
            from .serial_backend import SerialBackend

            assert settings.serial is not None
            self.backend = SerialBackend(settings.serial)
        else:
            self.backend = MockBackend(settings.mock_stage_seconds)
        if self.backend.name != settings.backend:
            raise BrokerError("backend_configuration_mismatch")
        self.clock = clock
        self.epoch = uuid4().hex
        # The only writer of the device lease and its deadline (jobs.md section 3).
        self.leases = LeaseManager(lambda: self._physical_busy())
        self.principal_valid = principal_valid
        # The pre-run / pre-write checks of every checkpoint (stage 3b).
        self.authority = ExecutionAuthority(self)
        # Rotation lineage (recorded, current) -> bool; None means exact principals only.
        self.principal_lineage: Callable[[str, str], bool] | None = None
        self.app_sessions = AppSessions()
        self.app_sessions.installed_elf = self._installed_elf
        self.work_sessions: dict[str, WorkSession] = {}
        self.device_requests: dict[str, DeviceWait] = {}
        self.waiters: list[str] = []
        self.hold: Hold | None = None
        self.tickets = Tickets(self._store, lambda: self.epoch)
        self.deferred = Deferred(self._store, lambda: self.epoch)
        self._queue_sequence = 0
        self.maintenance: asyncio.Task[None] | None = None
        self.active_job: str | None = None
        self.operator_changes = OperatorNotifications()
        self.logs = LogBook(settings.log_capacity, on_change=self.operator_changes.notify)
        self.job_logs = JobLogStore(settings.state_dir / "job-logs")
        self.capture_store = CaptureStore(settings.state_dir / "captures")
        self._job_log_failures: set[str] = set()
        from .firmware_build import FirmwareBuild

        self.firmware_build = FirmwareBuild(settings, settings.state_dir / "history.sqlite3")
        from .firmware_source import FirmwareSource

        self.firmware_source = FirmwareSource(settings, self._store, clock=lambda: self.clock())
        from .profile_jobs import ProfileJobs

        self.profile_jobs = ProfileJobs(self)
        from .firmware_flash import FirmwareFlash

        self.firmware_flash = FirmwareFlash(
            settings, self.backend, self.firmware_build, self._store
        )
        self.firmware_flash.same_owner = self.same_owner
        self.firmware_build.on_logs_change = self.operator_changes.notify_logs
        for module in (self.firmware_build, self.firmware_source, self.firmware_flash):
            module.open_job_log = self._open_job_log
            module.job_logs = self.job_logs
        self.firmware_source.on_logs_change = self.operator_changes.notify_logs
        self.firmware_flash.on_logs_change = self.operator_changes.notify_logs
        self.app_sessions.on_logs_change = self.operator_changes.notify_logs
        self.queue: asyncio.Queue[str | None] = asyncio.Queue(settings.max_queue)
        self.worker: asyncio.Task[None] | None = None
        self.store: Store | None = None
        self.closing = False
        self.execution_uncertain = False
        # RAM only: the fixed reason the latch was set, for status and re-check.
        self.execution_uncertain_reason: str | None = None
        # The job the device worker is processing right now, from dequeue until its
        # final cleanup. `active_job` can stay set after an unconfirmed close.
        self._worker_job: str | None = None
        self.captures: dict[str, RetainedCapture] = {}
        self.device_observations: dict[str, RetainedDeviceObservation] = {}
        self._flash_bindings: dict[str, FlashBinding] = {}
        # One offloaded firmware verification at a time: the source-cache guard
        # refuses (never waits for) a concurrent user, as the loop once serialized.
        self._file_verification = asyncio.Lock()
        self._execution_bindings: dict[str, str] = {}
        self._execution_intents: dict[str, ExecutionIntent] = {}
        self._device_lock = FileLock(
            str(lock_path or device_lock_path(settings.backend)), timeout=0
        )
        self._state_lock = FileLock(str(settings.state_dir / "broker.lockfile"), timeout=0)
        from .home_recovery import HomeRecovery

        self.home_recovery = HomeRecovery(self)

    def same_owner(self, recorded: object, current: object) -> bool:
        """Whether inputs recorded under `recorded` (a build or backup) may feed
        a new job of `current`.

        Exact equality keeps the previous behavior. Otherwise only the injected
        rotation lineage can link two different principals; it grants no
        authority of its own (the current principal is authenticated and its
        scope/lease checked elsewhere). Synchronous: no await is introduced
        into admission. Any callback failure fails closed.
        """
        if recorded == current:
            return True
        if type(recorded) is not str or type(current) is not str or not recorded or not current:
            return False
        lineage = self.principal_lineage
        if lineage is None:
            return False
        try:
            return lineage(recorded, current) is True
        except Exception:
            return False

    def _installed_elf(self) -> str | None:
        """ELF digest of the verified flash that is current on the serial device.

        Flash selects the receipt only after readback and same-device boot, so its plan
        names the image the device reports; the configured digest may be an older BIN pin.
        """
        serial = self.settings.serial
        if serial is None:
            return None
        store = self._store()
        for device_id, registration in self.settings.flash_devices.items():
            if registration.selector != serial.selector:
                continue
            current = store.firmware_current(device_id)
            job = store.job(current["completed_job"]) if current else None
            plan = store.plan(job["plan_hash"]) if job else None
            elf = plan.get("expected_elf_sha256") if plan else None
            if (
                current is not None
                and current["kind"] == "artifact_bundle"
                and type(elf) is str
                and re.fullmatch(r"[a-f0-9]{64}", elf)
            ):
                return elf
        return None

    def _store(self) -> Store:
        if self.store is None:
            raise BrokerError("broker_not_running")
        return self.store

    async def start(self) -> None:
        self.settings.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            self._device_lock.acquire()
            self._state_lock.acquire()
        except Timeout as exc:
            self._device_lock.release()
            raise BrokerError("broker_already_running") from exc
        try:
            self.store = Store(
                self.settings.state_dir / "history.sqlite3",
                on_commit=self.operator_changes.notify,
            )
            if os.name != "nt":
                os.chmod(self.settings.state_dir / "history.sqlite3", 0o600)
            self.store.ledger.on_refused = partial(self.logs.append, Event.TRANSITION_REFUSED)
            self.store.recover()
            self.tickets.void_stale()
            self.deferred.cancel_stale()
            self.logs.append(Event.STARTED)
            self.worker = asyncio.create_task(self._work(), name="single-device-worker")
            self.profile_jobs.start()
            self.maintenance = asyncio.create_task(
                self._maintain(), name="single-device-maintenance"
            )
        except BaseException:
            if self.store:
                self.store.close()
                self.store = None
            self._state_lock.release()
            self._device_lock.release()
            raise

    async def close(self) -> None:
        if self.store is None:
            return
        self.closing = True
        # Wake operator event streams so they end instead of holding shutdown open.
        self.operator_changes.notify()
        for app_run in self.app_sessions.jobs.values():
            app_run.cancelled.set()
        # Release completed private output before any service shutdown can yield.
        # Denying new reads alone must not leave these bytes retained in RAM.
        self.captures.clear()
        self.device_observations.clear()
        await self.home_recovery.close()
        await self.profile_jobs.close()
        self.firmware_build.request_stop()
        self.firmware_flash.request_stop()
        self._flash_bindings.clear()
        self._execution_bindings.clear()
        if self.maintenance:
            self.maintenance.cancel()
            try:
                await self.maintenance
            except asyncio.CancelledError:
                pass
            self.maintenance = None
        for request_id in tuple(self.waiters):
            self._end_wait(self.device_requests[request_id], "cancelled", "broker_shutdown")
        self._cancel_queued("broker_shutdown")
        if self.worker:
            # Never cancel the device task or release its lock during an active workflow.
            await self.queue.put(None)
            await asyncio.shield(self.worker)
        if (
            self._physical_busy()
            or self.firmware_build.process.uncertain
            or self.profile_jobs.uncertain
        ):
            # An unconfirmed serial close must not advertise a free device or
            # release the broker lock. Only OS process teardown ends this hold.
            raise BrokerError("device_close_unconfirmed")
        if self.session:
            self._end_ownership("released", "broker_shutdown")
        for work in self.work_sessions.values():
            if work.state == "open":
                work.state = "closed"
                self._store().set_work_session(work.id, "closed", "broker_shutdown")
        self.captures.clear()
        self.device_observations.clear()
        self.logs.append(Event.STOPPED)
        self.job_logs.close()
        self.store.close()
        self.store = None
        self.app_sessions.jobs.clear()
        self._state_lock.release()
        self._device_lock.release()

    def _cancel_queued(self, reason: str) -> None:
        store = self._store()
        for intent in tuple(self._execution_intents.values()):
            self._cancel_execution_intent(intent, reason)
        stopping = False
        while not self.queue.empty():
            job_id = self.queue.get_nowait()
            self.queue.task_done()
            if job_id is None:
                stopping = True
            else:
                job = store.job(job_id)
                if job and job["state"] == "queued":
                    cancelled = store.ledger.transition(
                        job_id, "cancelled", reason, expect={"queued"}
                    )
                    self._flash_bindings.pop(job_id, None)
                    self._execution_bindings.pop(job_id, None)
                    if cancelled:
                        self.logs.append(Event.CANCELLED)
        if stopping:
            self.queue.put_nowait(None)

    def _latch_execution(self, reason: str) -> None:
        self.execution_uncertain = True
        if self.execution_uncertain_reason is None:
            self.execution_uncertain_reason = reason

    def _record_review(
        self, job_id: str, result: str, diagnostic: dict[str, Any] | None = None
    ) -> None:
        """Move a running job to needs_review; never overwrite a committed outcome.

        If the row already left `running`, the ledger refuses and records the write
        (DESIGN-REDESIGN 2.1). The review it would have required becomes the runtime
        latch, so the lane admits nothing more until restart and review.
        """
        if not self._store().ledger.transition(job_id, "needs_review", result, diagnostic):
            self._latch_execution("job_review_write_refused")

    def _record_failure(
        self,
        job_id: str,
        result: str,
        diagnostic: dict[str, Any] | None,
        evidence: FailureEvidence,
    ) -> None:
        """End a running device job that raised a failure, by classification.

        `failed` (device state established: close confirmed, no write) keeps the
        result code and leaves other queued jobs alone. Everything else is the
        review barrier: `needs_review` plus cancelling the queue, as before.
        See device_failure_outcome.py for the table.
        """
        if classify_failure(evidence) == "failed":
            if self._store().ledger.transition(job_id, "failed", result, diagnostic):
                self.logs.append(Event.FAILED)
                return
            # The row already left `running`; treat it like a refused review write.
            self._latch_execution("job_failure_write_refused")
        else:
            self._record_review(job_id, result, diagnostic)
        self.logs.append(Event.REVIEW)
        self._cancel_queued("recovery_review_required")

    def _failure_evidence(self, plan: dict[str, Any], job_id: str, code: str) -> FailureEvidence:
        """Observed close/write evidence for a typed failure of the running job."""
        backend = self.backend
        try:
            serial_closed = not (
                backend.active
                or backend.hardware_connected
                or getattr(backend, "ownership_uncertain", False)
            )
        except Exception:
            serial_closed = False
        test = plan.get("test")
        if test in {item.value for item in FLASH_TESTS}:
            write_started, flash_closed = self.firmware_flash.failure_evidence(job_id)
            closed = serial_closed and flash_closed
        else:
            # Serial jobs never call a flash write; the mock workflow models one.
            write_started, closed = test == TestName.WORKFLOW.value, serial_closed
        from .home_recovery import CAPTURE_FAILURES

        return FailureEvidence(
            code=code,
            known=True,
            close_confirmed=closed,
            write_possibly_started=write_started,
            home_recovery_source=test == TestName.DERBY.value and code in CAPTURE_FAILURES,
        )

    def _cancel_execution_intent(self, intent: ExecutionIntent, reason: str) -> None:
        job = self._store().job(intent.job_id)
        if (
            job
            and job["plan_hash"] == intent.plan_hash
            and job["state"] in ("awaiting_approval", "queued")
        ):
            # Legacy awaiting_approval rows are cleared at startup and are not in the
            # transition table; only a queued row is cancelled here.
            if self._store().ledger.transition(
                intent.job_id, "cancelled", reason, expect={"queued"}
            ):
                self.logs.append(Event.CANCELLED)
        self._execution_intents.pop(intent.job_id, None)
        self._execution_bindings.pop(intent.job_id, None)
        self._flash_bindings.pop(intent.job_id, None)

    def _execution_intent_binding_live(self, intent: ExecutionIntent) -> bool:
        """Final callback-free identity/deadline checks; never extend an intent."""
        now = self.clock()
        return bool(
            math.isfinite(now)
            and math.isfinite(intent.deadline)
            and now < intent.deadline
            and self._execution_lease_binding_live(intent)
        )

    def _execution_lease_binding_live(
        self, intent: ExecutionIntent, *, running: bool = False
    ) -> bool:
        """Current lease authority, independent of the fixed admission deadline.

        `running=True` is used only for a job that was already admitted and is
        running under this lease. Such a job keeps its lease until it finishes
        (DESIGN-CONTINUATION-TICKETS.md section 5.3): the lease and work-session
        clocks no longer revoke it. A job-only lease returns no capability, so it
        could never be renewed. Revocation (`Session.revoked`, set when the principal
        or work session loses authority), closing, epoch and identity changes still
        stop it. Admission (`running=False`) keeps every clock check.
        """
        lease, now = self.session, self.clock()
        if (
            self.closing
            or not math.isfinite(now)
            or intent.epoch != self.epoch
            or lease is None
            or not math.isfinite(lease.deadline)
            or (lease.revoked if running else now >= lease.deadline)
            or (intent.lease_ref is not None and lease is not intent.lease_ref)
            or lease.id != intent.lease_id
            or lease.principal_id != intent.principal_id
            or lease.owner != intent.owner
            or lease.work_session_id != intent.work_session_id
        ):
            return False
        if intent.work_session_id is not None:
            work = self.work_sessions.get(intent.work_session_id)
            if (
                work is None
                or work is not intent.work_ref
                or work.state not in (("open", "expired") if running else ("open",))
                or not math.isfinite(work.deadline)
                or (not running and now >= work.deadline)
                or work.principal_id != intent.principal_id
                or work.owner != intent.owner
                or not {"read", "control"} <= work.scopes
            ):
                return False
        return True

    def _execution_intent_live(self, intent: ExecutionIntent) -> bool:
        if not self._execution_intent_binding_live(intent):
            return False
        job = self._store().job(intent.job_id)
        try:
            plan = self._store().plan(intent.plan_hash)
        except (TypeError, ValueError):
            return False
        return bool(
            self._execution_intents.get(intent.job_id) is intent
            and job is not None
            and job["state"] in ("awaiting_approval", "queued")
            and job["plan_hash"] == intent.plan_hash
            and plan is not None
            and digest_plan(plan) == intent.plan_hash
            and plan.get("job_id") == intent.job_id
            and plan.get("broker_epoch") == intent.epoch
            and plan.get("session_id") == intent.lease_id
            and plan.get("backend") == self.backend.name
            and plan.get("device") == self.backend.device
        )

    def _running_job_binding_live(
        self, plan: dict[str, Any], intent: ExecutionIntent, *, committing: bool = False
    ) -> bool:
        """Recheck the running job itself after callbacks, including atomic commit."""
        try:
            store = self._store()
            row = store.db.execute(
                "SELECT plan_hash,state,result FROM jobs WHERE id=?", (intent.job_id,)
            ).fetchone()
            if (
                row is None
                or row["plan_hash"] != intent.plan_hash
                or self.active_job != intent.job_id
                or plan.get("job_id") != intent.job_id
                or digest_plan(plan) != intent.plan_hash
                or store.plan(intent.plan_hash) != plan
                # A peer build's still-owned normal teardown is not revoked
                # device authority. Admission and failed cleanup remain gated.
                or self._execution_uncertain(allow_profile_cleanup=True)
                or not self._execution_lease_binding_live(intent, running=True)
                or not self._device_lock.is_locked
                or not self._state_lock.is_locked
                or (committing and not store.db.in_transaction)
            ):
                return False
            return bool(
                row["state"] == "running"
                or (
                    committing
                    and plan.get("test") in {test.value for test in FLASH_TESTS}
                    and row["state"] == "succeeded"
                    and row["result"] == f"firmware_{plan['flash_operation']}_completed"
                )
            )
        except Exception:
            return False

    def pending_execution(self, plan_hash: str) -> ExecutionIntent | None:
        """A bounded, callback-free lookup for local approval/review only."""
        for intent in self._execution_intents.values():
            if intent.plan_hash == plan_hash and self._execution_intent_live(intent):
                job = self._store().job(intent.job_id)
                if job is not None and job["state"] == "awaiting_approval":
                    return intent
        return None

    def _prune_execution_intents(self) -> None:
        for intent in tuple(self._execution_intents.values()):
            if not self._execution_intent_live(intent):
                reason = (
                    "execution_intent_expired"
                    if self.clock() >= intent.deadline
                    else "execution_intent_not_available"
                )
                self._cancel_execution_intent(intent, reason)

    def _execution_capacity_full(self) -> bool:
        awaiting = sum(
            bool((job := self._store().job(intent.job_id)) and job["state"] == "awaiting_approval")
            for intent in self._execution_intents.values()
        )
        return self.queue.qsize() + awaiting >= self.settings.max_queue

    def _execution_uncertain(self, *, allow_profile_cleanup: bool = False) -> bool:
        """Current resource/state uncertainty, never historical job outcomes.

        Active work is handled separately by the owner/worker queue. These
        latches survive completed callbacks until teardown; an offline review
        cannot clear them. A fresh broker still performs all resource preflights.
        """
        return bool(
            self.execution_uncertain
            or getattr(self.backend, "ownership_uncertain", False)
            or self.firmware_flash.uncertain
            or self.profile_jobs._uncertain(allow_owned_cleanup=allow_profile_cleanup)
        )

    def _physical_busy(self) -> bool:
        return bool(
            self.active_job is not None
            or self.backend.hardware_connected
            or self.backend.active
            or (self.profile_jobs.active is None and self.firmware_source.active)
            or (self.profile_jobs.active is None and self.firmware_build.process.active)
            or self.firmware_build.process.uncertain
            or self.firmware_build.output_uncertain
            or self.profile_jobs.uncertain
            or self.firmware_flash.active
            or self.firmware_flash.uncertain
            or getattr(self.backend, "ownership_uncertain", False)
        )

    def _principal_live(self, work: WorkSession) -> bool:
        # Cached request authentication does not authorize a later FIFO grant.
        if self.principal_valid is None:
            return False
        try:
            return (
                self.principal_valid(work.principal_id, work.owner, frozenset({"read", "control"}))
                is True
            )
        except Exception:
            return False

    def _end_wait(self, request: DeviceWait, state: str, reason: str) -> None:
        self._store().set_request(request.id, state, reason)
        request.state = state
        if request.id in self.waiters:
            self.waiters.remove(request.id)
            self._queue_sequence += 1
            self.operator_changes.notify()

    @property
    def session(self) -> Session | None:
        """The current device lease; `self.leases` is its only writer."""
        return self.leases.current

    @session.setter
    def session(self, value: Session | None) -> None:
        # Test seam only. Production code grants through `leases.grant` and releases
        # through `_end_ownership`.
        self.leases.overwrite(value)

    def _end_ownership(self, state: str, reason: str) -> None:
        """The only release path; refused while physically busy (jobs.md section 3.2)."""

        def finalize(session: Session) -> None:
            for intent in tuple(self._execution_intents.values()):
                self._cancel_execution_intent(intent, reason)
            if session.request_id is not None:
                self._store().set_request(session.request_id, state, reason)
                request = self.device_requests.get(session.request_id)
                if request is not None:
                    request.state = state

        try:
            ended = self.leases.end(finalize)
        except ReleaseRefused:
            raise BrokerError("active_job_cannot_release") from None
        if ended is None:
            return
        self._queue_sequence += 1
        self.operator_changes.notify()
        self.logs.append(Event.EXPIRED if state == "expired" else Event.RELEASED)

    def maintenance_tick(self) -> None:
        """Single-event-loop transition point. No awaits or hardware opens here."""
        self._prune_captures()
        self._prune_device_observations()
        for app_run in self.app_sessions.jobs.values():
            app_run.events.expire()
        if self.store is None:
            return
        self.home_recovery.maintain()
        now = self.clock()
        for work in self.work_sessions.values():
            if work.state == "open" and now >= work.deadline:
                self._store().set_work_session(work.id, "expired", "work_session_expired")
                work.state = "expired"
        for request_id in tuple(self.waiters):
            request = self.device_requests[request_id]
            if request.job_id is not None:
                if now >= request.deadline:
                    self._end_wait(request, "expired", "wait_expired")
                elif not self._owner_live(request.principal_id, request.origin):
                    self._end_wait(request, "cancelled", "principal_not_authorized")
                continue
            work = self.work_sessions[request.session_id]
            if work.state != "open" or now >= request.deadline:
                self._end_wait(request, "expired", "wait_expired")
            elif not self._principal_live(work):
                self._end_wait(request, "cancelled", "principal_not_authorized")
        session = self.session
        if session is not None:
            owning_work = self.work_sessions.get(session.work_session_id or "")
            if owning_work is not None:
                if owning_work.state not in ("open", "expired") or not self._principal_live(
                    owning_work
                ):
                    self.leases.revoke(session, now)
                elif owning_work.state == "expired":
                    # Clock expiry of the work session: like lease expiry, it ends an
                    # idle lease but does not revoke a running job.
                    self.leases.shorten(session, now)
            if (
                owning_work is None
                and session.principal_id is not None
                and self.principal_valid is not None
            ):
                try:
                    live = self.principal_valid(
                        session.principal_id, session.owner, frozenset({"read", "control"})
                    )
                except Exception:
                    live = False
                if live is not True:
                    self.leases.revoke(session, now)
            if now >= session.deadline:
                self._cancel_queued("lease_expired")
                if not self._physical_busy():
                    self._end_ownership("expired", "lease_expired")
        self._release_finished_job_lease()
        self._prune_execution_intents()
        self._deferred_tick()
        self._tickets_tick(now)
        if (
            self.closing
            or self.session is not None
            or self._physical_busy()
            or self._execution_uncertain()
            or not self.waiters
        ):
            return
        if self.hold is not None:
            # A reservation is the only thing that may take the slot while it lasts.
            held = [
                w for w in self.waiters if self.device_requests[w].ticket_id == self.hold.ticket_id
            ]
            if not held:
                return
            request = self.device_requests[held[0]]
        else:
            request = self.device_requests[self.waiters[0]]
        if request.job_id is not None:
            self._grant_job_lease(request)
            return
        work = self.work_sessions[request.session_id]
        # Recheck directly before the durable transition; no token is cached.
        if not self._principal_live(work):
            self._end_wait(request, "cancelled", "principal_not_authorized")
            return
        grant_now = self.clock()
        if grant_now >= work.deadline or grant_now >= request.deadline:
            self._end_wait(request, "expired", "wait_expired")
            if grant_now >= work.deadline:
                self._store().set_work_session(work.id, "expired", "work_session_expired")
                work.state = "expired"
            return
        lease = Session(
            secrets.token_hex(32),
            uuid4().hex,
            work.owner,
            min(grant_now + request.lease_seconds, work.deadline),
            work.principal_id,
            work.id,
            request.id,
            request.ticket_id,
        )
        self._store().set_request(request.id, "owning", lease_id=lease.id)
        request.state = "owning"
        request.lease_id = lease.id
        self.waiters.remove(request.id)
        if self.hold is not None and self.hold.ticket_id == request.ticket_id:
            self.hold = None
            self.tickets.set_hold(request.ticket_id or "", "ended")
        self.leases.grant(lease)
        self._queue_sequence += 1
        self.operator_changes.notify()
        self.logs.append(Event.ACQUIRED)

    def _owner_live(self, principal_id: str | None, origin: str | None) -> bool:
        if self.principal_valid is None or principal_id is None or origin is None:
            return False
        try:
            return (
                self.principal_valid(principal_id, origin, frozenset({"read", "control"})) is True
            )
        except Exception:
            return False

    def _parent_state(self, parent_id: str) -> str:
        """pending while the parent may still succeed, succeeded, or broken."""
        job = self._store().job(parent_id)
        if job is not None:
            if job["state"] in ("awaiting_approval", "queued", "running"):
                return "pending"
            return "succeeded" if job["state"] == "succeeded" else "broken"
        waiting = self.deferred.get(parent_id)
        return "pending" if waiting is not None and waiting["state"] == "pending" else "broken"

    def _deferred_tick(self) -> None:
        store = self._store()
        for row in self.deferred.pending():
            plan = store.plan(str(row["plan_hash"]))
            if plan is not None and plan.get("execution_context") == "device_deferred":
                self._advance_device_deferred(row, plan)
            else:
                self.profile_jobs.promote(row)

    def _advance_device_deferred(self, row: dict[str, Any], plan: dict[str, Any]) -> None:
        job_id = str(row["job_id"])
        if row["wait_id"]:
            wait = self.device_requests.get(str(row["wait_id"]))
            if wait is None or wait.state in ("cancelled", "expired"):
                self.deferred.finish(job_id, "cancelled", "wait_cancelled")
            return  # queued for the device; the grant happens later in the tick
        state = self._parent_state(str(row["parent_job_id"]))
        if state == "pending":
            return
        if state == "broken" or digest_plan(plan) != row["plan_hash"]:
            self.deferred.finish(job_id, "cancelled", "chain_broken")
            return
        if len(self.waiters) >= self.settings.max_waiters:
            return  # keep waiting for room in the device queue
        if self._store().record_count("device_requests") >= 10000:
            self.deferred.finish(job_id, "cancelled", "history_full_archive_offline")
            return
        wait = DeviceWait(
            uuid4().hex,
            "job:" + job_id,
            int(plan["lease_seconds"]),
            self.clock() + DEVICE_WAIT_SECONDS,
            ticket_id=row["ticket_id"],
            job_id=job_id,
            principal_id=str(row["principal_id"]),
            origin=str(row["origin"]),
        )
        self._store().put_request(wait.id, None, wait.lease_seconds)
        self.device_requests[wait.id] = wait
        self.deferred.set_wait(job_id, wait.id)
        if self.hold is not None and self.hold.ticket_id == row["ticket_id"]:
            self.waiters.insert(0, wait.id)  # continuation priority
        else:
            self.waiters.append(wait.id)
        self._queue_sequence += 1
        self.operator_changes.notify()

    def _grant_job_lease(self, request: DeviceWait) -> None:
        """Give the slot to a job its owner requested ahead of time; no capability is returned."""
        store = self._store()
        job_id = str(request.job_id)
        row = self.deferred.get(job_id)
        plan = store.plan(str(row["plan_hash"])) if row is not None else None

        def refuse(reason: str) -> None:
            self._end_wait(request, "cancelled", reason)
            if row is not None:
                self.deferred.finish(job_id, "cancelled", reason)
            if self.hold is not None and self.hold.ticket_id == request.ticket_id:
                self.hold = None
                self.tickets.set_hold(request.ticket_id or "", "ended")

        if (
            row is None
            or row["state"] != "pending"
            or plan is None
            or digest_plan(plan) != row["plan_hash"]
        ):
            return refuse("job_not_available")
        if not self._owner_live(request.principal_id, request.origin):
            return refuse("principal_not_authorized")
        fence = self.tickets._row(request.ticket_id) if request.ticket_id else None
        if (
            fence is not None
            and fence["fence_seq"] is not None
            and int(fence["fence_seq"]) != store.device_write_seq()
        ):
            return refuse("device_state_changed")
        lease = Session(
            secrets.token_hex(32),
            uuid4().hex,
            str(request.origin),
            self.clock() + request.lease_seconds,
            request.principal_id,
            None,
            request.id,
            request.ticket_id,
            job_id,
        )
        store.set_request(request.id, "owning", lease_id=lease.id)
        request.state = "owning"
        request.lease_id = lease.id
        self.waiters.remove(request.id)
        if self.hold is not None and self.hold.ticket_id == request.ticket_id:
            self.hold = None
            self.tickets.set_hold(request.ticket_id or "", "ended")
        self.leases.grant(lease)
        self._queue_sequence += 1
        self.operator_changes.notify()
        self.logs.append(Event.ACQUIRED)
        # Kept re-entrant on purpose: prepare/execute -> _lease -> _expire runs a nested
        # maintenance_tick under the new lease (see lease_manager.py).
        try:
            prepared = self.prepare(
                PrepareRequest(
                    session=lease.capability,
                    job_id=job_id,
                    test=TestName(plan["test"]),
                    **plan["params"],
                )
            )
            self.execute(ExecuteRequest(session=lease.capability, plan_hash=prepared["plan_hash"]))
        except Exception as failure:
            code = (
                str(failure)
                if isinstance(failure, BrokerError)
                else failure.code
                if isinstance(failure, SourceError)
                else "promotion_failed"
            )
            self.deferred.finish(job_id, "cancelled", code)
            try:
                self._end_ownership("released", "promotion_failed")
            except BrokerError:
                pass
            return
        self.deferred.finish(job_id, "promoted", None, prepared["plan_hash"])

    def _release_finished_job_lease(self) -> None:
        """A lease made for one pre-requested job ends with that job (or becomes a hold)."""
        session = self.session
        if session is None or session.job_id is None or self._physical_busy():
            return
        job = self._store().job(session.job_id)
        if job is None:
            waiting = self.deferred.get(session.job_id)
            if waiting is not None and waiting["state"] == "pending":
                return  # the job is being prepared under this lease right now
        if job is not None and job["state"] in ("awaiting_approval", "queued", "running"):
            return
        if job is not None and job["state"] == "succeeded":
            ticket = self.tickets.row_for_parent(session.job_id)
            if ticket is not None and ticket["state"] == "issued" and ticket["hold"] is None:
                return  # the ticket tick turns this lease into the reservation
        self._end_ownership("released", "job_complete")

    def prepare_deferred(
        self, request: PrepareRequest, principal_id: str, origin: str
    ) -> dict[str, Any]:
        """Plan a device job to be requested before the slot or its parent is ready."""
        store = self._store()
        assert request.after is not None
        if self.closing or not self._owner_live(principal_id, origin):
            raise BrokerError("principal_not_authorized")
        try:
            ticket = self.tickets.peek(request.after, principal_id)
        except TicketError as exc:
            raise BrokerError(str(exc)) from None
        parent_id = str(ticket["parent_job_id"])
        if self._parent_state(parent_id) == "broken":
            raise BrokerError("chain_broken")
        params = {
            name: getattr(request, name)
            for name in (
                "device_id",
                "build_job_id",
                "backup_job_id",
                "lkg_receipt",
                "flash_job_id",
            )
            if getattr(request, name) is not None
        }
        if "derby_detail_mode" in request.model_fields_set and request.derby_detail_mode:
            params["derby_detail_mode"] = request.derby_detail_mode.value
        plan: dict[str, Any] = dict(
            version=2,
            execution_context="device_deferred",
            broker_epoch=self.epoch,
            job_id=request.job_id,
            test=request.test.value,
            principal_id=principal_id,
            origin=origin,
            lease_seconds=request.lease_seconds or 60,
            params=params,
            continues={"parent_job_id": parent_id},
        )
        digest = digest_plan(plan)
        existing = store.job(request.job_id)
        waiting = self.deferred.get(request.job_id)
        if (existing and existing["plan_hash"] != digest) or (
            waiting and waiting["plan_hash"] != digest
        ):
            raise BrokerError("job_id_conflict")
        if store.plan(digest) is None and store.record_count("plans") >= 10000:
            raise BrokerError("history_full_archive_offline")
        store.put_plan(digest, plan)
        return dict(plan_hash=digest, plan=plan, approval_required=False)

    def execute_deferred(
        self, request: ExecuteRequest, principal_id: str, origin: str
    ) -> dict[str, Any]:
        """Accept the owner's explicit request now; the job joins the queue when its parent ends."""
        store = self._store()
        plan = store.plan(request.plan_hash)
        if (
            plan is None
            or digest_plan(plan) != request.plan_hash
            or plan.get("execution_context") != "device_deferred"
            or plan.get("principal_id") != principal_id
            or plan.get("origin") != origin
        ):
            raise BrokerError("plan_not_found_or_changed")
        job_id = str(plan["job_id"])
        waiting = self.deferred.get(job_id)
        existing = store.job(job_id)
        if waiting is not None or existing is not None:
            if (waiting and waiting["plan_hash"] != request.plan_hash) or (
                waiting is None and existing is not None
            ):
                raise BrokerError("job_id_conflict")
            assert waiting is not None
            return {**self.deferred.view(waiting), "deduplicated": True}
        if (
            plan["broker_epoch"] != self.epoch
            or self.closing
            or not self._owner_live(principal_id, origin)
        ):
            raise BrokerError("principal_not_authorized")
        if self._execution_uncertain():
            raise BrokerError("recovery_review_required")
        if store.record_count("jobs") >= 10000:
            raise BrokerError("history_full_archive_offline")
        parent_id = str(plan["continues"]["parent_job_id"])
        ticket = self.tickets.row_for_parent(parent_id)
        if ticket is None:
            raise BrokerError("ticket_invalid")
        try:
            self.deferred.add(
                job_id, request.plan_hash, parent_id, principal_id, origin, str(ticket["ticket_id"])
            )
            try:
                self.tickets.consume(str(ticket["ticket_id"]), principal_id)
                self.tickets.note_consumer(str(ticket["ticket_id"]), job_id)
            except TicketError:
                self.deferred.remove(job_id)
                raise
        except TicketError as exc:
            raise BrokerError(str(exc)) from None
        row = self.deferred.get(job_id)
        assert row is not None
        return {**self.deferred.view(row), "deduplicated": False}

    def cancel_job_only(self, job_id: str, principal_id: str) -> dict[str, Any] | None:
        """Cancel a pre-requested job through its own lease (owner identity, no capability)."""
        lease = self.session
        if lease is None or lease.job_id != job_id or self.job_principal(job_id) != principal_id:
            return None
        return self.cancel(CancelRequest(session=lease.capability, job_id=job_id))

    def _release_hold(self, reason: str) -> None:
        hold = self.hold
        if hold is None:
            return
        self.hold = None
        self.tickets.mark_state(hold.ticket_id, "expired", reason)
        self.tickets.set_hold(hold.ticket_id, "ended")
        # A follower that spent the ticket in time keeps waiting, but no longer first.
        for request_id in tuple(self.waiters):
            if self.device_requests[request_id].ticket_id == hold.ticket_id:
                self.waiters.remove(request_id)
                self.waiters.append(request_id)
        self._queue_sequence += 1
        self.operator_changes.notify()

    def _tickets_tick(self, now: float) -> None:
        """Decide void/hold for unspent tickets from job state. Synchronous, no awaits."""
        store = self._store()
        wall = time.time()
        hold = self.hold
        if hold is not None:
            row = self.tickets._row(hold.ticket_id)
            if (
                row is None
                or now >= hold.deadline
                or self.tickets.budget_remaining(row) == 0.0
                or self._execution_uncertain()
            ):
                self._release_hold("hold_expired")
        for row in self.tickets.issued():
            ticket_id = str(row["ticket_id"])
            job = store.job(str(row["parent_job_id"]))
            if job is None:
                waiting = self.deferred.get(str(row["parent_job_id"]))
                if waiting is not None and waiting["state"] == "pending":
                    continue
                if waiting is not None and waiting["state"] == "promoted":
                    continue
                self.tickets.mark_state(ticket_id, "void", "chain_broken")
                continue
            if job["state"] in ("awaiting_approval", "queued", "running"):
                continue
            if job["state"] != "succeeded":
                self.tickets.mark_state(ticket_id, "void", "ticket_void")
                continue
            if row["hold"] is not None:
                if row["hold"] == "none" and wall - float(row["created"]) > TICKET_TTL_SECONDS:
                    self.tickets.mark_state(ticket_id, "expired", "hold_expired")
                continue
            plan = store.plan(job["plan_hash"])
            if plan is None or "session_id" not in plan:
                self.tickets.set_hold(ticket_id, "none")
                continue
            self.tickets.set_fence(ticket_id, store.device_write_seq())
            if self._execution_uncertain():
                self.tickets.mark_state(ticket_id, "void", "ticket_void")
                continue
            if self.hold is not None or self.tickets.budget_remaining(row) == 0.0:
                self.tickets.set_hold(ticket_id, "none")
                continue
            if self._physical_busy() or not self.queue.empty() or self._execution_intents:
                continue
            session = self.session
            if session is not None and session.id != plan["session_id"]:
                self.tickets.mark_state(ticket_id, "expired", "hold_expired")
                self.tickets.set_hold(ticket_id, "ended")
                continue
            finished = self.tickets.finished_epoch(job)
            elapsed = max(0.0, wall - finished) if finished is not None else 0.0
            if elapsed >= HOLD_SECONDS:
                self.tickets.mark_state(ticket_id, "expired", "hold_expired")
                self.tickets.set_hold(ticket_id, "ended")
                continue
            if session is not None:
                try:
                    self._end_ownership("released", "chain_hold")
                except BrokerError:
                    continue
            self.hold = Hold(ticket_id, str(row["principal_id"]), now + HOLD_SECONDS - elapsed)
            self.tickets.set_hold(ticket_id, "held")
            self._queue_sequence += 1
            self.operator_changes.notify()

    def job_principal(self, job_id: str) -> str | None:
        """Principal that explicitly requested a job (recorded when it was queued)."""
        row = (
            self._store()
            .db.execute("SELECT principal_id FROM execution_requests WHERE job_id=?", (job_id,))
            .fetchone()
        )
        return str(row[0]) if row is not None else None

    def issue_ticket(self, job_id: str, total_hint: int | None = None) -> str:
        """Issue the single continuation ticket of one queued device job."""
        store = self._store()
        job = store.job(job_id)
        plan = store.plan(job["plan_hash"]) if job is not None else None
        principal_id = self.job_principal(job_id)
        if job is None:
            waiting = self.deferred.get(job_id)
            plan = store.plan(waiting["plan_hash"]) if waiting is not None else None
            principal_id = waiting["principal_id"] if waiting is not None else None
        elif plan is not None and plan.get("execution_context") == "registered_project":
            principal_id = plan.get("principal_id")
        registered = plan is not None and (
            str(plan.get("execution_context", "")).startswith("registered_project")
            or plan.get("execution_context") == "device_deferred"
        )
        if (
            plan is None
            or ("session_id" not in plan and not registered)
            or not isinstance(principal_id, str)
        ):
            raise BrokerError("continuation_not_supported")
        try:
            return self.tickets.issue(job_id, principal_id, total_hint=total_hint)
        except TicketError as exc:
            raise BrokerError(str(exc)) from None

    def deferred_job(self, job_id: str, principal_id: str) -> dict[str, Any] | None:
        """Job-shaped view of a waiting job; only for the principal that requested it."""
        row = self.deferred.get(job_id)
        if row is None or row["principal_id"] != principal_id:
            return None
        if row["state"] == "promoted":
            return None  # the real job row answers from here on
        view = self.deferred.view(row)
        if row["wait_id"] and row["wait_id"] in self.waiters:
            view["device_queue_position"] = self.waiters.index(row["wait_id"]) + 1
        return view

    def cancel_deferred(self, job_id: str, principal_id: str) -> dict[str, Any] | None:
        row = self.deferred.get(job_id)
        if row is None or row["principal_id"] != principal_id or row["state"] != "pending":
            return None
        self.deferred.finish(job_id, "cancelled", "client_cancelled_waiting")
        wait = self.device_requests.get(str(row["wait_id"])) if row["wait_id"] else None
        if wait is not None and wait.state == "waiting":
            self._end_wait(wait, "cancelled", "client_cancelled_waiting")
        updated = self.deferred.get(job_id)
        assert updated is not None
        return self.deferred.view(updated)

    def cancel_ticket(self, ticket_id: str, principal_id: str) -> dict[str, Any]:
        row = self.tickets._row(ticket_id)
        if row is None or row["principal_id"] != principal_id:
            raise BrokerError("ticket_invalid")
        if row["state"] == "consumed":
            raise BrokerError("ticket_consumed")
        if row["state"] == "issued":
            self.tickets.mark_state(ticket_id, "void", "client_cancelled")
        if self.hold is not None and self.hold.ticket_id == ticket_id:
            self.hold = None
            self.tickets.set_hold(ticket_id, "ended")
            self._queue_sequence += 1
            self.operator_changes.notify()
        return {"cancelled": True}

    def hold_status(self) -> dict[str, Any]:
        """Aggregate only: no ticket, job or principal is disclosed."""
        hold = self.hold
        return {
            "active": hold is not None,
            "expires_in_seconds": max(0, round(hold.deadline - self.clock(), 3)) if hold else 0,
        }

    def chain_progress(self, job_id: str) -> dict[str, Any] | None:
        """Read-only chain progress derived from ticket links; None if not in a chain."""
        before, after, open_ticket = self.tickets.chain_jobs(job_id)
        own = self.tickets.row_for_parent(job_id)
        if not before and not after and own is None:
            return None
        job = self._store().job(job_id)
        waiting = self.deferred.get(job_id) if job is None else None
        reference = own or open_ticket
        if reference is None and before:
            reference = self.tickets._previous(job_id)
        hold = self.hold
        held = own is not None and hold is not None and hold.ticket_id == own["ticket_id"]
        state = (
            str(job["state"])
            if job is not None
            else "waiting_parent"
            if waiting is not None and waiting["state"] == "pending"
            else "cancelled"
            if waiting is not None and waiting["state"] == "cancelled"
            else "unknown"
        )
        if state == "waiting_parent":
            chain_state = "waiting_parent"
        elif state in ("failed", "cancelled", "needs_review", "interrupted"):
            chain_state = "broken"
        elif state == "queued":
            chain_state = "waiting_for_device"
        elif state == "running":
            chain_state = "running"
        elif held:
            chain_state = "holding_device"
        elif after or (own is not None and own["state"] == "issued"):
            chain_state = "between_jobs"
        else:
            chain_state = "finished"
        budget = self.tickets.budget_remaining(reference)
        return {
            "chain_position": len(before) + 1,
            "chain_jobs_known": len(before) + 1 + len(after),
            "chain_open": bool(open_ticket is not None and open_ticket["state"] == "issued"),
            "chain_state": chain_state,
            "chain_total": reference["chain_total"] if reference is not None else None,
            "hold_expires_in_seconds": (
                max(0, round(hold.deadline - self.clock(), 3)) if held and hold else None
            ),
            "chain_budget_remaining_seconds": round(budget, 1) if budget is not None else None,
        }

    async def _maintain(self) -> None:
        while not self.closing:
            await asyncio.sleep(0.25)
            self.maintenance_tick()

    def _expire(self) -> None:
        self.maintenance_tick()

    def _lease(self, capability: str | None, *, running_job: bool = False) -> Session:
        """The live lease for `capability`.

        `running_job=True` (cancel only) also accepts a lease past its deadline while
        a running job keeps it, so the owner can still stop that job.
        """
        self._expire()
        if self.closing:
            raise BrokerError("broker_closing")
        if (
            capability is None
            or self.session is None
            or not secrets.compare_digest(self.session.capability, capability)
        ):
            raise BrokerError("invalid_session")
        if self.clock() >= self.session.deadline and not (
            running_job and self._lease_kept_by_running_job(self.session)
        ):
            raise BrokerError("lease_expired_active_job_retains_ownership")
        return self.session

    def _lease_kept_by_running_job(self, lease: Session) -> bool:
        """The lease is held past its deadline only because its own job is running."""
        job_id = self.active_job
        if job_id is None or lease.revoked or self.closing or self.session is not lease:
            return False
        job = self._store().job(job_id)
        if job is None or job["state"] != "running":
            return False
        plan = self._store().plan(job["plan_hash"])
        return bool(plan is not None and plan.get("session_id") == lease.id)

    def _connection(self) -> dict[str, Any]:
        """Keep the broker's own port state apart from USB presence; opens nothing."""
        presence = getattr(self.backend, "usb_presence", None)
        if self.backend.name == "mock":
            usb, source = "not_applicable_mock_backend", None
        elif callable(presence):
            usb, source = presence(), "usb_enumeration_without_opening_port"
        else:
            usb, source = "not_checked_by_status", None
        return {
            "serial_port_open": self.backend.hardware_connected,
            "serial_port_open_meaning": SERIAL_PORT_OPEN_MEANING,
            "usb_presence": usb,
            "usb_presence_source": source,
            "usb_presence_meaning": USB_PRESENCE_MEANING,
        }

    def status(self) -> dict[str, Any]:
        self._expire()
        store = self._store()
        session = self.session
        uncertain = self._execution_uncertain()
        busy = self._physical_busy()
        pending_review = store.needs_review()
        return {
            "backend": self.backend.name,
            "hardware_connected": self.backend.hardware_connected,
            "connection": self._connection(),
            "ownership_uncertain": bool(getattr(self.backend, "ownership_uncertain", False)),
            "device": self.backend.device,
            "broker_epoch": self.epoch,
            "active_job": self.active_job,
            "profile_active_job": self.profile_jobs.active,
            "profile_queue_depth": self.profile_jobs.queue.qsize(),
            "profile_uncertain": self.profile_jobs._uncertain(),
            "profile_execution_policy": "registered_project",
            "owner": session.owner if session else None,
            "lease_remaining_seconds": max(0, round(session.deadline - self.clock(), 3))
            if session
            else 0,
            "queue_depth": self.queue.qsize(),
            "wait_queue_depth": len(self.waiters),
            "device_hold": self.hold_status(),
            "work_session_count": sum(w.state == "open" for w in self.work_sessions.values()),
            "history": store.counts(),
            "needs_review": pending_review,
            "execution_uncertain": uncertain,
            "safety": {
                "current_resource_uncertain": uncertain,
                "owned_work_active": busy,
                "historical_review_pending": pending_review,
                # Each set runtime latch and its fixed reason. The local operator's
                # explicit re-check clears only latches whose concrete check passes.
                "uncertainty": uncertainty(self),
                "physical_device_state": "not_inferred_from_job_history",
                "required_action": (
                    "inspect_owned_resources_before_new_work"
                    if uncertain
                    else "wait_for_owned_work"
                    if busy
                    else "inspect_job_and_use_explicit_recovery_if_eligible"
                    if pending_review
                    else "none_reported_by_broker"
                ),
                "authorizes_new_work": False,
            },
            "execution_policy": "authenticated_explicit_request",
            "hardware_backend": "serial_operations_opt_in"
            if self.backend.name == "serial"
            else "available_opt_in_only",
            "tunnel": "external_lifecycle_unknown",
            "tunnel_ingress_enabled": (
                self.settings.tunnel is not None and self.settings.tunnel.ingress_enabled
            ),
        }

    def acquire(
        self, request: AcquireRequest, *, principal_id: str | None = None
    ) -> dict[str, Any]:
        self._expire()
        if self.closing:
            raise BrokerError("broker_closing")
        if self._execution_uncertain():
            raise BrokerError("recovery_review_required")
        if self.session or self.waiters or self.hold is not None or self._physical_busy():
            raise BrokerError("device_busy")
        if self._store().record_count("device_requests") >= 10000:
            raise BrokerError("history_full_archive_offline")
        lease = Session(
            secrets.token_hex(32),
            uuid4().hex,
            request.owner.value,
            self.clock() + request.lease_seconds,
            principal_id,
        )
        request_id = uuid4().hex
        self._store().put_request(
            request_id, None, request.lease_seconds, state="owning", lease_id=lease.id
        )
        lease.request_id = request_id
        self.leases.grant(lease)
        self._queue_sequence += 1
        self.operator_changes.notify()
        self.logs.append(Event.ACQUIRED)
        return {
            "session": lease.capability,
            "lease_seconds": request.lease_seconds,
            "owner": request.owner.value,
        }

    def require_owner(
        self, capability: str | None, owner: str, *, principal_id: str | None = None
    ) -> None:
        session = self._lease(capability)
        if session.owner != owner or (
            principal_id is not None and session.principal_id != principal_id
        ):
            raise BrokerError("authenticated_owner_mismatch")

    def renew(self, request: RenewRequest) -> dict[str, Any]:
        session = self._lease(request.session)
        until = self.clock() + request.lease_seconds
        work = self.work_sessions.get(session.work_session_id or "")
        self.leases.renew(session, until, work.deadline if work is not None else None)
        self._queue_sequence += 1
        self.operator_changes.notify()
        return {"renewed": True, "lease_seconds": request.lease_seconds}

    def release(self, request: LeaseRequest) -> dict[str, Any]:
        self._lease(request.session)
        if self._physical_busy():
            raise BrokerError("active_job_cannot_release")
        self._cancel_queued("session_released")
        self._end_ownership("released", "session_released")
        self.maintenance_tick()
        return {"released": True}

    @staticmethod
    def _valid_scopes(scopes: frozenset[str], required: str) -> None:
        if not isinstance(scopes, frozenset) or not scopes <= {"read", "control"}:
            raise BrokerError("invalid_session_scopes")
        if "read" not in scopes or required not in scopes:
            raise BrokerError("insufficient_scope")

    def _authorized_work(
        self,
        resume_token: str,
        principal_id: str,
        scopes: frozenset[str],
        *,
        required: str = "control",
        allow_terminal: bool = False,
    ) -> WorkSession:
        self._store()
        self.maintenance_tick()
        if self.closing:
            raise BrokerError("broker_closing")
        self._valid_scopes(scopes, required)
        work = next(
            (
                item
                for item in self.work_sessions.values()
                if secrets.compare_digest(item.resume_token, resume_token)
            ),
            None,
        )
        if work is None:
            raise BrokerError("invalid_work_session")
        if principal_id != work.principal_id:
            raise BrokerError("authenticated_owner_mismatch")
        if required not in work.scopes:
            raise BrokerError("insufficient_scope")
        if work.state != "open" and not allow_terminal:
            raise BrokerError("work_session_not_open")
        return work

    def _public_request(self, request: DeviceWait) -> dict[str, Any]:
        lease = self.session
        is_owner = lease is not None and lease.request_id == request.id
        return {
            "request_id": request.id,
            "session_id": request.session_id,
            "state": request.state,
            "position": self.waiters.index(request.id) + 1 if request.id in self.waiters else None,
            "lease_id": request.lease_id,
            "wait_remaining_seconds": max(0, round(request.deadline - self.clock(), 3))
            if request.state == "waiting"
            else 0,
            "lease_remaining_seconds": max(0, round(lease.deadline - self.clock(), 3))
            if is_owner and lease is not None
            else 0,
            "active_job": self.active_job if is_owner else None,
        }

    def _public_work(self, work: WorkSession) -> dict[str, Any]:
        return {
            "session_id": work.id,
            "owner": work.owner,
            "principal_id": work.principal_id,
            "scopes": sorted(work.scopes),
            "state": work.state,
            "expires_in_seconds": max(0, round(work.deadline - self.clock(), 3)),
            "request_id": work.request_id,
        }

    def _work_result(self, work: WorkSession, scopes: frozenset[str]) -> dict[str, Any]:
        public = self._public_work(work)
        public["work_session_id"] = public.pop("session_id")
        request = self.device_requests.get(work.request_id or "")
        lease = self.session
        lease_result = None
        if (
            lease is not None
            and lease.work_session_id == work.id
            and work.state == "open"
            and self.clock() < lease.deadline
            and "control" in scopes
            and "control" in work.scopes
        ):
            lease_result = {
                "session": lease.capability,
                "lease_id": lease.id,
                "lease_seconds": max(0, round(lease.deadline - self.clock(), 3)),
                "request_id": lease.request_id,
            }
        return {
            **public,
            "request": self._public_request(request) if request else None,
            "lease": lease_result,
        }

    def create_work_session(
        self,
        request: CreateWorkSessionRequest,
        *,
        principal_id: str,
        scopes: frozenset[str],
    ) -> dict[str, Any]:
        self._store()
        self.maintenance_tick()
        if self.closing:
            raise BrokerError("broker_closing")
        self._valid_scopes(scopes, "control")
        if type(principal_id) is not str or not 1 <= len(principal_id) <= 128:
            raise BrokerError("invalid_principal")
        if sum(w.state == "open" for w in self.work_sessions.values()) >= (
            self.settings.max_work_sessions
        ):
            raise BrokerError("work_session_limit")
        if self._store().record_count("work_sessions") >= 10000:
            raise BrokerError("history_full_archive_offline")
        work = WorkSession(
            secrets.token_hex(32),
            uuid4().hex,
            request.owner.value,
            principal_id,
            scopes,
            self.clock() + request.ttl_seconds,
        )
        self._store().put_work_session(work.id, self.epoch, work.owner, principal_id, scopes)
        self.work_sessions[work.id] = work
        return {**self._work_result(work, scopes), "resume_token": work.resume_token}

    def resume_work_session(
        self,
        request: ResumeWorkSessionRequest,
        *,
        principal_id: str,
        scopes: frozenset[str],
    ) -> dict[str, Any]:
        work = self._authorized_work(
            request.resume_token, principal_id, scopes, required="read", allow_terminal=True
        )
        return self._work_result(work, scopes)

    def request_device(
        self,
        request: RequestDeviceRequest,
        *,
        principal_id: str,
        scopes: frozenset[str],
    ) -> dict[str, Any]:
        work = self._authorized_work(request.resume_token, principal_id, scopes)
        current = self.device_requests.get(work.request_id or "")
        if current is not None and current.state in ("waiting", "owning"):
            return self._work_result(work, scopes)
        if not self._principal_live(work):
            raise BrokerError("principal_not_authorized")
        if self._execution_uncertain():
            raise BrokerError("recovery_review_required")
        if len(self.waiters) >= self.settings.max_waiters:
            raise BrokerError("wait_queue_full")
        if self._store().record_count("device_requests") >= 10000:
            raise BrokerError("history_full_archive_offline")
        waiting = DeviceWait(
            uuid4().hex,
            work.id,
            request.lease_seconds,
            min(self.clock() + request.wait_seconds, work.deadline),
        )
        priority = False
        if request.after is not None:
            # Spending the ticket and taking the queue position are one synchronous step.
            self.maintenance_tick()
            try:
                self.tickets.consume(request.after, work.principal_id)
            except TicketError as exc:
                raise BrokerError(str(exc)) from None
            waiting.ticket_id = request.after
            priority = self.hold is not None and self.hold.ticket_id == request.after
        self._store().put_request(waiting.id, work.id, request.lease_seconds)
        self.device_requests[waiting.id] = waiting
        work.request_id = waiting.id
        if priority:
            self.waiters.insert(0, waiting.id)
        else:
            self.waiters.append(waiting.id)
        self._queue_sequence += 1
        self.operator_changes.notify()
        self.maintenance_tick()
        return self._work_result(work, scopes)

    def cancel_device_request(
        self,
        request: CancelDeviceRequest,
        *,
        principal_id: str,
        scopes: frozenset[str],
    ) -> dict[str, Any]:
        work = self._authorized_work(
            request.resume_token, principal_id, scopes, allow_terminal=True
        )
        waiting = self.device_requests.get(request.request_id)
        if waiting is None:
            raise BrokerError("request_not_found")
        if waiting.session_id != work.id:
            raise BrokerError("request_session_mismatch")
        if waiting.state == "cancelled":
            return {**self._work_result(work, scopes), "cancelled": True}
        if waiting.state != "waiting":
            raise BrokerError("only_waiting_requests_can_be_cancelled")
        self._end_wait(waiting, "cancelled", "client_cancelled_wait")
        self.maintenance_tick()
        return {**self._work_result(work, scopes), "cancelled": True}

    def close_work_session(
        self,
        request: CloseWorkSessionRequest,
        *,
        principal_id: str,
        scopes: frozenset[str],
    ) -> dict[str, Any]:
        work = self._authorized_work(
            request.resume_token, principal_id, scopes, allow_terminal=True
        )
        if self.session is not None and self.session.work_session_id == work.id:
            if self._physical_busy():
                raise BrokerError("active_job_cannot_release")
            self._cancel_queued("work_session_closed")
            self._end_ownership("released", "work_session_closed")
        waiting = self.device_requests.get(work.request_id or "")
        if waiting is not None and waiting.state == "waiting":
            self._end_wait(waiting, "cancelled", "work_session_closed")
        if work.state == "open":
            self._store().set_work_session(work.id, "closed", "client_closed_session")
            work.state = "closed"
        self.maintenance_tick()
        return {**self._work_result(work, scopes), "closed": True}

    def _queue_revision(self) -> str:
        return digest_plan({"broker_epoch": self.epoch, "sequence": self._queue_sequence})

    def operator_snapshot(self) -> dict[str, Any]:
        """Local operator view only. Public identifiers, never either capability."""
        self.maintenance_tick()
        store = self._store()
        lease = self.session
        owner = None
        if lease is not None:
            owner = {
                "lease_id": lease.id,
                "request_id": lease.request_id,
                "session_id": lease.work_session_id,
                "owner": lease.owner,
                "principal_id": lease.principal_id,
                "lease_remaining_seconds": max(0, round(lease.deadline - self.clock(), 3)),
                "release_blocked": self._physical_busy(),
            }
        # Keep every live session and an expired owner that is still physically
        # pinned. Historical rows remain durable but do not grow every GUI poll.
        current_owner = lease.work_session_id if lease is not None else None
        visible_ids = {
            work.id
            for work in self.work_sessions.values()
            if work.state == "open" or work.id == current_owner
        }
        recent_terminal = [
            work for work in reversed(self.work_sessions.values()) if work.id not in visible_ids
        ][:20]
        visible_ids.update(work.id for work in recent_terminal)
        visible_sessions = [
            self._public_work(work)
            for work in self.work_sessions.values()
            if work.id in visible_ids
        ]
        return {
            "broker_epoch": self.epoch,
            "backend": self.backend.name,
            "queue_revision": self._queue_revision(),
            "sessions": visible_sessions,
            "session_history_omitted": store.record_count("work_sessions") - len(visible_sessions),
            "waiters": [self._public_request(self.device_requests[r]) for r in self.waiters],
            "owner": owner,
            "active_job": self.active_job,
            "needs_review": store.needs_review(),
            "execution_uncertain": self._execution_uncertain(),
            "ownership_uncertain": bool(getattr(self.backend, "ownership_uncertain", False)),
            "recovery_requests": store.recovery_requests(),
            "uncertainty": uncertainty(self),
        }

    def operator_cancel_wait(self, request_id: str) -> dict[str, Any]:
        self.maintenance_tick()
        waiting = self.device_requests.get(request_id)
        if waiting is None:
            raise BrokerError("request_not_found")
        if waiting.state != "waiting":
            raise BrokerError("only_waiting_requests_can_be_cancelled")
        self._end_wait(waiting, "cancelled", "operator_cancelled_wait")
        self.maintenance_tick()
        return {"cancelled": True, "request_id": request_id}

    def operator_stop_job(
        self, job_id: str, plan_hash: str, broker_epoch: str, principal_id: str
    ) -> dict[str, Any]:
        """Local operator stop of another client's lease job; never a flash write."""
        from . import operator_stop  # operator_stop.py imports this module

        return operator_stop.stop_job(self, job_id, plan_hash, broker_epoch, principal_id)

    def operator_reorder_wait(
        self,
        request_id: str,
        position: int,
        *,
        expected_revision: str,
    ) -> dict[str, Any]:
        self._store()
        self.maintenance_tick()
        if type(expected_revision) is not str or not secrets.compare_digest(
            expected_revision, self._queue_revision()
        ):
            raise BrokerError("stale_operator_snapshot")
        waiting = self.device_requests.get(request_id)
        if waiting is None:
            raise BrokerError("request_not_found")
        if waiting.state != "waiting":
            raise BrokerError("only_waiting_requests_can_be_reordered")
        if type(position) is not int or not 1 <= position <= len(self.waiters):
            raise BrokerError("invalid_wait_position")
        self.waiters.remove(request_id)
        self.waiters.insert(position - 1, request_id)
        self._queue_sequence += 1
        self.operator_changes.notify()
        return {"reordered": True, "request_id": request_id, "position": position}

    def operator_release_idle(self, lease_id: str) -> dict[str, Any]:
        self.maintenance_tick()
        if self.session is None or self.session.id != lease_id:
            raise BrokerError("lease_not_found")
        if self._physical_busy():
            raise BrokerError("active_job_cannot_release")
        self._cancel_queued("operator_released_idle")
        self._end_ownership("released", "operator_released_idle")
        self.maintenance_tick()
        return {"released": True, "lease_id": lease_id}

    def prepare(self, request: PrepareRequest) -> dict[str, Any]:
        session = self._lease(request.session)
        store = self._store()
        if (
            request.test
            in (
                {
                    TestName.APP_SESSION,
                    TestName.CAPTURE,
                    TestName.DERBY,
                    TestName.DERBY_FIXED,
                    TestName.FIRMWARE,
                    TestName.RESTART,
                    TestName.BUILD,
                    TestName.SOURCE,
                }
                | FLASH_TESTS
            )
            and session.principal_id is None
        ):
            raise BrokerError("authentication_required")
        if self._execution_uncertain():
            raise BrokerError("recovery_review_required")
        storage = None
        if (
            request.test == TestName.APP_SESSION
            and request.app is not None
            and request.app.start == "resume"
        ):
            raise BrokerError("unsupported")
        try:
            if request.test == TestName.APP_SESSION:
                if request.app is None or request.project_id is not None:
                    raise ValueError("app_plan_invalid")
                details = self.app_sessions.prepare(self.backend, request.app)
            elif request.test in FLASH_TESTS:
                details = self.firmware_flash.prepare(
                    request, session.principal_id or "", self.epoch, session.id
                )
            elif request.test == TestName.SOURCE:
                self.firmware_source.settings = self.settings
                details = self.firmware_source.prepare(request, session.principal_id or "")
                storage = {
                    "source_cache": self.firmware_source.snapshots.usage(request.project_id or "")
                }
            elif request.test == TestName.BUILD:
                if request.source_receipt is not None:
                    self.firmware_source.authorize_receipt(
                        request.project_id or "", request.source_receipt, session.principal_id or ""
                    )
                details = self.firmware_build.prepare(
                    request.project_id,
                    session.principal_id or "",
                    request.source_receipt,
                    request.derby_detail_mode,
                    **(
                        {"derby_dma_mode": request.derby_dma_mode}
                        if request.derby_dma_mode is not None
                        else {}
                    ),
                )
                storage = self.firmware_build.storage(request.project_id or "")
            elif request.test == TestName.DERBY_FIXED and request.flash_job_id is not None:
                if request.project_id is not None:
                    raise ValueError("project_only_for_build")
                details = prepare_collector(self, request.flash_job_id, session.principal_id or "")
            else:
                if request.project_id is not None:
                    raise ValueError("project_only_for_build")
                details = self.backend.prepare(request.test)
        except BuildLocationError as exc:
            raise BrokerError(exc.code) from None
        except FlashFailure as exc:
            if exc.public_details is not None:
                raise RecoveryPointRefusal(str(exc), exc.public_details) from None
            raise BrokerError(str(exc)) from None
        except FlashAdmissionError as exc:
            raise BrokerError(str(exc)) from None
        except ValueError as exc:
            raise BrokerError("backend_preflight_failed") from exc
        payload = {
            "version": 1,
            "broker_epoch": self.epoch,
            "session_id": session.id,
            "device": self.backend.device,
            "backend": self.backend.name,
            "job_id": request.job_id,
            "test": request.test.value,
            **details,
        }
        if request.test in (TestName.FIRMWARE, TestName.RESTART, TestName.APP_SESSION):
            payload["log_delivery"] = self._log_delivery(session)
        digest = digest_plan(payload)
        existing = store.job(request.job_id)
        if existing and existing["plan_hash"] != digest:
            raise BrokerError("job_id_conflict")
        if store.plan(digest) is None and store.record_count("plans") >= 10000:
            raise BrokerError("history_full_archive_offline")
        store.put_plan(digest, payload)
        return {
            "plan_hash": digest,
            "plan": payload,
            **({"storage": storage} if storage is not None else {}),
            "approval_required": False,
        }

    @staticmethod
    def _log_delivery(session: Session) -> dict[str, Any]:
        """The immutable plan explicitly names the bounded log recipient."""
        if session.principal_id is None:
            raise BrokerError("authentication_required")
        return {
            "principal_id": session.principal_id,
            "origin": session.owner,
            "retention_seconds": 300,
            "max_received_bytes": 65536,
            "max_lines": 256,
            "max_line_bytes": 1024,
            "max_rendered_bytes": 32768,
        }

    def _runtime_binding(self) -> str:
        """Callback-free configuration snapshot, separate from observed device proof."""
        config = getattr(self.backend, "config", None)
        return digest_plan(
            {
                "settings": self.settings.model_dump(mode="json"),
                "backend": self.backend.name,
                "device": self.backend.device,
                "backend_config": config.model_dump(mode="json") if config is not None else None,
            }
        )

    def execute(self, request: ExecuteRequest) -> dict[str, Any]:
        result = self._execute(request, "sync", None)
        assert result is not None
        return result

    async def execute_async(
        self, request: ExecuteRequest, reauthorize: Callable[[], None] | None = None
    ) -> dict[str, Any]:
        """Admit a request without blocking the loop on firmware file verification.

        (a) Every cheap check, including deduplication, runs synchronously.
        (b) Pure file reads/hashing of the flash binding run in worker threads
            (offload_io); store access stays on the loop.
        (c) Every check of (a) runs again, the binding is recomputed only from
            (b)'s memoized reads and must equal (b)'s snapshot, then the final
            check block and the enqueue follow with no await in between.
        Other plans keep the unchanged synchronous path.
        """
        payload = self._store().plan(request.plan_hash)
        if not isinstance(payload, dict) or payload.get("test") not in {
            t.value for t in FLASH_TESTS
        }:
            return self.execute(request)
        admitted = self._execute(request, "admit", None)
        if admitted is not None:
            return admitted
        memo = IOMemo()

        def verify() -> FlashBinding | None:
            session = self._lease(request.session)
            return self._verify_execution_inputs(request.plan_hash, session, payload)

        async with self._file_verification:
            # A concurrent request for the same plan may have queued it meanwhile.
            admitted = self._execute(request, "admit", None)
            if admitted is not None:
                return admitted
            try:
                snapshot = await offload(verify, memo)
            except BrokerError:
                # The same plan may have been queued (and even finished) meanwhile:
                # a repeated request stays deduplicated rather than refused.
                repeated = self._execute(request, "admit", None)
                if repeated is not None:
                    return repeated
                raise
            if reauthorize is not None:
                reauthorize()
            result = self._execute(request, "commit", (memo, snapshot))
        assert result is not None
        return result

    def _verify_execution_inputs(
        self, plan_hash: str, session: Session, payload: dict[str, Any]
    ) -> FlashBinding | None:
        """Fresh preflight and flash provenance; firmware file work goes through pure_io."""
        # Fresh preflight preserves device, firmware and registered-tool binding.
        # It cannot grant authority: only this authenticated request can enqueue.
        from .plan_approval import PlanApprovals

        PlanApprovals(self).validate_current(plan_hash, session)
        if payload["test"] in {t.value for t in FLASH_TESTS}:
            return prepare_binding(self, payload)
        return None

    def _execute(
        self,
        request: ExecuteRequest,
        phase: str,
        prefetched: tuple[IOMemo, FlashBinding | None] | None,
    ) -> dict[str, Any] | None:
        """`phase`: "sync" (all inline), "admit" (stop before file work), "commit"."""
        store = self._store()
        payload = store.plan(request.plan_hash)
        if payload is None or digest_plan(payload) != request.plan_hash:
            raise BrokerError("plan_not_found_or_changed")
        session = self._lease(request.session)
        if payload["broker_epoch"] != self.epoch or payload["session_id"] != session.id:
            raise BrokerError("plan_session_mismatch")
        if payload["backend"] != self.backend.name or payload["device"] != self.backend.device:
            raise BrokerError("plan_backend_mismatch")
        if payload["test"] in (
            TestName.FIRMWARE.value,
            TestName.RESTART.value,
            TestName.APP_SESSION.value,
        ) and payload.get("log_delivery") != self._log_delivery(session):
            raise BrokerError("plan_session_mismatch")
        job_id = str(payload["job_id"])
        intent = ExecutionIntent(
            self.epoch,
            request.plan_hash,
            job_id,
            session.id,
            session.principal_id,
            session.owner,
            session.work_session_id,
            session.deadline,
            session,
            self.work_sessions.get(session.work_session_id or ""),
        )
        existing = store.job(job_id)
        if existing:
            if existing["plan_hash"] != request.plan_hash:
                raise BrokerError("job_id_conflict")
            return {**existing, "deduplicated": True}
        if self._execution_uncertain():
            raise BrokerError("recovery_review_required")
        if self._execution_capacity_full():
            raise BrokerError("queue_full")
        if store.record_count("jobs") >= 10000:
            raise BrokerError("history_full_archive_offline")
        if phase == "admit" and payload["test"] in {t.value for t in FLASH_TESTS}:
            return None
        binding = self._runtime_binding()
        flash_binding: FlashBinding | None
        if prefetched is None:
            flash_binding = self._verify_execution_inputs(request.plan_hash, session, payload)
        else:
            memo, snapshot = prefetched
            # Fresh durable checks over the memoized reads only; any new file
            # work would need an await before the enqueue, so refuse instead.
            done, flash_binding = complete(
                partial(self._verify_execution_inputs, request.plan_hash, session, payload),
                memo,
            )
            if not done or flash_binding != snapshot:
                raise BrokerError("execution_intent_not_available")
        # The principal callback, then the callback-free final identity/lease/hash
        # checks; no awaits until the enqueue (execution_authority ENQUEUE).
        refused = self.authority.check(
            Checkpoint.ENQUEUE,
            Gate(
                store=store,
                job_id=job_id,
                plan_hash=request.plan_hash,
                plan=payload,
                lease=session,
                intent=intent,
                runtime_binding=binding,
                flash_binding=flash_binding,
            ),
        )
        if refused is not None:
            raise BrokerError(refused)
        if session.principal_id is None:  # refused above; this only narrows the type
            raise BrokerError("principal_not_authorized")
        store.enqueue_requested(job_id, request.plan_hash, session.principal_id)
        if session.chain_ticket is not None:
            self.tickets.note_consumer(session.chain_ticket, job_id)
        self._execution_intents[job_id] = intent
        self._execution_bindings[job_id] = binding
        if flash_binding is not None:
            self._flash_bindings[job_id] = flash_binding
        self.queue.put_nowait(job_id)
        self.logs.append(Event.QUEUED)
        return {**self.job(job_id), "deduplicated": False}

    def cancel(self, request: CancelRequest) -> dict[str, Any]:
        # A running job keeps its lease past the deadline; its owner can still stop it.
        session = self._lease(request.session, running_job=True)
        job = self.job(request.job_id)
        plan = self._store().plan(job["plan_hash"])
        if not plan or plan["session_id"] != session.id:
            raise BrokerError("job_session_mismatch")
        stop = device_stop(job["state"], plan["test"])
        if stop is DeviceStop.INTENT:
            intent = self._execution_intents.get(request.job_id)
            if intent is None or not self._execution_intent_live(intent):
                raise BrokerError("execution_intent_not_available")
            self._cancel_execution_intent(intent, "client_cancelled_awaiting_approval")
            return self.job(request.job_id)
        if stop is DeviceStop.APP_EVENT:
            app_run = self.app_sessions.jobs.get(request.job_id)
            if app_run is None:
                raise BrokerError("app_session_not_available")
            app_run.cancelled.set()
            return {**job, "stop_requested": True, "requires_review": False}
        if stop is DeviceStop.SOURCE:
            accepted = self.firmware_source.request_stop(request.job_id)
            return {**job, "stop_requested": accepted, "requires_review": accepted}
        if stop is DeviceStop.SMOKE:
            from .serial_backend import SerialBackend

            if not isinstance(self.backend, SerialBackend):
                raise BrokerError("backend_not_available")
            accepted = self.backend.request_smoke_stop()
            return {**job, "stop_requested": accepted, "requires_review": accepted}
        if stop in (DeviceStop.BUILD, DeviceStop.FLASH):
            controller = self.firmware_build if stop is DeviceStop.BUILD else self.firmware_flash
            accepted = controller.request_stop(request.job_id)
            return {**job, "stop_requested": accepted, "requires_review": accepted}
        if stop is DeviceStop.REFUSE:
            raise BrokerError("only_queued_jobs_can_be_cancelled")
        if self._store().ledger.transition(
            request.job_id, "cancelled", "client_cancelled_queued", expect={"queued"}
        ):
            self.logs.append(Event.CANCELLED)
        self._execution_intents.pop(request.job_id, None)
        return self.job(request.job_id)

    def _prune_captures(self) -> None:
        now = self.clock()
        for job_id, retained in tuple(self.captures.items()):
            if now >= retained.deadline:
                del self.captures[job_id]
                self.operator_changes.notify()

    def _prune_device_observations(self) -> None:
        now = self.clock()
        for job_id, retained in tuple(self.device_observations.items()):
            if not math.isfinite(now) or now >= retained.deadline:
                self.device_observations.pop(job_id, None)
                self.operator_changes.notify()

    def _device_observation(self, job_id: str) -> RetainedDeviceObservation:
        self._store()
        self._prune_device_observations()
        retained = self.device_observations.get(job_id)
        job = self._store().job(job_id)
        if self.closing or retained is None or job is None or job["state"] != "succeeded":
            raise BrokerError("device_logs_not_available")
        try:
            authorized = (
                self.principal_valid is not None
                and self.principal_valid(
                    retained.principal_id, retained.origin, frozenset({"read"})
                )
                is True
            )
        except Exception:
            authorized = False
        now = self.clock()
        current_job = None if self.closing else self._store().job(job_id)
        if (
            not authorized
            or self.closing
            or current_job is None
            or current_job["state"] != "succeeded"
            or not math.isfinite(now)
            or now >= retained.deadline
            or self.device_observations.get(job_id) is not retained
        ):
            if self.device_observations.get(job_id) is retained:
                self.device_observations.pop(job_id, None)
                self.operator_changes.notify()
            raise BrokerError("device_logs_not_available")
        return retained

    def _open_job_log(self, job_id: str, principal_id: str, kind: str) -> JobLog | None:
        """Open one job's durable log; prune never removes queued/running jobs."""
        if not principal_id:
            self._job_log_failures.add(job_id)
            return None
        try:
            store = self._store()
            active = {
                str(row[0])
                for row in store.db.execute(
                    "SELECT id FROM jobs WHERE state IN ('queued','running','awaiting_approval')"
                )
            }
            return self.job_logs.begin(
                job_id, principal_id, kind, keep=active, tiers=self._job_log_tiers(store)
            )
        except Exception:
            self._job_log_failures.add(job_id)
            return None

    @staticmethod
    def _job_log_tiers(store: Store) -> dict[str, int]:
        """Retention value per job for log pruning; unlisted ids rank 0.

        0: succeeded non-flash job (evicted first); 1: other ordinary jobs;
        2: needs_review, any flash/backup/restore job, or a job that produced an LKG.
        """
        flash = tuple(test.value for test in FLASH_TESTS)
        tiers: dict[str, int] = {}
        try:
            rows = store.db.execute(
                "SELECT j.id, j.state, json_extract(p.payload,'$.test') FROM jobs j "
                "LEFT JOIN plans p ON p.hash=j.plan_hash"
            ).fetchall()
            for job_id, state, test in rows:
                if state == "needs_review" or test in flash:
                    tiers[str(job_id)] = 2
                elif state != "succeeded":
                    tiers[str(job_id)] = 1
            for (job_id,) in store.db.execute(
                "SELECT completed_job FROM firmware_lkg_history "
                "UNION SELECT completed_job FROM firmware_lkg"
            ).fetchall():
                tiers[str(job_id)] = 2
        except Exception:
            # Ranking only orders eviction; plain oldest-first stays safe.
            return tiers
        return tiers

    def _open_device_job_log(
        self, job_id: str, principal_id: str | None, test: str
    ) -> tuple[JobLog | None, DeviceLogTap | None]:
        """Unconditionally record what the serial reader receives for a device job.

        Passive only: the backend tees bytes it already read. A store failure
        never fails the job; it is recorded as ``job_log_unavailable``. An app
        session's own control link tees its events/logs into the returned log.
        """
        if test == TestName.APP_SESSION.value:
            return self._open_job_log(job_id, principal_id or "", test), None
        if test in _NON_DEVICE_LOG_TESTS or not hasattr(self.backend, "device_log"):
            return None, None
        job_log = self._open_job_log(job_id, principal_id or "", test)
        if job_log is None:
            return None, None
        tap = DeviceLogTap(job_log)
        self.backend.device_log = tap
        return job_log, tap

    def _close_job_logs(
        self, job_id: str, job_log: JobLog | None, tap: DeviceLogTap | None
    ) -> None:
        try:
            if hasattr(self.backend, "device_log"):
                self.backend.device_log = None
            if tap is not None:
                tap.finish()
            durable_logs = [job_log] + [
                getattr(module.logs.get(job_id), "durable", None)
                for module in (self.firmware_build, self.firmware_source, self.firmware_flash)
            ]
            for durable in durable_logs:
                if durable is not None:
                    durable.close()
                    if durable.unavailable:
                        self._job_log_failures.add(job_id)
            if job_id in self._job_log_failures:
                self._job_log_failures.discard(job_id)
                store = self._store()
                job = store.job(job_id)
                plan = store.plan(job["plan_hash"]) if job is not None else None
                # Flash records are committed elsewhere and are left byte-identical.
                if (
                    job is not None
                    and job["state"] not in ("queued", "running")
                    and plan is not None
                    and plan.get("test") not in {test.value for test in FLASH_TESTS}
                ):
                    # Same-state annotation: never changes the committed state/result.
                    store.ledger.annotate(job_id, {"job_log_unavailable": True})
        except Exception:
            # Logging is never allowed to change the job outcome.
            pass

    def _record_flash_outcome(self, job_id: str, plan: dict[str, Any]) -> None:
        """Persist the structured outcome of every flash/backup/restore the worker ran.

        Runs after the terminal transition and before the job's logs close; it
        only reads committed state and never changes the job outcome.
        """
        try:
            if plan.get("test") not in {test.value for test in FLASH_TESTS}:
                return
            job = self._store().job(job_id)
            if job is None:
                return
            if not self.firmware_flash.record_outcome(plan, job):
                self._job_log_failures.add(job_id)
        except Exception:
            pass

    def _durable_device_logs(self, job_id: str, principal_id: str) -> dict[str, Any]:
        """Owner-only persisted device text for any terminal job state."""
        job = None if self.closing else self._store().job(job_id)
        if (
            job is None
            or job["state"] not in _TERMINAL_LOG_STATES
            or not isinstance(principal_id, str)
            or not principal_id
        ):
            raise BrokerError("device_logs_not_available")
        owner = self.job_principal(job_id)
        if owner is not None and not secrets.compare_digest(owner, principal_id):
            raise BrokerError("device_logs_not_available")
        report = self.job_logs.device_report(job_id, principal_id)
        if report is None:
            raise BrokerError("device_logs_not_available")
        return {**report, "state": job["state"], "result": job["result"]}

    def read_device_logs(self, job_id: str, principal_id: str) -> dict[str, Any]:
        """Read only the requesting principal's own device log for a finished job.

        A retained RAM report (succeeded observe/restart) is returned as before;
        otherwise the durable per-job log serves any terminal state.
        """
        try:
            retained = self._device_observation(job_id)
        except BrokerError:
            return self._durable_device_logs(job_id, principal_id)
        if not secrets.compare_digest(retained.principal_id, principal_id):
            raise BrokerError("device_logs_not_available")
        return {
            "job_id": job_id,
            "sha256": retained.observation.sha256,
            "remaining_seconds": max(0, retained.deadline - self.clock()),
            **retained.observation.payload(),
        }

    def operator_device_observation_metadata(self, job_id: str) -> dict[str, Any] | None:
        """No console text enters the timeline, durable history or public event cache."""
        try:
            retained = self._device_observation(job_id)
        except BrokerError:
            return None
        return {
            "job_id": job_id,
            "sha256": retained.observation.sha256,
            "recipient_principal": retained.principal_id,
            "remaining_seconds": max(0, retained.deadline - self.clock()),
            "metadata": retained.observation.metadata(),
        }

    def _durable_capture(self, job_id: str, principal_id: str) -> CapturedImage:
        """The persisted copy of a succeeded capture, with the RAM read's checks."""
        job = self._store().job(job_id)
        plan = self._store().plan(job["plan_hash"]) if job is not None else None
        if (
            self.closing
            or job is None
            or plan is None
            or plan.get("test") not in (TestName.CAPTURE.value, TestName.DERBY.value)
            or job["state"] != "succeeded"
            or not isinstance(principal_id, str)
            or not principal_id
        ):
            raise BrokerError("capture_not_available")
        loaded = self.capture_store.load(job_id, principal_id)
        if loaded is None:
            raise BrokerError("capture_not_available")
        image, metadata = loaded
        epoch = self.epoch
        valid = self.principal_valid
        try:
            authorized = (
                valid is not None
                and valid(principal_id, metadata["origin"], frozenset({"read"})) is True
            )
        except Exception:
            authorized = False
        if not authorized or self.principal_valid is not valid or self.epoch != epoch:
            raise BrokerError("capture_not_available")
        if self.closing:
            raise BrokerError("capture_not_available")
        return image

    def read_capture(self, job_id: str, principal_id: str) -> CapturedImage:
        """Read completed pixels after per-request authentication.

        The five-minute RAM copy is served while retained; afterwards (expiry,
        eviction or restart) the saved copy under the state dir, until the
        capture store's count/byte caps prune it.
        """
        self._store()
        self._prune_captures()
        retained = self.captures.get(job_id)
        if retained is None:
            return self._durable_capture(job_id, principal_id)
        job = self._store().job(job_id)
        if (
            self.closing
            or retained is None
            or job is None
            or job["state"] != "succeeded"
            or not secrets.compare_digest(retained.principal_id, principal_id)
        ):
            raise BrokerError("capture_not_available")
        epoch = self.epoch
        valid = self.principal_valid
        try:
            authorized = (
                valid is not None
                and valid(principal_id, retained.origin, frozenset({"read"})) is True
            )
        except Exception:
            authorized = False
        if not authorized or self.principal_valid is not valid or self.epoch != epoch:
            raise BrokerError("capture_not_available")
        if self._operator_capture(job_id) is not retained:
            raise BrokerError("capture_not_available")
        return retained.image

    def _operator_capture(self, job_id: str) -> RetainedCapture:
        self._store()
        self._prune_captures()
        retained = self.captures.get(job_id)
        job = self._store().job(job_id)
        if self.closing or retained is None or job is None or job["state"] != "succeeded":
            raise BrokerError("capture_not_available")
        return retained

    def _delivery_revision(self, job_id: str, retained: RetainedCapture) -> str:
        return digest_plan(
            {
                "broker_epoch": self.epoch,
                "job_id": job_id,
                "sha256": retained.image.sha256,
                "principal_id": retained.principal_id,
                "origin": retained.origin,
                "born_utc": retained.born_utc,
                "deadline": retained.deadline,
            }
        )

    def _image_summary(self, job_id: str, retained: RetainedCapture) -> dict[str, Any]:
        return {
            "job_id": job_id,
            "sha256": retained.image.sha256,
            "source": retained.image.source,
            "recipient_principal": retained.principal_id,
            "born_utc": retained.born_utc,
            "remaining_seconds": max(0, round(retained.deadline - self.clock(), 3)),
            "delivery_state": retained.delivery_state,
            "policy_allowed": False,
        }

    def operator_images(self) -> dict[str, Any]:
        self._store()
        self._prune_captures()
        if self.closing:
            raise BrokerError("broker_closing")
        images = []
        for job_id, retained in self.captures.items():
            job = self._store().job(job_id)
            if job is not None and job["state"] == "succeeded":
                images.append(self._image_summary(job_id, retained))
        return {"broker_epoch": self.epoch, "images": images}

    def operator_saved_image_summary(self, job_id: str) -> dict[str, Any] | None:
        """Summary of a succeeded job's saved capture for the local GUI, or None.

        Metadata only: no pixels, origin or storage path. Saved copies do not
        expire on a timer, so ``remaining_seconds`` is None and ``retention``
        says ``saved``; retention caps may still prune them.
        """
        job = self._store().job(job_id)
        plan = self._store().plan(job["plan_hash"]) if job is not None else None
        if (
            self.closing
            or job is None
            or plan is None
            or plan.get("test") not in (TestName.CAPTURE.value, TestName.DERBY.value)
            or job["state"] != "succeeded"
        ):
            return None
        metadata = self.capture_store.summary(job_id)
        if metadata is None:
            return None
        owner = self.job_principal(job_id)
        if owner is not None and not secrets.compare_digest(owner, metadata["principal_id"]):
            return None
        return {
            "job_id": job_id,
            "sha256": metadata["sha256"],
            "source": metadata["source"],
            "recipient_principal": metadata["principal_id"],
            "born_utc": metadata["captured_utc"],
            "remaining_seconds": None,
            "retention": "saved",
            "delivery_state": "available",
            "policy_allowed": False,
        }

    def _operator_saved_image(self, job_id: str) -> dict[str, Any]:
        summary = self.operator_saved_image_summary(job_id)
        if summary is None:
            raise BrokerError("capture_not_available")
        loaded = self.capture_store.load(job_id, summary["recipient_principal"])
        if loaded is None or loaded[0].sha256 != summary["sha256"]:
            raise BrokerError("capture_not_available")
        image = loaded[0]
        if self.closing:
            raise BrokerError("capture_not_available")
        return {
            "broker_epoch": self.epoch,
            **summary,
            "metadata": image.metadata(),
            "image_base64": base64.b64encode(image.png).decode("ascii"),
            "delivery_revision": digest_plan(
                {
                    "broker_epoch": self.epoch,
                    "job_id": job_id,
                    "sha256": image.sha256,
                    "principal_id": summary["recipient_principal"],
                    "born_utc": summary["born_utc"],
                    "retention": "saved",
                }
            ),
        }

    def operator_image(self, job_id: str) -> dict[str, Any]:
        """The RAM capture while retained, else the job's saved copy (e.g. after restart)."""
        self._store()
        self._prune_captures()
        if job_id not in self.captures:
            return self._operator_saved_image(job_id)
        retained = self._operator_capture(job_id)
        job = self._store().job(job_id)
        plan = self._store().plan(job["plan_hash"]) if job else None
        provenance = {}
        if plan and plan["test"] == TestName.DERBY.value:
            if (
                job is None
                or plan["broker_epoch"] != self.epoch
                or digest_plan(plan) != job["plan_hash"]
            ):
                raise BrokerError("stale_image_preview")
            provenance = {
                "derby_scope": {
                    "broker_epoch": self.epoch,
                    "job_id": job_id,
                    "plan_hash": digest_plan(plan),
                    "principal_id": retained.principal_id,
                    "device_fingerprint": plan["device_fingerprint"],
                    "expected_firmware_sha256": plan["expected_firmware_sha256"],
                }
            }
        result = {
            "broker_epoch": self.epoch,
            **provenance,
            **self._image_summary(job_id, retained),
            "metadata": retained.image.metadata(),
            "image_base64": base64.b64encode(retained.image.png).decode("ascii"),
            "delivery_revision": self._delivery_revision(job_id, retained),
        }
        if provenance and (
            plan is None
            or self._operator_capture(job_id) is not retained
            or plan["broker_epoch"] != self.epoch
        ):
            raise BrokerError("stale_image_preview")
        return result

    def operator_decide_image(
        self, job_id: str, sha256: str, delivery_revision: str, decision: str
    ) -> dict[str, Any]:
        raise BrokerError("approval_interface_retired")

    def job(self, job_id: str) -> dict[str, Any]:
        job = self._store().job(job_id)
        if job is None:
            raise BrokerError("job_not_found")
        return job

    def app_command(self, request: AppCommandRequest) -> dict[str, Any]:
        lease = self._lease(request.session)
        run = self.app_sessions.jobs.get(request.job_id)
        job = self._store().job(request.job_id)
        plan = self._store().plan(job["plan_hash"]) if job else None
        if (
            run is None
            or plan is None
            or plan.get("test") != TestName.APP_SESSION.value
            or plan.get("broker_epoch") != self.epoch
            or plan.get("session_id") != lease.id
            or run.principal != lease.principal_id
            or (request.operation != "status" and self.active_job != request.job_id)
        ):
            raise BrokerError("app_session_not_available")
        valid = self.principal_valid
        try:
            authorized = (
                valid is not None
                and valid(run.principal, lease.owner, frozenset({"read", "control"})) is True
            )
        except Exception:
            authorized = False
        if not authorized:
            raise BrokerError("principal_not_authorized")
        if self.session is not lease or self.clock() >= lease.deadline or self.closing:
            raise BrokerError("app_session_not_available")
        try:
            return run.command(request)
        except ValueError as exc:
            raise BrokerError(str(exc)) from None

    def read_app_events(self, request: AppEventsRequest, principal_id: str) -> dict[str, Any]:
        """RAM events while retained; after expiry/eviction/restart the durable copy."""
        run = self.app_sessions.jobs.get(request.job_id)
        if self.closing:
            raise BrokerError("app_events_not_available")
        if run is None:
            return self._durable_app_events(request, principal_id)
        if run.principal != principal_id:
            raise BrokerError("app_events_not_available")
        try:
            result = run.events.read(principal_id, request)
        except ValueError as exc:
            raise BrokerError(str(exc)) from None
        if result["expired"] and not run.running:
            try:
                return self._durable_app_events(request, principal_id)
            except BrokerError:
                return result
        return result

    def _durable_app_events(self, request: AppEventsRequest, principal_id: str) -> dict[str, Any]:
        """Owner-only persisted app session events/logs for a terminal job."""
        job = self._store().job(request.job_id)
        plan = self._store().plan(job["plan_hash"]) if job is not None else None
        if (
            job is None
            or plan is None
            or plan.get("test") != TestName.APP_SESSION.value
            or job["state"] not in _TERMINAL_LOG_STATES
            or not isinstance(principal_id, str)
            or not principal_id
        ):
            raise BrokerError("app_events_not_available")
        owner = self.job_principal(request.job_id)
        if owner is not None and not secrets.compare_digest(owner, principal_id):
            raise BrokerError("app_events_not_available")
        try:
            result = self.job_logs.app_events_read(request.job_id, principal_id, request)
        except ValueError as exc:
            raise BrokerError(str(exc)) from None
        if result is None:
            raise BrokerError("app_events_not_available")
        return {**result, "state": job["state"], "result": job["result"]}

    def read_logs(self, request: LogRequest) -> dict[str, Any]:
        return self.logs.read(request)

    async def _prefetch_flash_binding(self, job_id: str) -> IOMemo | None:
        store = self._store()
        binding = self._flash_bindings.get(job_id)
        row = store.job(job_id) if binding is not None else None
        plan = store.plan(row["plan_hash"]) if row is not None else None
        if binding is None or row is None or row["state"] != "queued" or plan is None:
            return None
        memo = IOMemo()
        async with self._file_verification:
            await offload(partial(validate_binding, self, binding, plan), memo)
        return memo

    def _flash_binding_current(
        self, binding: FlashBinding, plan: dict[str, Any], reads: IOMemo | None
    ) -> bool:
        if reads is None:
            return validate_binding(self, binding, plan)
        done, valid = complete(partial(validate_binding, self, binding, plan), reads)
        return done and valid is True

    async def _work(self) -> None:
        """The single device worker: ``admit`` → the kind's executor → ``finish``.

        Only the queue read, the flash-binding prefetch and the executor await.
        ``admit``, ``_accept_result``, ``finish`` and ``_close_admitted`` are
        synchronous sections (DESIGN-REDESIGN §3, stage 3a).
        """
        while True:
            job_id = await self.queue.get()
            try:
                if job_id is None:
                    return
                # Pure file verification for a flash binding runs in worker threads
                # before any check below; those checks then run synchronously
                # through the running transition, using only the memoized reads.
                binding_reads = await self._prefetch_flash_binding(job_id)
                run = self.admit(job_id, binding_reads)
                if run is None:
                    continue
                run.job_log, run.device_tap = self._open_device_job_log(
                    job_id, run.capture_principal, run.test
                )
                result: str | BackendResult = ""
                try:
                    try:
                        result = await kind_for(run.test).execute(self, run)
                        self._accept_result(run, result)
                    except Exception as failure:
                        self.finish(run, result, failure)
                    else:
                        self.finish(run, result, None)
                finally:
                    # Do not keep the last image alive in this suspended worker frame
                    # after the bounded cache expires or is cleared.
                    result = ""
                    self._close_admitted(run)
            except Exception:
                # A failed durable-state operation must not leave an apparently
                # idle lane admitting more work. History is not the runtime latch.
                self._latch_execution("durable_state_operation_failed")
                self.logs.append(Event.REVIEW)
            finally:
                self._worker_job = None
                if job_id is not None:
                    self._flash_bindings.pop(job_id, None)
                    self._execution_bindings.pop(job_id, None)
                    self._execution_intents.pop(job_id, None)
                self.queue.task_done()

    @sync_section
    def admit(self, job_id: str, binding_reads: IOMemo | None) -> AdmittedJob | None:
        """Admission: every check, then the ``running`` transition, in one loop step.

        Returns None when the job is skipped or was given a terminal reason here.
        The order of the checks is part of the contract (jobs.md section 5). No
        ``await`` may appear in this function (tests/test_stage3a_sync_sections.py).
        """
        store = self._store()
        job = self.job(job_id)
        if job["state"] != "queued":
            return None
        queued_intent = self._execution_intents.get(job_id)
        if queued_intent is None:
            store.ledger.transition(
                job_id, "cancelled", "execution_intent_not_available", expect={"queued"}
            )
            return None
        queued_lease = self.session.id if self.session is not None else None
        self._expire()
        if self.job(job_id)["state"] != "queued":
            return None
        if (
            self.session is None
            or self.session.id != queued_lease
            or self.clock() >= self.session.deadline
            or self.closing
        ):
            store.ledger.transition(job_id, "cancelled", "session_not_available", expect={"queued"})
            return None
        plan = store.plan(job["plan_hash"])
        if plan is None or plan["session_id"] != self.session.id:
            store.ledger.transition(job_id, "needs_review", "plan_not_available", expect={"queued"})
            self._cancel_queued("recovery_review_required")
            return None
        lease = self.session
        if plan["test"] in (TestName.FIRMWARE.value, TestName.RESTART.value) and plan.get(
            "log_delivery"
        ) != self._log_delivery(lease):
            if store.ledger.transition(
                job_id,
                "cancelled",
                "principal_or_lease_no_longer_valid",
                expect={"queued"},
            ):
                self.logs.append(Event.CANCELLED)
            return None
        if lease.principal_id is not None and self.principal_valid is not None:
            try:
                live = self.principal_valid(
                    lease.principal_id, lease.owner, frozenset({"read", "control"})
                )
            except Exception:
                live = False
            if live is not True or self.session is not lease or self.clock() >= lease.deadline:
                # The callback may already have committed a terminal reason;
                # the first committed reason is kept (DESIGN-REDESIGN 2.1).
                if store.ledger.transition(
                    job_id,
                    "cancelled",
                    "principal_or_lease_no_longer_valid",
                    expect={"queued"},
                ):
                    self.logs.append(Event.CANCELLED)
                return None
        current_valid = self.principal_valid
        current_binding = self._execution_bindings.get(job_id)
        flash_binding = self._flash_bindings.get(job_id)
        if flash_binding is not None and not self._flash_binding_current(
            flash_binding, plan, binding_reads
        ):
            self._cancel_execution_intent(queued_intent, "flash_binding_invalid")
            return None
        if "flash_job_id" in plan:
            try:
                fresh = prepare_collector(self, plan["flash_job_id"], lease.principal_id or "")
                if any(plan.get(key) != value for key, value in fresh.items()):
                    raise ValueError("collector_binding_changed")
            except Exception:
                self._cancel_execution_intent(queued_intent, "collector_binding_changed")
                return None
        # The last principal callback, then the durable row and every binding again
        # before any backend work starts: the callbacks above can change the queued
        # row, the original intent, the lease or the clock (execution_authority DISPATCH).
        refused = self.authority.check(
            Checkpoint.DISPATCH,
            Gate(
                store=store,
                job_id=job_id,
                plan_hash=job["plan_hash"],
                plan=plan,
                lease=lease,
                intent=queued_intent,
                runtime_binding=current_binding,
                flash_binding=flash_binding,
                valid=current_valid,
                queued_lease=queued_lease,
            ),
        )
        if refused is not None:
            self._cancel_execution_intent(queued_intent, refused)
            return None
        self._execution_intents.pop(job_id, None)
        if plan["test"] == TestName.BUILD.value:
            self.firmware_build.begin()
        elif plan["test"] == TestName.SOURCE.value:
            self.firmware_source.begin()
        self.active_job = job_id
        self._worker_job = job_id
        capture_principal = self.session.principal_id
        if not store.ledger.transition(job_id, "running"):
            # Unreachable after the final synchronous check; never run unrecorded.
            raise RuntimeError("job_transition_refused")
        derby_scope = (
            capture_provenance(self, plan, job_id, capture_principal or "")
            if plan["test"] == TestName.DERBY.value
            else None
        )
        return AdmittedJob(
            job_id=job_id,
            store=store,
            job=job,
            plan=plan,
            lease=lease,
            queued_intent=queued_intent,
            current_binding=current_binding,
            flash_binding=flash_binding,
            capture_principal=capture_principal,
            derby_scope=derby_scope,
        )

    # --- callbacks handed to executors; each reads only the admitted job ---------------

    def _on_stage(self, run: AdmittedJob, name: str) -> None:
        run.store.set_stage(run.job_id, name)
        self.logs.append(Event.STAGE, stage=name)

    def _derby_diagnostic(
        self, run: AdmittedJob, diagnostic: dict[str, Any] | None
    ) -> dict[str, Any] | None:
        if diagnostic is None or run.derby_scope is None:
            return diagnostic
        scoped = dict(diagnostic)
        # Freeze observed dispatch/completion evidence. Reading a
        # historic report never consults a newer firmware target.
        scoped[PROVENANCE_KEY] = finish_provenance(
            run.derby_scope,
            capture_provenance(self, run.plan, run.job_id, run.capture_principal or ""),
        ).model_dump(mode="json", exclude_none=True)
        return scoped

    def _running_authorized(self, run: AdmittedJob) -> bool:
        """The RUNNING checkpoint: non-flash executors and their commits."""
        return self.authority.allows(Checkpoint.RUNNING, Gate.admitted(run))

    def _flash_live(self, run: AdmittedJob, committing: bool = False) -> bool:
        """FLASH_IO between device side effects; FLASH_COMMIT inside the final commit."""
        checkpoint = Checkpoint.FLASH_COMMIT if committing else Checkpoint.FLASH_IO
        return self.authority.allows(checkpoint, Gate.admitted(run))

    # --- after the executor: synchronous sections ---------------------------------------

    @sync_section
    def _accept_result(self, run: AdmittedJob, result: str | BackendResult) -> None:
        """Validate the executor's result and retain private output, or raise."""
        job_id, plan, lease = run.job_id, run.plan, run.lease
        capture_principal = run.capture_principal
        if plan["test"] == TestName.DERBY.value:
            from .derby_observation import NO_CAPTURE, DerbyObservation

            if (
                not isinstance(result, BackendResult)
                or not isinstance(result.derby_observation, DerbyObservation)
                or not result.derby_observation.exit_home_observed
                or result.derby_observation.image_acquired is not (result.image is not None)
                or (
                    result.image is None
                    and (
                        result.code != NO_CAPTURE
                        or not result.derby_observation.no_capture_completed()
                    )
                )
            ):
                raise BrokerError("capture_result_invalid")
        if plan["test"] == TestName.DERBY_FIXED.value and (
            not isinstance(result, BackendResult)
            or result.fixed_trial is None
            or not result.fixed_trial.valid
            or result.image is not None
        ):
            raise BrokerError("fixed_trial_result_invalid")
        if plan["test"] == TestName.CAPTURE.value and (
            not isinstance(result, BackendResult) or result.image is None
        ):
            raise BrokerError("capture_result_invalid")
        if plan["test"] in (TestName.FIRMWARE.value, TestName.RESTART.value) and (
            not isinstance(result, BackendResult) or result.observation is None
        ):
            raise BrokerError("device_log_result_invalid")
        if isinstance(result, BackendResult) and result.observation is not None:
            if (
                plan["test"] not in (TestName.FIRMWARE.value, TestName.RESTART.value)
                or capture_principal is None
                or self.backend.active
                or self.backend.hardware_connected
                or getattr(self.backend, "ownership_uncertain", False)
                or self.session is not lease
                or plan.get("log_delivery") != self._log_delivery(lease)
                or result.observation.reset_sequence_issued
                is not (plan["test"] == TestName.RESTART.value)
                or type(result.observation.capture_gap) is not bool
                or (plan["test"] != TestName.RESTART.value and result.observation.capture_gap)
            ):
                raise BrokerError("device_log_result_invalid")
            # Shutdown may be waiting on another service while this
            # worker finishes. Do not republish private output then.
            if not self.closing:
                # Only the newest report is retained. No text is written to SQLite.
                self.device_observations.clear()
                self.device_observations[job_id] = RetainedDeviceObservation(
                    capture_principal,
                    lease.owner,
                    result.observation,
                    self.clock() + 300,
                )
        if isinstance(result, BackendResult) and result.image is not None:
            if (
                plan["test"] not in (TestName.CAPTURE.value, TestName.DERBY.value)
                or capture_principal is None
                or self.backend.active
                or self.backend.hardware_connected
                or getattr(self.backend, "ownership_uncertain", False)
                or not 0 < len(result.image.png) <= 200_000
                or result.image.source
                != ("synthetic_fixture" if self.backend.name == "mock" else "render_buffer")
            ):
                raise BrokerError("capture_result_invalid")
            # Durable copy first, unconditionally (also while closing);
            # it is served only after the job commits as succeeded.
            self.capture_store.save(
                job_id,
                capture_principal,
                self.session.owner if self.session is not None else "",
                result.image,
            )
            if not self.closing:
                self._prune_captures()
                while len(self.captures) >= 8:
                    del self.captures[next(iter(self.captures))]
                self.captures[job_id] = RetainedCapture(
                    capture_principal,
                    result.image,
                    self.clock() + 300,
                    self.session.owner if self.session is not None else "",
                    datetime.now(UTC).isoformat(),
                    delivery_state="available",
                )

    @sync_section
    def finish(
        self,
        run: AdmittedJob,
        result: str | BackendResult,
        failure: Exception | None,
    ) -> None:
        """Terminal handling for an admitted job, in one loop step.

        With ``failure``, map it to its code (the order of the checks is the order
        of the former ``except`` clauses). Otherwise run the kind's synchronous
        commit; a commit that raises is reviewed and latches the lane.
        """
        if failure is not None:
            self._finish_failure(run, failure)
        else:
            self._finish_success(run, result)

    @sync_section
    def _finish_failure(self, run: AdmittedJob, failure: Exception) -> None:
        job_id, plan, store = run.job_id, run.plan, run.store
        capture_principal = run.capture_principal
        if isinstance(failure, (FlashFailure, FlashAdmissionError)):
            self._record_failure(
                job_id,
                str(failure),
                _bounded_read_diagnostic(failure.code, failure.diagnostic)
                if isinstance(failure, FlashFailure)
                else None,
                self._failure_evidence(plan, job_id, str(failure)),
            )
        elif isinstance(failure, (BuildPreflightFailure, BuildLocationError)):
            self._record_review(
                job_id,
                failure.code,
                failure.public_diagnostic()
                if isinstance(failure, BuildPreflightFailure)
                else {"code": failure.code},
            )
            self.logs.append(Event.REVIEW)
            self._cancel_queued("recovery_review_required")
        elif isinstance(failure, SourceError):
            if plan["test"] == TestName.SOURCE.value:
                self.firmware_source.record_failure(plan, failure.code)
                self._record_review(job_id, failure.code, {"code": failure.code})
            else:
                self._record_review(job_id, "backend_error_no_retry")
            self.logs.append(Event.REVIEW)
            self._cancel_queued("recovery_review_required")
        elif isinstance(failure, DeviceOperationFailure):
            from .serial_backend import SerialFailure

            evidence = self._failure_evidence(plan, job_id, failure.code)
            if (
                plan["test"] == TestName.DERBY_FIXED.value
                and isinstance(failure, SerialFailure)
                and failure.fixed_trial is not None
                and capture_principal is not None
            ):
                outcome = classify_failure(evidence)
                try:
                    store.finish_fixed_trial(
                        job_id,
                        run.job["plan_hash"],
                        capture_principal,
                        outcome,
                        failure.code,
                        failure.fixed_trial,
                        failure.diagnostic,
                    )
                except Exception:
                    self._latch_execution("fixed_trial_persist_failed")
                    self._record_review(job_id, "fixed_trial_persist_failed")
                    outcome = "needs_review"
                if outcome == "failed":
                    self.logs.append(Event.FAILED)
                else:
                    self.logs.append(Event.REVIEW)
                    self._cancel_queued("recovery_review_required")
            else:
                self._record_failure(
                    job_id,
                    failure.code,
                    self._derby_diagnostic(run, failure.diagnostic),
                    evidence,
                )
        elif isinstance(failure, DeviceDisconnected):
            self._record_failure(
                job_id,
                "device_disconnected_no_retry",
                None,
                self._failure_evidence(plan, job_id, "device_disconnected_no_retry"),
            )
        else:
            # Never persist the exception string (may contain credentials).
            if plan["test"] == TestName.SOURCE.value:
                self.firmware_source.record_failure(plan, "source_fetch_failed")
                self._record_review(
                    job_id,
                    "source_fetch_failed",
                    {"code": "source_fetch_failed"},
                )
            else:
                self._record_review(job_id, "backend_error_no_retry")
            self.logs.append(Event.REVIEW)
            self._cancel_queued("recovery_review_required")

    @sync_section
    def _finish_success(self, run: AdmittedJob, result: str | BackendResult) -> None:
        job_id, plan = run.job_id, run.plan
        try:
            completed = result.code if isinstance(result, BackendResult) else result
            terminal_event, fixed_review = kind_for(run.test).commit(self, run, result, completed)
        except Exception as failure:
            self._latch_execution("job_final_commit_failed")
            if plan["test"] == TestName.SOURCE.value:
                code = failure.code if isinstance(failure, SourceError) else "source_fetch_failed"
                self.firmware_source.record_failure(plan, code)
                self._record_review(job_id, code, {"code": code})
            else:
                self._record_review(
                    job_id,
                    "fixed_trial_persist_failed"
                    if plan["test"] == TestName.DERBY_FIXED.value
                    else "firmware_final_commit_failed",
                )
            self.logs.append(Event.REVIEW)
            self._cancel_queued("recovery_review_required")
        else:
            self.logs.append(Event.REVIEW if fixed_review else terminal_event)
            if fixed_review:
                self._cancel_queued("recovery_review_required")

    @sync_section
    def _close_admitted(self, run: AdmittedJob) -> None:
        """Cleanup after every admitted job, on any outcome (also on cancellation)."""
        self._record_flash_outcome(run.job_id, run.plan)
        self._close_job_logs(run.job_id, run.job_log, run.device_tap)
        run.job_log = run.device_tap = None
        # A running registered-project job owns its own source/build activity
        # (as in `_physical_busy`); it must not leave this device job active.
        # A device-lane build/source job still counts its own surviving process.
        profile_owned = self.profile_jobs.active is not None and run.plan["test"] not in (
            TestName.BUILD.value,
            TestName.SOURCE.value,
        )
        if (
            not self.backend.active
            and (profile_owned or not self.firmware_source.active)
            and (profile_owned or not self.firmware_build.process.active)
            and not self.firmware_build.process.uncertain
            and not self.firmware_flash.active
            and not self.firmware_flash.uncertain
        ):
            self.active_job = None
        if self.active_job is None:
            try:
                self.home_recovery.record_closed(
                    run.plan, run.lease.principal_id or "", run.lease.owner
                )
            except Exception:
                # Failure to retain closure evidence never grants recovery.
                pass
        self._expire()
