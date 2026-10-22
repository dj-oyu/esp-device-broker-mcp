"""Single resident owner: independent of MCP connections and client lifetimes."""

import asyncio
import base64
import hashlib
import json
import math
import os
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
from .capture import CapturedImage
from .collector_binding import collector_live, prepare_collector
from .config import Settings
from .derby_provenance import DerbyProvenance, capture_provenance, finish_provenance
from .derby_reports import PROVENANCE_KEY
from .device_observation import DeviceObservation
from .firmware_build import BuildPreflightFailure
from .fixed_diagnostic import DiagnosticContext, finish_fixed_diagnostic
from .flash_binding import FlashBinding, live_binding, prepare_binding, validate_binding
from .flash_lifecycle import FlashFailure, _bounded_read_diagnostic
from .flash_manifest import FlashAdmissionError
from .home_line import HomeLineProbe
from .logs import Event, LogBook
from .models import (
    FLASH_TESTS,
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
from .operator_notifications import OperatorNotifications
from .source_snapshot import SourceError
from .store import Store


class BrokerError(ValueError):
    """Public, fixed-string errors. Do not include exceptions or request content."""


@dataclass
class Session:
    capability: str = field(repr=False)
    id: str
    owner: str
    deadline: float
    principal_id: str | None = None
    work_session_id: str | None = None
    request_id: str | None = None


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


@dataclass(frozen=True, repr=False)
class CaptureDeliveryConsent:
    """Local consent for one future image from this exact reviewed capture."""

    intent: ExecutionIntent
    lease: Session
    work: WorkSession | None
    target: str


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


@dataclass(repr=False)
class LocalHomeDiagnostic:
    """One explicit local approval; neither this grant nor its probe is durable."""

    diagnostic_id: str
    epoch: str
    job_id: str
    plan_hash: str
    principal_id: str
    origin: str
    lease_id: str
    deadline: float
    consumed: bool = False
    invalidated: bool = False
    probe: HomeLineProbe | None = field(default=None, repr=False)


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
        self.session: Session | None = None
        self.principal_valid = principal_valid
        self.app_sessions = AppSessions()
        self.work_sessions: dict[str, WorkSession] = {}
        self.device_requests: dict[str, DeviceWait] = {}
        self.waiters: list[str] = []
        self._queue_sequence = 0
        self.maintenance: asyncio.Task[None] | None = None
        self.active_job: str | None = None
        self.operator_changes = OperatorNotifications()
        self.logs = LogBook(settings.log_capacity, on_change=self.operator_changes.notify)
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
        self.firmware_build.on_logs_change = self.operator_changes.notify_logs
        self.firmware_source.on_logs_change = self.operator_changes.notify_logs
        self.firmware_flash.on_logs_change = self.operator_changes.notify_logs
        self.app_sessions.on_logs_change = self.operator_changes.notify_logs
        self.queue: asyncio.Queue[str | None] = asyncio.Queue(settings.max_queue)
        self.worker: asyncio.Task[None] | None = None
        self.store: Store | None = None
        self.closing = False
        self.execution_uncertain = False
        self.captures: dict[str, RetainedCapture] = {}
        self.capture_delivery_consents: dict[str, CaptureDeliveryConsent] = {}
        self.device_observations: dict[str, RetainedDeviceObservation] = {}
        self._flash_bindings: dict[str, FlashBinding] = {}
        self._execution_bindings: dict[str, str] = {}
        self._execution_intents: dict[str, ExecutionIntent] = {}
        self.local_home_diagnostic: LocalHomeDiagnostic | None = None
        self._device_lock = FileLock(
            str(lock_path or device_lock_path(settings.backend)), timeout=0
        )
        self._state_lock = FileLock(str(settings.state_dir / "broker.lockfile"), timeout=0)
        from .home_recovery import HomeRecovery

        self.home_recovery = HomeRecovery(self)

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
            self.store.recover()
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
        for app_run in self.app_sessions.jobs.values():
            app_run.cancelled.set()
        # Release completed private output before any service shutdown can yield.
        # Denying new reads alone must not leave these bytes retained in RAM.
        self.captures.clear()
        self.capture_delivery_consents.clear()
        self.device_observations.clear()
        await self.home_recovery.close()
        await self.profile_jobs.close()
        self.firmware_build.request_stop()
        self.firmware_flash.request_stop()
        self._clear_local_home_diagnostic()
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
                    store.set_job(job_id, "cancelled", reason)
                    self._flash_bindings.pop(job_id, None)
                    self._execution_bindings.pop(job_id, None)
                    self.capture_delivery_consents.pop(job_id, None)
                    self.logs.append(Event.CANCELLED)
        if stopping:
            self.queue.put_nowait(None)

    def _cancel_execution_intent(self, intent: ExecutionIntent, reason: str) -> None:
        job = self._store().job(intent.job_id)
        if (
            job
            and job["plan_hash"] == intent.plan_hash
            and job["state"] in ("awaiting_approval", "queued")
        ):
            self._store().set_job(intent.job_id, "cancelled", reason)
            self.logs.append(Event.CANCELLED)
        self._execution_intents.pop(intent.job_id, None)
        self._execution_bindings.pop(intent.job_id, None)
        self._flash_bindings.pop(intent.job_id, None)
        self.capture_delivery_consents.pop(intent.job_id, None)

    def _execution_intent_binding_live(self, intent: ExecutionIntent) -> bool:
        """Final callback-free identity/deadline checks; never extend an intent."""
        now = self.clock()
        return bool(
            math.isfinite(now)
            and math.isfinite(intent.deadline)
            and now < intent.deadline
            and self._execution_lease_binding_live(intent)
        )

    def _execution_lease_binding_live(self, intent: ExecutionIntent) -> bool:
        """Current lease authority, independent of the fixed admission deadline."""
        lease, now = self.session, self.clock()
        if (
            self.closing
            or not math.isfinite(now)
            or intent.epoch != self.epoch
            or lease is None
            or not math.isfinite(lease.deadline)
            or now >= lease.deadline
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
                or work.state != "open"
                or not math.isfinite(work.deadline)
                or now >= work.deadline
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
                or not self._execution_lease_binding_live(intent)
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

    def _end_ownership(self, state: str, reason: str) -> None:
        session = self.session
        if session is None:
            return
        if self._physical_busy():
            raise BrokerError("active_job_cannot_release")
        for intent in tuple(self._execution_intents.values()):
            self._cancel_execution_intent(intent, reason)
        if session.request_id is not None:
            self._store().set_request(session.request_id, state, reason)
            request = self.device_requests.get(session.request_id)
            if request is not None:
                request.state = state
        self.session = None
        self.capture_delivery_consents.clear()
        self._queue_sequence += 1
        self.operator_changes.notify()
        self.logs.append(Event.EXPIRED if state == "expired" else Event.RELEASED)

    def maintenance_tick(self) -> None:
        """Single-event-loop transition point. No awaits or hardware opens here."""
        self._prune_captures()
        self._prune_device_observations()
        for app_run in self.app_sessions.jobs.values():
            app_run.events.expire()
        self._prune_local_home_diagnostic()
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
            work = self.work_sessions[request.session_id]
            if work.state != "open" or now >= request.deadline:
                self._end_wait(request, "expired", "wait_expired")
            elif not self._principal_live(work):
                self._end_wait(request, "cancelled", "principal_not_authorized")
        session = self.session
        if session is not None:
            owning_work = self.work_sessions.get(session.work_session_id or "")
            if owning_work is not None and (
                owning_work.state != "open" or not self._principal_live(owning_work)
            ):
                session.deadline = min(session.deadline, now)
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
                    session.deadline = min(session.deadline, now)
            if now >= session.deadline:
                self._cancel_queued("lease_expired")
                if not self._physical_busy():
                    self._end_ownership("expired", "lease_expired")
        self._prune_execution_intents()
        # The approval interface is retired: discard any legacy consent rather
        # than calling its removed validator or reviving delivery authority.
        self.capture_delivery_consents.clear()
        if (
            self.closing
            or self.session is not None
            or self._physical_busy()
            or self._execution_uncertain()
            or not self.waiters
        ):
            return
        request = self.device_requests[self.waiters[0]]
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
        )
        self._store().set_request(request.id, "owning", lease_id=lease.id)
        request.state = "owning"
        request.lease_id = lease.id
        self.waiters.pop(0)
        self.session = lease
        self._queue_sequence += 1
        self.operator_changes.notify()
        self.logs.append(Event.ACQUIRED)

    async def _maintain(self) -> None:
        while not self.closing:
            await asyncio.sleep(0.25)
            self.maintenance_tick()

    def _expire(self) -> None:
        self.maintenance_tick()

    def _lease(self, capability: str | None) -> Session:
        self._expire()
        if self.closing:
            raise BrokerError("broker_closing")
        if (
            capability is None
            or self.session is None
            or not secrets.compare_digest(self.session.capability, capability)
        ):
            raise BrokerError("invalid_session")
        if self.clock() >= self.session.deadline:
            raise BrokerError("lease_expired_active_job_retains_ownership")
        return self.session

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
            "work_session_count": sum(w.state == "open" for w in self.work_sessions.values()),
            "history": store.counts(),
            "needs_review": pending_review,
            "execution_uncertain": uncertain,
            "safety": {
                "current_resource_uncertain": uncertain,
                "owned_work_active": busy,
                "historical_review_pending": pending_review,
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
        if self.session or self.waiters or self._physical_busy():
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
        self.session = lease
        self._queue_sequence += 1
        self.operator_changes.notify()
        self.logs.append(Event.ACQUIRED)
        return {
            "session": self.session.capability,
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
        session.deadline = self.clock() + request.lease_seconds
        work = self.work_sessions.get(session.work_session_id or "")
        if work is not None:
            session.deadline = min(session.deadline, work.deadline)
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
        self._store().put_request(waiting.id, work.id, request.lease_seconds)
        self.device_requests[waiting.id] = waiting
        work.request_id = waiting.id
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
        except (FlashFailure, FlashAdmissionError) as exc:
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
        # Fresh preflight preserves device, firmware and registered-tool binding.
        # It cannot grant authority: only this authenticated request can enqueue.
        from .plan_approval import PlanApprovals

        binding = self._runtime_binding()
        PlanApprovals(self).validate_current(request.plan_hash, session)
        flash_binding = (
            prepare_binding(self, payload)
            if payload["test"] in {t.value for t in FLASH_TESTS}
            else None
        )
        valid = self.principal_valid
        try:
            authorized = (
                session.principal_id is not None
                and valid is not None
                and valid(session.principal_id, session.owner, frozenset({"read", "control"}))
                is True
            )
        except Exception:
            authorized = False
        if not authorized or session.principal_id is None:
            raise BrokerError("principal_not_authorized")
        # No callbacks or awaits after these final identity/lease/hash checks.
        if (
            self.principal_valid is not valid
            or self._runtime_binding() != binding
            or (flash_binding is not None and not live_binding(self, flash_binding, payload))
            or not collector_live(self, payload, session.principal_id or "")
            or self.session is not session
            or not self._execution_intent_binding_live(intent)
            or self.closing
            or self._execution_uncertain()
            or self._execution_capacity_full()
            or store.record_count("jobs") >= 10000
            or store.job(job_id) is not None
            or store.plan(request.plan_hash) != payload
            or not self._state_lock.is_locked
            or not self._device_lock.is_locked
        ):
            raise BrokerError("execution_intent_not_available")
        store.enqueue_requested(job_id, request.plan_hash, session.principal_id)
        self._execution_intents[job_id] = intent
        self._execution_bindings[job_id] = binding
        if flash_binding is not None:
            self._flash_bindings[job_id] = flash_binding
        self.queue.put_nowait(job_id)
        self.logs.append(Event.QUEUED)
        return {**self.job(job_id), "deduplicated": False}

    def dispatch_pending_execution(self, plan_hash: str) -> bool:
        raise BrokerError("approval_interface_retired")

    def cancel(self, request: CancelRequest) -> dict[str, Any]:
        session = self._lease(request.session)
        job = self.job(request.job_id)
        plan = self._store().plan(job["plan_hash"])
        if not plan or plan["session_id"] != session.id:
            raise BrokerError("job_session_mismatch")
        if job["state"] == "awaiting_approval":
            intent = self._execution_intents.get(request.job_id)
            if intent is None or not self._execution_intent_live(intent):
                raise BrokerError("execution_intent_not_available")
            self._cancel_execution_intent(intent, "client_cancelled_awaiting_approval")
            return self.job(request.job_id)
        if job["state"] == "running" and plan["test"] == TestName.APP_SESSION.value:
            app_run = self.app_sessions.jobs.get(request.job_id)
            if app_run is None:
                raise BrokerError("app_session_not_available")
            app_run.cancelled.set()
            return {**job, "stop_requested": True, "requires_review": False}
        if job["state"] == "running" and plan["test"] == TestName.SOURCE.value:
            accepted = self.firmware_source.request_stop(request.job_id)
            return {**job, "stop_requested": accepted, "requires_review": accepted}
        if job["state"] == "running" and plan["test"] in (
            TestName.DERBY.value,
            TestName.DERBY_FIXED.value,
        ):
            from .serial_backend import SerialBackend

            if not isinstance(self.backend, SerialBackend):
                raise BrokerError("backend_not_available")
            accepted = self.backend.request_smoke_stop()
            return {**job, "stop_requested": accepted, "requires_review": accepted}
        if job["state"] == "running" and plan["test"] in (
            {TestName.BUILD.value} | {t.value for t in FLASH_TESTS}
        ):
            controller = (
                self.firmware_build if plan["test"] == TestName.BUILD.value else self.firmware_flash
            )
            accepted = controller.request_stop(request.job_id)
            return {**job, "stop_requested": accepted, "requires_review": accepted}
        if job["state"] != "queued":
            raise BrokerError("only_queued_jobs_can_be_cancelled")
        self._store().set_job(request.job_id, "cancelled", "client_cancelled_queued")
        self._execution_intents.pop(request.job_id, None)
        self.capture_delivery_consents.pop(request.job_id, None)
        self.logs.append(Event.CANCELLED)
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

    def read_device_logs(self, job_id: str, principal_id: str) -> dict[str, Any]:
        """Read only the requesting principal's explicitly requested completed report."""
        retained = self._device_observation(job_id)
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

    def _capture_delivery_target(self) -> str:
        from .serial_backend import SerialBackend

        return digest_plan(
            {
                "backend": self.backend.name,
                "device": self.backend.device,
                "serial": self.settings.serial.model_dump(mode="json")
                if self.settings.serial
                else None,
                "backend_serial": self.backend.config.model_dump(mode="json")
                if isinstance(self.backend, SerialBackend)
                else None,
            }
        )

    def _make_capture_delivery_consent(
        self, plan: dict[str, Any], lease: Session
    ) -> CaptureDeliveryConsent:
        raise BrokerError("approval_interface_retired")

    def read_capture(self, job_id: str, principal_id: str) -> CapturedImage:
        """Read completed pixels from memory only, after per-request authentication."""
        self._store()
        self._prune_captures()
        retained = self.captures.get(job_id)
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

    def _image_policy_allowed(
        self, job_id: str, retained: RetainedCapture, *, record_use: bool = False
    ) -> bool:
        return False

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

    def operator_image(self, job_id: str) -> dict[str, Any]:
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
        run = self.app_sessions.jobs.get(request.job_id)
        if self.closing or run is None or run.principal != principal_id:
            raise BrokerError("app_events_not_available")
        try:
            return run.events.read(principal_id, request)
        except ValueError as exc:
            raise BrokerError(str(exc)) from None

    def read_logs(self, request: LogRequest) -> dict[str, Any]:
        return self.logs.read(request)

    def _home_line_principal_live(self, entry: LocalHomeDiagnostic) -> bool:
        try:
            return (
                self.principal_valid is not None
                and self.principal_valid(
                    entry.principal_id, entry.origin, frozenset({"read", "control"})
                )
                is True
            )
        except Exception:
            return False

    def _clear_local_home_diagnostic(self) -> None:
        entry, self.local_home_diagnostic = self.local_home_diagnostic, None
        if entry is not None and entry.probe is not None:
            entry.probe.clear()

    def _home_line_binding_live(self, entry: LocalHomeDiagnostic, lease: Session | None) -> bool:
        """No callbacks: final time and identity check after principal validation."""
        now = self.clock()
        return bool(
            not self.closing
            and math.isfinite(now)
            and now < entry.deadline
            and entry.epoch == self.epoch
            and lease is not None
            and self.session is lease
            and math.isfinite(lease.deadline)
            and now < lease.deadline
            and lease.id == entry.lease_id
            and lease.principal_id == entry.principal_id
            and lease.owner == entry.origin
        )

    def _prune_local_home_diagnostic(self) -> None:
        entry = self.local_home_diagnostic
        if entry is None:
            return
        if entry.probe is not None:
            entry.probe.prune()
            authorized = self._home_line_principal_live(entry)
            if self.closing or entry.epoch != self.epoch or not authorized:
                entry.probe.clear()
            return
        lease = self.session
        authorized = self._home_line_principal_live(entry)
        if not authorized or not self._home_line_binding_live(entry, lease):
            entry.invalidated = True

    def _make_local_home_diagnostic(
        self, plan: dict[str, Any], lease: Session
    ) -> LocalHomeDiagnostic:
        raise BrokerError("approval_interface_retired")

    def _take_local_home_probe(
        self, plan: dict[str, Any], lease: Session, *, policy_authorized: bool
    ) -> HomeLineProbe | None:
        # The old approval opt-in is retired. Explicit execution never expands
        # raw serial retention or silently inherits an old diagnostic grant.
        self._clear_local_home_diagnostic()
        return None

    def operator_home_line_metadata(self, job_id: str, plan_hash: str) -> dict[str, Any] | None:
        """Metadata only. This method is intentionally absent from MCP surfaces."""
        self._prune_local_home_diagnostic()
        entry = self.local_home_diagnostic
        if (
            entry is None
            or entry.epoch != self.epoch
            or entry.job_id != job_id
            or entry.plan_hash != plan_hash
        ):
            return None
        if entry.probe is not None:
            metadata = entry.probe.metadata()
        else:
            metadata = {
                "state": "expired" if entry.invalidated else "armed",
                "remaining_seconds": 0
                if entry.invalidated
                else max(0.0, min(300.0, entry.deadline - self.clock())),
                "wire_bytes": 0,
                "limit_bytes": 1024,
                "shape": None,
            }
        return {
            "job_id": entry.job_id,
            "plan_hash": entry.plan_hash,
            "broker_epoch": entry.epoch,
            "principal_id": entry.principal_id,
            "diagnostic_id": entry.diagnostic_id,
            **metadata,
        }

    def operator_home_line(self, job_id: str, plan_hash: str, diagnostic_id: str) -> dict[str, Any]:
        """Private local preview only; callers must enforce operator authentication."""
        metadata = self.operator_home_line_metadata(job_id, plan_hash)
        entry = self.local_home_diagnostic
        if (
            metadata is None
            or entry is None
            or entry.diagnostic_id != diagnostic_id
            or entry.probe is None
        ):
            raise BrokerError("local_home_diagnostic_not_available")
        preview = entry.probe.preview()
        # No raw data appears in metadata, ordinary job results, or history.
        return {**metadata, **preview}

    async def _work(self) -> None:
        while True:
            job_id = await self.queue.get()
            try:
                if job_id is None:
                    return
                store = self._store()
                job = self.job(job_id)
                if job["state"] != "queued":
                    continue
                queued_intent = self._execution_intents.get(job_id)
                if queued_intent is None:
                    store.set_job(job_id, "cancelled", "execution_intent_not_available")
                    continue
                requires_current_principal = (
                    queued_intent is not None or self.principal_valid is not None
                )
                queued_lease = self.session.id if self.session is not None else None
                self._expire()
                if self.job(job_id)["state"] != "queued":
                    continue
                if (
                    self.session is None
                    or self.session.id != queued_lease
                    or self.clock() >= self.session.deadline
                    or self.closing
                ):
                    store.set_job(job_id, "cancelled", "session_not_available")
                    continue
                plan = store.plan(job["plan_hash"])
                if plan is None or plan["session_id"] != self.session.id:
                    store.set_job(job_id, "needs_review", "plan_not_available")
                    self._cancel_queued("recovery_review_required")
                    continue
                lease = self.session
                if plan["test"] in (TestName.FIRMWARE.value, TestName.RESTART.value) and plan.get(
                    "log_delivery"
                ) != self._log_delivery(lease):
                    store.set_job(job_id, "cancelled", "principal_or_lease_no_longer_valid")
                    self.logs.append(Event.CANCELLED)
                    continue
                if lease.principal_id is not None and self.principal_valid is not None:
                    try:
                        live = self.principal_valid(
                            lease.principal_id, lease.owner, frozenset({"read", "control"})
                        )
                    except Exception:
                        live = False
                    if (
                        live is not True
                        or self.session is not lease
                        or self.clock() >= lease.deadline
                    ):
                        store.set_job(job_id, "cancelled", "principal_or_lease_no_longer_valid")
                        self.logs.append(Event.CANCELLED)
                        continue
                local_home_probe = None
                capture_target = ""
                current_valid = self.principal_valid
                current_binding = self._execution_bindings.get(job_id)
                flash_binding = self._flash_bindings.get(job_id)
                if flash_binding is not None and not validate_binding(self, flash_binding, plan):
                    self._cancel_execution_intent(queued_intent, "flash_binding_invalid")
                    continue
                if "flash_job_id" in plan:
                    try:
                        fresh = prepare_collector(
                            self, plan["flash_job_id"], lease.principal_id or ""
                        )
                        if any(plan.get(key) != value for key, value in fresh.items()):
                            raise ValueError("collector_binding_changed")
                    except Exception:
                        self._cancel_execution_intent(queued_intent, "collector_binding_changed")
                        continue
                current_authorized = True
                if lease.principal_id is not None and (
                    requires_current_principal or self.principal_valid is not None
                ):
                    try:
                        current_authorized = (
                            self.principal_valid is not None
                            and self.principal_valid(
                                lease.principal_id, lease.owner, frozenset({"read", "control"})
                            )
                            is True
                        )
                    except Exception:
                        current_authorized = False
                # Authentication, local-probe and policy callbacks above can
                # change the queued row, original intent, lease or clock. Keep
                # the original intent requirement through every callback, and
                # check the durable row again before any backend work starts.
                current_job = store.job(job_id)
                current_plan = store.plan(job["plan_hash"])
                if (
                    current_job is None
                    or not current_authorized
                    or queued_intent is None
                    or self.principal_valid is not current_valid
                    or self._runtime_binding() != current_binding
                    or (flash_binding is not None and not live_binding(self, flash_binding, plan))
                    or not collector_live(self, plan, lease.principal_id or "")
                    or not self._state_lock.is_locked
                    or current_job["state"] != "queued"
                    or current_job["plan_hash"] != job["plan_hash"]
                    or current_plan != plan
                    or digest_plan(plan) != job["plan_hash"]
                    or plan.get("broker_epoch") != self.epoch
                    or self.session is not lease
                    or lease.id != queued_lease
                    or not math.isfinite(lease.deadline)
                    or not math.isfinite(self.clock())
                    or self.clock() >= lease.deadline
                    or self.closing
                    or self._execution_uncertain()
                    or self._physical_busy()
                    or self.backend.hardware_connected
                    or not self._device_lock.is_locked
                    or (
                        queued_intent is not None and not self._execution_intent_live(queued_intent)
                    )
                ):
                    if local_home_probe is not None:
                        local_home_probe.clear()
                    if queued_intent is not None:
                        self._cancel_execution_intent(
                            queued_intent, "execution_intent_not_available"
                        )
                    elif current_job is not None and current_job["state"] == "queued":
                        store.set_job(job_id, "cancelled", "session_not_available")
                        self.logs.append(Event.CANCELLED)
                    continue
                self._execution_intents.pop(job_id, None)
                if plan["test"] == TestName.BUILD.value:
                    self.firmware_build.begin()
                elif plan["test"] == TestName.SOURCE.value:
                    self.firmware_source.begin()
                self.active_job = job_id
                capture_principal = self.session.principal_id
                store.set_job(job_id, "running")
                result: str | BackendResult = ""
                derby_scope = (
                    capture_provenance(self, plan, job_id, capture_principal or "")
                    if plan["test"] == TestName.DERBY.value
                    else None
                )

                def derby_diagnostic(
                    diagnostic: dict[str, Any] | None,
                    scope: DerbyProvenance | None = derby_scope,
                    prepared: dict[str, Any] = plan,
                    completed_job: str = job_id,
                    principal_id: str = capture_principal or "",
                ) -> dict[str, Any] | None:
                    if diagnostic is None or scope is None:
                        return diagnostic
                    scoped = dict(diagnostic)
                    # Freeze observed dispatch/completion evidence. Reading a
                    # historic report never consults a newer firmware target.
                    scoped[PROVENANCE_KEY] = finish_provenance(
                        scope,
                        capture_provenance(self, prepared, completed_job, principal_id),
                    ).model_dump(mode="json", exclude_none=True)
                    return scoped

                def running_authorized(
                    lease: Session = lease,
                    current_binding: str = current_binding,
                    queued_intent: ExecutionIntent = queued_intent,
                    job_id: str = job_id,
                    store: Store = store,
                    job: dict[str, Any] = job,
                    plan: dict[str, Any] = plan,
                ) -> bool:
                    valid = self.principal_valid
                    try:
                        authorized = (
                            lease.principal_id is not None
                            and valid is not None
                            and valid(
                                lease.principal_id, lease.owner, frozenset({"read", "control"})
                            )
                            is True
                        )
                        return bool(
                            authorized
                            and self.principal_valid is valid
                            and self._runtime_binding() == current_binding
                            and collector_live(self, plan, lease.principal_id or "")
                            and self.session is lease
                            and self._execution_lease_binding_live(queued_intent)
                            and self._device_lock.is_locked
                            and self._state_lock.is_locked
                            and self.active_job == job_id
                            and store.plan(job["plan_hash"]) == plan
                            and digest_plan(plan) == job["plan_hash"]
                            and self._running_job_binding_live(plan, queued_intent)
                        )
                    except Exception:
                        return False

                try:

                    def on_stage(name: str, job: str = job_id, db: Store = store) -> None:
                        db.set_stage(job, name)
                        self.logs.append(Event.STAGE, stage=name)

                    if plan["test"] == TestName.APP_SESSION.value:
                        result = await self.app_sessions.execute(
                            self.backend,
                            plan,
                            capture_principal or "",
                            on_stage,
                            running_authorized,
                        )
                    elif plan["test"] in {t.value for t in FLASH_TESTS}:
                        if capture_principal != plan.get("flash_principal"):
                            raise BrokerError("flash_principal_mismatch")

                        def flash_live(
                            plan: dict[str, Any] = plan,
                            lease: Session = lease,
                            capture_principal: str | None = capture_principal,
                            committing: bool = False,
                            current_binding: str = current_binding,
                            flash_binding: FlashBinding | None = flash_binding,
                            queued_intent: ExecutionIntent = queued_intent,
                        ) -> bool:
                            current = self.session
                            if (
                                self.closing
                                or current is None
                                or capture_principal is None
                                or current is not lease
                                or current.principal_id != capture_principal
                                or not math.isfinite(self.clock())
                                or self.clock() >= current.deadline
                            ):
                                return False
                            registration = self.settings.flash_devices.get(plan["device_id"])
                            if (
                                registration is None
                                or getattr(registration, "allow_" + plan["flash_operation"])
                                is not True
                                or hashlib.sha256(
                                    registration.model_dump_json().encode()
                                ).hexdigest()
                                != plan["registration_sha256"]
                            ):
                                return False
                            if not self._device_lock.is_locked or not self._state_lock.is_locked:
                                return False
                            if self.principal_valid is None:
                                return False
                            valid = self.principal_valid
                            binding = self._runtime_binding()
                            authorized = (
                                valid(
                                    capture_principal, current.owner, frozenset({"read", "control"})
                                )
                                is True
                            )
                            return bool(
                                authorized
                                and self.principal_valid is valid
                                and self._runtime_binding() == binding
                                and self._runtime_binding() == current_binding
                                and flash_binding is not None
                                and live_binding(self, flash_binding, plan)
                                and self.session is lease
                                and self._execution_lease_binding_live(queued_intent)
                                and self._device_lock.is_locked
                                and self._state_lock.is_locked
                                and self._running_job_binding_live(
                                    plan, queued_intent, committing=committing
                                )
                            )

                        result = await self.firmware_flash.execute(plan, on_stage, flash_live)
                    elif plan["test"] == TestName.SOURCE.value:
                        result = await self.firmware_source.execute(
                            plan, on_stage, running_authorized
                        )
                    elif plan["test"] == TestName.BUILD.value:
                        if capture_principal != plan.get("build_principal"):
                            raise BrokerError("build_principal_mismatch")
                        result = await self.firmware_build.execute(plan, on_stage)
                    elif plan["test"] in (TestName.DERBY.value, TestName.DERBY_FIXED.value):
                        from .serial_backend import SerialBackend

                        if not isinstance(self.backend, SerialBackend):
                            raise BrokerError("backend_not_available")

                        result = await self.backend.execute_smoke(
                            plan, on_stage, running_authorized
                        )
                        if plan["test"] == TestName.DERBY.value and (
                            not running_authorized() or self.backend.smoke_stop.is_set()
                        ):
                            raise BrokerError("operation_interrupted_review_required")
                    else:
                        result = await self.backend.execute(plan, on_stage)
                    if plan["test"] == TestName.DERBY.value:
                        from .derby_observation import NO_CAPTURE, DerbyObservation

                        if (
                            not isinstance(result, BackendResult)
                            or not isinstance(result.derby_observation, DerbyObservation)
                            or not result.derby_observation.exit_home_observed
                            or result.derby_observation.image_acquired
                            is not (result.image is not None)
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
                            or (
                                plan["test"] != TestName.RESTART.value
                                and result.observation.capture_gap
                            )
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
                            != (
                                "synthetic_fixture"
                                if self.backend.name == "mock"
                                else "render_buffer"
                            )
                        ):
                            raise BrokerError("capture_result_invalid")
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
                                policy_target=capture_target,
                            )
                except (FlashFailure, FlashAdmissionError) as failure:
                    store.set_job(
                        job_id,
                        "needs_review",
                        str(failure),
                        _bounded_read_diagnostic(failure.code, failure.diagnostic)
                        if isinstance(failure, FlashFailure)
                        else None,
                    )
                    self.logs.append(Event.REVIEW)
                    self._cancel_queued("recovery_review_required")
                except (BuildPreflightFailure, BuildLocationError) as failure:
                    store.set_job(
                        job_id,
                        "needs_review",
                        failure.code,
                        failure.public_diagnostic()
                        if isinstance(failure, BuildPreflightFailure)
                        else {"code": failure.code},
                    )
                    self.logs.append(Event.REVIEW)
                    self._cancel_queued("recovery_review_required")
                except SourceError as failure:
                    if plan["test"] == TestName.SOURCE.value:
                        self.firmware_source.record_failure(plan, failure.code)
                        store.set_job(job_id, "needs_review", failure.code, {"code": failure.code})
                    else:
                        store.set_job(job_id, "needs_review", "backend_error_no_retry")
                    self.logs.append(Event.REVIEW)
                    self._cancel_queued("recovery_review_required")
                except DeviceOperationFailure as failure:
                    from .serial_backend import SerialFailure

                    if (
                        plan["test"] == TestName.DERBY_FIXED.value
                        and isinstance(failure, SerialFailure)
                        and failure.fixed_trial is not None
                        and capture_principal is not None
                    ):
                        try:
                            store.finish_fixed_trial(
                                job_id,
                                job["plan_hash"],
                                capture_principal,
                                "needs_review",
                                failure.code,
                                failure.fixed_trial,
                                failure.diagnostic,
                            )
                        except Exception:
                            self.execution_uncertain = True
                            store.set_job(job_id, "needs_review", "fixed_trial_persist_failed")
                    else:
                        store.set_job(
                            job_id,
                            "needs_review",
                            failure.code,
                            derby_diagnostic(failure.diagnostic),
                        )
                    self.logs.append(Event.REVIEW)
                    self._cancel_queued("recovery_review_required")
                except DeviceDisconnected:
                    store.set_job(job_id, "needs_review", "device_disconnected_no_retry")
                    self.logs.append(Event.REVIEW)
                    self._cancel_queued("recovery_review_required")
                except Exception:
                    # Never persist the exception string (may contain credentials).
                    if plan["test"] == TestName.SOURCE.value:
                        self.firmware_source.record_failure(plan, "source_fetch_failed")
                        store.set_job(
                            job_id,
                            "needs_review",
                            "source_fetch_failed",
                            {"code": "source_fetch_failed"},
                        )
                    else:
                        store.set_job(job_id, "needs_review", "backend_error_no_retry")
                    self.logs.append(Event.REVIEW)
                    self._cancel_queued("recovery_review_required")
                else:
                    fixed_review = False
                    terminal_event = Event.PASSED
                    try:
                        completed = result.code if isinstance(result, BackendResult) else result
                        if plan["test"] == TestName.APP_SESSION.value:
                            terminal = (
                                "cancelled"
                                if completed == "app_session_cancelled"
                                else "failed"
                                if completed == "app_session_timeout"
                                else "succeeded"
                            )
                            store.set_job(job_id, terminal, completed)
                            terminal_event = {
                                "succeeded": Event.PASSED,
                                "failed": Event.FAILED,
                                "cancelled": Event.CANCELLED,
                            }[terminal]
                        elif plan["test"] in {item.value for item in FLASH_TESTS}:
                            self.firmware_flash.commit_success(
                                plan, completed, partial(flash_live, committing=True)
                            )
                        elif plan["test"] == TestName.SOURCE.value:
                            self.firmware_source.commit_success(plan, completed, running_authorized)
                        elif plan["test"] == TestName.DERBY_FIXED.value:
                            from .serial_backend import SerialBackend

                            if (
                                not isinstance(result, BackendResult)
                                or result.fixed_trial is None
                                or capture_principal is None
                                or not isinstance(self.backend, SerialBackend)
                            ):
                                raise ValueError("fixed_trial_result_invalid")
                            fixed_review = (
                                not running_authorized() or self.backend.smoke_stop.is_set()
                            )
                            finish_fixed_diagnostic(
                                DiagnosticContext(
                                    capture_principal,
                                    origin=lease.owner,
                                    epoch=self.epoch,
                                    lease_id=lease.id,
                                ),
                                job_id,
                                job["plan_hash"],
                                completed,
                                result.fixed_trial,
                                fixed_review,
                                store,
                            )
                        else:
                            diagnostic = (
                                {
                                    "derby_observation": result.derby_observation.model_dump(
                                        mode="json", exclude_none=True
                                    )
                                }
                                if isinstance(result, BackendResult)
                                and result.derby_observation is not None
                                and plan["test"] == TestName.DERBY.value
                                else None
                            )
                            store.set_job(
                                job_id, "succeeded", completed, derby_diagnostic(diagnostic)
                            )
                    except Exception as failure:
                        self.execution_uncertain = True
                        if plan["test"] == TestName.SOURCE.value:
                            code = (
                                failure.code
                                if isinstance(failure, SourceError)
                                else "source_fetch_failed"
                            )
                            self.firmware_source.record_failure(plan, code)
                            store.set_job(job_id, "needs_review", code, {"code": code})
                        else:
                            store.set_job(
                                job_id,
                                "needs_review",
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
                finally:
                    if local_home_probe is not None:
                        local_home_probe.finish()
                        local_home_probe = None
                    # Do not keep the last image alive in this suspended worker frame
                    # after the bounded cache expires or is cleared.
                    result = ""
                    if (
                        not self.backend.active
                        and not self.firmware_source.active
                        and not self.firmware_build.process.active
                        and not self.firmware_build.process.uncertain
                        and not self.firmware_flash.active
                        and not self.firmware_flash.uncertain
                    ):
                        self.active_job = None
                    if self.active_job is None:
                        try:
                            self.home_recovery.record_closed(
                                plan, lease.principal_id or "", lease.owner
                            )
                        except Exception:
                            # Failure to retain closure evidence never grants recovery.
                            pass
                    self._expire()
            except Exception:
                # A failed durable-state operation must not leave an apparently
                # idle lane admitting more work. History is not the runtime latch.
                self.execution_uncertain = True
                self.logs.append(Event.REVIEW)
            finally:
                if job_id is not None:
                    self.capture_delivery_consents.pop(job_id, None)
                    self._flash_bindings.pop(job_id, None)
                    self._execution_bindings.pop(job_id, None)
                    self._execution_intents.pop(job_id, None)
                self.queue.task_done()
