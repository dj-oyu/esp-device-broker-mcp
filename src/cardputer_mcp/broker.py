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
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

from filelock import FileLock, Timeout

from .backend import Backend, BackendResult, DeviceDisconnected, DeviceOperationFailure, MockBackend
from .build_locations import BuildLocationError
from .capture import CapturedImage
from .config import Settings
from .device_observation import DeviceObservation
from .firmware_build import BuildPreflightFailure
from .flash_lifecycle import FlashFailure
from .flash_manifest import FlashAdmissionError
from .flash_policy import FlashAuthorization
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
    """Process-local request to run exactly one plan after local approval."""

    epoch: str
    plan_hash: str
    job_id: str
    lease_id: str
    principal_id: str | None
    owner: str
    work_session_id: str | None
    deadline: float


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
    delivery_state: str = "pending"
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
        self.work_sessions: dict[str, WorkSession] = {}
        self.device_requests: dict[str, DeviceWait] = {}
        self.waiters: list[str] = []
        self._queue_sequence = 0
        self.maintenance: asyncio.Task[None] | None = None
        self.active_job: str | None = None
        self.logs = LogBook(settings.log_capacity)
        from .firmware_build import FirmwareBuild

        self.firmware_build = FirmwareBuild(settings, settings.state_dir / "history.sqlite3")
        from .firmware_source import FirmwareSource

        self.firmware_source = FirmwareSource(settings, self._store)
        from .profile_jobs import ProfileJobs

        self.profile_jobs = ProfileJobs(self)
        from .firmware_flash import FirmwareFlash

        self.firmware_flash = FirmwareFlash(
            settings, self.backend, self.firmware_build, self._store
        )
        self.queue: asyncio.Queue[str | None] = asyncio.Queue(settings.max_queue)
        self.worker: asyncio.Task[None] | None = None
        self.store: Store | None = None
        self.closing = False
        self.captures: dict[str, RetainedCapture] = {}
        self.capture_delivery_consents: dict[str, CaptureDeliveryConsent] = {}
        self.device_observations: dict[str, RetainedDeviceObservation] = {}
        self.capture_policies: Any = None
        self.policy_jobs: dict[str, Any] = {}
        from .flash_policy import FlashPolicies

        self.flash_policies = FlashPolicies(self)
        self.flash_policy_jobs: dict[str, FlashAuthorization] = {}
        self._execution_intents: dict[str, ExecutionIntent] = {}
        self.local_home_diagnostic: LocalHomeDiagnostic | None = None
        self._device_lock = FileLock(
            str(lock_path or device_lock_path(settings.backend)), timeout=0
        )
        self._state_lock = FileLock(str(settings.state_dir / "broker.lockfile"), timeout=0)

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
            self.store = Store(self.settings.state_dir / "history.sqlite3")
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
        await self.profile_jobs.close()
        self.firmware_build.request_stop()
        self.firmware_flash.request_stop()
        self._clear_local_home_diagnostic()
        self.captures.clear()
        self.capture_delivery_consents.clear()
        self.device_observations.clear()
        self.policy_jobs.clear()
        self.flash_policy_jobs.clear()
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
                    self.policy_jobs.pop(job_id, None)
                    self.flash_policy_jobs.pop(job_id, None)
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
        self.flash_policy_jobs.pop(intent.job_id, None)
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

    def _physical_busy(self) -> bool:
        return bool(
            self.active_job is not None
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
        if self.capture_policies is not None:
            self.capture_policies.invalidate_session()
        self.session = None
        self.capture_delivery_consents.clear()
        self._queue_sequence += 1
        self.logs.append(Event.EXPIRED if state == "expired" else Event.RELEASED)

    def maintenance_tick(self) -> None:
        """Single-event-loop transition point. No awaits or hardware opens here."""
        self._prune_captures()
        self._prune_device_observations()
        self._prune_local_home_diagnostic()
        if self.store is None:
            return
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
        for job_id, consent in tuple(self.capture_delivery_consents.items()):
            try:
                plan = self._store().plan(consent.intent.plan_hash)
                live = plan is not None and self._capture_delivery_binding_live(consent, plan)
            except Exception:
                live = False
            if not live:
                self.capture_delivery_consents.pop(job_id, None)
        if (
            self.closing
            or self.session is not None
            or self._physical_busy()
            or self._store().needs_review()
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
            "profile_approval_policy": "registered_project",
            "owner": session.owner if session else None,
            "lease_remaining_seconds": max(0, round(session.deadline - self.clock(), 3))
            if session
            else 0,
            "queue_depth": self.queue.qsize(),
            "wait_queue_depth": len(self.waiters),
            "work_session_count": sum(w.state == "open" for w in self.work_sessions.values()),
            "history": store.counts(),
            "needs_review": store.needs_review(),
            "approval_policy": "operator_opted_in_to_mock"
            if self.settings.allow_mock_execution
            else "local_operator_per_plan",
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
        if self._store().needs_review():
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
        if self._store().needs_review():
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
                    TestName.CAPTURE,
                    TestName.DERBY,
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
        if store.needs_review():
            raise BrokerError("recovery_review_required")
        storage = None
        try:
            if request.test in FLASH_TESTS:
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
                    request.project_id, session.principal_id or "", request.source_receipt
                )
                storage = self.firmware_build.storage(request.project_id or "")
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
        if request.test in (TestName.FIRMWARE, TestName.RESTART):
            payload["log_delivery"] = self._log_delivery(session)
        digest = digest_plan(payload)
        existing = store.job(request.job_id)
        if existing and existing["plan_hash"] != digest:
            raise BrokerError("job_id_conflict")
        if store.plan(digest) is None and store.record_count("plans") >= 10000:
            raise BrokerError("history_full_archive_offline")
        store.put_plan(digest, payload)
        policy_available = False
        if request.test == TestName.CAPTURE and self.capture_policies is not None:
            try:
                authorization = self.capture_policies.authorize_capture(payload, record_use=False)
                policy_available = (
                    authorization is not None
                    and self.capture_policies.validate_capture(authorization, payload)
                )
            except Exception:
                policy_available = False
        flash_available = (
            request.test == TestName.FLASH and self.flash_policies.authorize(payload) is not None
        )
        result = {
            "plan_hash": digest,
            "plan": payload,
            **({"storage": storage} if storage is not None else {}),
            "approval_required": not flash_available
            and (
                request.test in ({TestName.BUILD, TestName.SOURCE} | FLASH_TESTS)
                or (not self.settings.allow_mock_execution and not policy_available)
            ),
        }
        if request.test == TestName.CAPTURE:
            result["policy_permit_available"] = policy_available
        return result

    @staticmethod
    def _log_delivery(session: Session) -> dict[str, Any]:
        """The immutable approval explicitly names the bounded log recipient."""
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
        if payload["test"] in (TestName.FIRMWARE.value, TestName.RESTART.value) and payload.get(
            "log_delivery"
        ) != self._log_delivery(session):
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
        )
        existing = store.job(job_id)
        if existing:
            if existing["plan_hash"] != request.plan_hash:
                raise BrokerError("job_id_conflict")
            return {**existing, "deduplicated": True}
        if store.needs_review():
            raise BrokerError("recovery_review_required")
        if self._execution_capacity_full():
            raise BrokerError("queue_full")
        if store.record_count("jobs") >= 10000:
            raise BrokerError("history_full_archive_offline")
        authorization = None
        approved = store.db.execute(
            "SELECT 1 FROM approvals WHERE hash=?", (request.plan_hash,)
        ).fetchone()
        if (
            not approved
            and payload["test"] == TestName.CAPTURE.value
            and self.capture_policies is not None
        ):
            authorization = self.capture_policies.authorize_capture(payload)
            if authorization is not None and not self.capture_policies.validate_capture(
                authorization, payload
            ):
                raise BrokerError("capture_policy_no_longer_valid")
        flash_authorization = (
            self.flash_policies.authorize(payload)
            if not approved and payload["test"] == TestName.FLASH.value
            else None
        )
        allow_mock = (
            self.settings.backend == "mock"
            and self.settings.allow_mock_execution
            and payload["test"]
            not in ({TestName.BUILD.value, TestName.SOURCE.value} | {t.value for t in FLASH_TESTS})
        )
        if (
            request.wait_for_approval
            and not approved
            and not allow_mock
            and authorization is None
            and flash_authorization is None
        ):
            # Policy callbacks must not extend the original request's lifetime
            # or register work under a replacement lease/principal.
            current = self._lease(request.session)
            if current is not session or not self._execution_intent_binding_live(intent):
                raise BrokerError("execution_intent_not_available")
            if store.needs_review():
                raise BrokerError("recovery_review_required")
            if self._execution_capacity_full():
                raise BrokerError("queue_full")
            current_plan = store.plan(request.plan_hash)
            if current_plan is None or digest_plan(current_plan) != request.plan_hash:
                raise BrokerError("plan_not_found_or_changed")
            store.add_awaiting_job(job_id, request.plan_hash)
            self._execution_intents[job_id] = intent
            return {**self.job(job_id), "deduplicated": False}
        if flash_authorization is not None and (
            self.session is not session
            or not self._execution_intent_binding_live(intent)
            or not self.flash_policies.live(flash_authorization, payload)
            or self._execution_capacity_full()
            or store.record_count("jobs") >= 10000
            or store.job(job_id) is not None
        ):
            raise BrokerError("flash_policy_no_longer_valid")
        try:
            store.approved_enqueue(
                job_id,
                request.plan_hash,
                allow_mock,
                policy_authorized=authorization is not None or flash_authorization is not None,
            )
        except ValueError as exc:
            raise BrokerError("operator_approval_required") from exc
        # No await between durable enqueue and queue insertion. Crash in between is
        # recovered to needs_review; it never causes replay.
        if authorization is not None:
            self.policy_jobs[job_id] = authorization
        if flash_authorization is not None:
            self.flash_policy_jobs[job_id] = flash_authorization
            self._execution_intents[job_id] = intent
        self.queue.put_nowait(job_id)
        self.logs.append(Event.QUEUED)
        return {**self.job(job_id), "deduplicated": False}

    def dispatch_pending_execution(self, plan_hash: str) -> bool:
        """Called synchronously only after the local operator approved this review."""
        intent = next(
            (item for item in self._execution_intents.values() if item.plan_hash == plan_hash),
            None,
        )
        if intent is None:
            return False
        store = self._store()
        live = True
        if intent.principal_id is not None:
            try:
                live = (
                    self.principal_valid is not None
                    and self.principal_valid(
                        intent.principal_id, intent.owner, frozenset({"read", "control"})
                    )
                    is True
                )
            except Exception:
                live = False
        # No callbacks or awaits between these final checks, the atomic
        # awaiting->queued approval consumption, and inserting into the worker.
        job = store.job(intent.job_id)
        if not live or not self._execution_intent_live(intent):
            self._cancel_execution_intent(intent, "execution_intent_not_available")
            raise BrokerError("execution_intent_not_available")
        if self.closing:
            raise BrokerError("broker_closing")
        if store.needs_review():
            self._cancel_execution_intent(intent, "recovery_review_required")
            raise BrokerError("recovery_review_required")
        if (
            self._physical_busy()
            or self.backend.hardware_connected
            or not self._device_lock.is_locked
        ):
            raise BrokerError("device_busy")
        if not self.queue.empty() or store.counts().get("queued") or store.counts().get("running"):
            raise BrokerError("device_jobs_pending")
        if job is None or job["state"] != "awaiting_approval":
            raise BrokerError("execution_intent_not_available")
        if self.queue.full():
            raise BrokerError("queue_full")
        try:
            # Later settings/policies cannot turn a pending request into an
            # automatic permission. This path always consumes manual approval.
            store.approved_enqueue(intent.job_id, plan_hash, False, awaiting_approval=True)
        except ValueError:
            raise BrokerError("execution_intent_not_available") from None
        self.queue.put_nowait(intent.job_id)
        self.logs.append(Event.QUEUED)
        return True

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
        if job["state"] == "running" and plan["test"] == TestName.SOURCE.value:
            accepted = self.firmware_source.request_stop(request.job_id)
            return {**job, "stop_requested": accepted, "requires_review": accepted}
        if job["state"] == "running" and plan["test"] == TestName.DERBY.value:
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

    def _prune_device_observations(self) -> None:
        now = self.clock()
        for job_id, retained in tuple(self.device_observations.items()):
            if not math.isfinite(now) or now >= retained.deadline:
                self.device_observations.pop(job_id, None)

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
            raise BrokerError("device_logs_not_available")
        return retained

    def read_device_logs(self, job_id: str, principal_id: str) -> dict[str, Any]:
        """Read only the requesting principal's explicitly approved completed report."""
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
        if plan["test"] != TestName.CAPTURE.value or lease.principal_id is None:
            raise BrokerError("capture_delivery_consent_not_available")
        pending = self.pending_execution(digest_plan(plan))
        intent = pending or ExecutionIntent(
            self.epoch,
            digest_plan(plan),
            plan["job_id"],
            lease.id,
            lease.principal_id,
            lease.owner,
            lease.work_session_id,
            lease.deadline,
        )
        consent = CaptureDeliveryConsent(
            intent,
            lease,
            self.work_sessions.get(lease.work_session_id or ""),
            self._capture_delivery_target(),
        )
        if not self._capture_delivery_authorized(consent, plan):
            raise BrokerError("capture_delivery_consent_not_available")
        if len(self.capture_delivery_consents) >= 64:
            raise BrokerError("capture_delivery_consent_not_available")
        return consent

    def _capture_delivery_binding_live(
        self, consent: CaptureDeliveryConsent, plan: dict[str, Any]
    ) -> bool:
        intent = consent.intent
        job = self._store().job(intent.job_id)
        return bool(
            self.session is consent.lease
            and self.work_sessions.get(intent.work_session_id or "") is consent.work
            and self._execution_intent_binding_live(intent)
            and plan.get("test") == TestName.CAPTURE.value
            and plan.get("job_id") == intent.job_id
            and plan.get("broker_epoch") == intent.epoch
            and plan.get("session_id") == intent.lease_id
            and digest_plan(plan) == intent.plan_hash
            and self._store().plan(intent.plan_hash) == plan
            and self._capture_delivery_target() == consent.target
            and self._device_lock.is_locked
            and self._state_lock.is_locked
            and not self._store().needs_review()
            and (
                job is None
                or (
                    job["plan_hash"] == intent.plan_hash
                    and job["state"] in ("awaiting_approval", "queued", "running")
                )
            )
        )

    def _capture_delivery_authorized(
        self, consent: CaptureDeliveryConsent, plan: dict[str, Any]
    ) -> bool:
        try:
            valid = self.principal_valid
            authorized = (
                valid is not None
                and valid(
                    consent.intent.principal_id or "",
                    consent.intent.owner,
                    frozenset({"read", "control"}),
                )
                is True
            )
            return bool(
                authorized
                and self.principal_valid is valid
                and self._capture_delivery_binding_live(consent, plan)
            )
        except Exception:
            return False

    def _consume_capture_delivery(self, plan: dict[str, Any]) -> bool:
        job_id = plan["job_id"]
        consent = self.capture_delivery_consents.get(job_id)
        allowed = bool(
            consent is not None
            and self.active_job == job_id
            and self._capture_delivery_authorized(consent, plan)
            and self.capture_delivery_consents.get(job_id) is consent
        )
        self.capture_delivery_consents.pop(job_id, None)
        return allowed

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
        if retained.delivery_state != "approved" and not (
            retained.delivery_state == "pending"
            and self._image_policy_allowed(job_id, retained, record_use=True)
        ):
            raise BrokerError(
                "capture_delivery_rejected"
                if retained.delivery_state == "rejected"
                else "capture_delivery_approval_required"
            )
        if self._operator_capture(job_id) is not retained:
            raise BrokerError("capture_not_available")
        return retained.image

    def _image_policy_allowed(
        self, job_id: str, retained: RetainedCapture, *, record_use: bool = False
    ) -> bool:
        if self.capture_policies is None or retained.delivery_state == "rejected":
            return False
        try:
            return (
                self.capture_policies.authorize_image(job_id, retained, record_use=record_use)
                is True
            )
        except Exception:
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
            "policy_allowed": self._image_policy_allowed(job_id, retained),
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
        return {
            **provenance,
            **self._image_summary(job_id, retained),
            "metadata": retained.image.metadata(),
            "image_base64": base64.b64encode(retained.image.png).decode("ascii"),
            "delivery_revision": self._delivery_revision(job_id, retained),
        }

    def operator_decide_image(
        self, job_id: str, sha256: str, delivery_revision: str, decision: str
    ) -> dict[str, Any]:
        retained = self._operator_capture(job_id)
        if decision not in ("approve", "reject"):
            raise BrokerError("invalid_image_decision")
        if not secrets.compare_digest(sha256, retained.image.sha256) or not secrets.compare_digest(
            delivery_revision, self._delivery_revision(job_id, retained)
        ):
            raise BrokerError("stale_image_preview")
        target = "approved" if decision == "approve" else "rejected"
        if retained.delivery_state not in ("pending", target):
            raise BrokerError("image_decision_final")
        if target == "approved" and self.principal_valid is not None:
            try:
                valid = self.principal_valid(
                    retained.principal_id, retained.origin, frozenset({"read"})
                )
            except Exception:
                valid = False
            if valid is not True:
                raise BrokerError("image_recipient_not_authorized")
        if self._operator_capture(job_id) is not retained:
            raise BrokerError("stale_image_preview")
        self.captures[job_id] = replace(retained, delivery_state=target)
        return {"job_id": job_id, "delivery_state": target}

    def job(self, job_id: str) -> dict[str, Any]:
        job = self._store().job(job_id)
        if job is None:
            raise BrokerError("job_not_found")
        return job

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
        """Called only by the manual operator approval path, after exact review."""
        from .serial_backend import SerialBackend

        self._prune_local_home_diagnostic()
        if (
            self.settings.backend != "serial"
            or not isinstance(self.backend, SerialBackend)
            or plan["test"] != TestName.CAPTURE.value
            or plan["backend"] != "serial"
            or plan["broker_epoch"] != self.epoch
            or plan["session_id"] != lease.id
            or self.session is not lease
            or not lease.principal_id
        ):
            raise BrokerError("local_home_diagnostic_not_available")
        previous = self.local_home_diagnostic
        if previous is not None and not previous.consumed and not previous.invalidated:
            raise BrokerError("local_home_diagnostic_already_armed")
        now = self.clock()
        if not math.isfinite(now) or not math.isfinite(lease.deadline) or now >= lease.deadline:
            raise BrokerError("local_home_diagnostic_not_available")
        entry = LocalHomeDiagnostic(
            uuid4().hex,
            self.epoch,
            plan["job_id"],
            digest_plan(plan),
            lease.principal_id,
            lease.owner,
            lease.id,
            min(now + 300, lease.deadline),
        )
        if not self._home_line_principal_live(entry):
            raise BrokerError("local_home_diagnostic_not_available")
        return entry

    def _install_local_home_diagnostic(self, entry: LocalHomeDiagnostic) -> None:
        self._clear_local_home_diagnostic()
        self.local_home_diagnostic = entry

    def _local_home_opt_in_current(self, plan: dict[str, Any], lease: Session) -> bool:
        self._prune_local_home_diagnostic()
        entry = self.local_home_diagnostic
        return bool(
            entry is not None
            and not entry.consumed
            and not entry.invalidated
            and entry.epoch == self.epoch
            and entry.job_id == plan["job_id"]
            and entry.plan_hash == digest_plan(plan)
            and entry.lease_id == lease.id
            and entry.principal_id == lease.principal_id
            and entry.origin == lease.owner
            and self._home_line_binding_live(entry, lease)
        )

    def _take_local_home_probe(
        self, plan: dict[str, Any], lease: Session, *, policy_authorized: bool
    ) -> HomeLineProbe | None:
        self._prune_local_home_diagnostic()
        entry = self.local_home_diagnostic
        if entry is None or entry.plan_hash != digest_plan(plan) or entry.job_id != plan["job_id"]:
            return None
        if policy_authorized or not self._local_home_opt_in_current(plan, lease):
            raise BrokerError("local_home_diagnostic_no_longer_valid")
        # approved_enqueue consumed the durable manual approval before this
        # worker runs. The one-shot in-memory opt-in was installed only by that
        # exact manual approval; policy-authorized enqueue cannot consume it.
        entry.consumed = True
        entry.probe = HomeLineProbe(self.clock, self.clock() + 120)
        return entry.probe

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
                authorization = self.policy_jobs.get(job_id)
                if authorization is not None:
                    try:
                        valid = (
                            self.capture_policies is not None
                            and self.capture_policies.validate_capture(authorization, plan)
                        )
                    except Exception:
                        valid = False
                    if valid is not True:
                        store.set_job(job_id, "cancelled", "capture_policy_no_longer_valid")
                        self.logs.append(Event.CANCELLED)
                        continue
                flash_authorization = self.flash_policy_jobs.get(job_id)
                if flash_authorization is not None and not self.flash_policies.validate(
                    flash_authorization, plan
                ):
                    store.set_job(job_id, "cancelled", "flash_policy_no_longer_valid")
                    self.logs.append(Event.CANCELLED)
                    continue
                try:
                    local_home_probe = self._take_local_home_probe(
                        plan, lease, policy_authorized=authorization is not None
                    )
                except BrokerError:
                    store.set_job(job_id, "cancelled", "local_home_diagnostic_no_longer_valid")
                    self.logs.append(Event.CANCELLED)
                    continue
                capture_target = ""
                if self.capture_policies is not None:
                    try:
                        capture_target = self.capture_policies.target()
                    except Exception:
                        pass
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
                    or (
                        flash_authorization is not None
                        and (
                            self.flash_policy_jobs.get(job_id) is not flash_authorization
                            or not self.flash_policies.live(flash_authorization, plan)
                        )
                    )
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
                    or store.needs_review()
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
                try:

                    def on_stage(name: str, job: str = job_id, db: Store = store) -> None:
                        db.set_stage(job, name)
                        self.logs.append(Event.STAGE, stage=name)

                    if plan["test"] in {t.value for t in FLASH_TESTS}:
                        if capture_principal != plan.get("flash_principal"):
                            raise BrokerError("flash_principal_mismatch")

                        def flash_live(
                            plan: dict[str, Any] = plan,
                            lease: Session = lease,
                            capture_principal: str | None = capture_principal,
                            flash_authorization: FlashAuthorization | None = flash_authorization,
                            committing: bool = False,
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
                            authorized = (
                                self.principal_valid(
                                    capture_principal, current.owner, frozenset({"read", "control"})
                                )
                                is True
                            )
                            return authorized and (
                                flash_authorization is None
                                or (
                                    self.flash_policy_jobs.get(plan["job_id"])
                                    is flash_authorization
                                    and self.flash_policies.live(
                                        flash_authorization, plan, committing=committing
                                    )
                                )
                            )

                        result = await self.firmware_flash.execute(plan, on_stage, flash_live)
                    elif plan["test"] == TestName.SOURCE.value:

                        def source_live(
                            lease: Session = lease,
                            capture_principal: str | None = capture_principal,
                        ) -> bool:
                            return (
                                not self.closing
                                and self.session is lease
                                and capture_principal is not None
                                and lease.principal_id == capture_principal
                                and math.isfinite(self.clock())
                                and self.clock() < lease.deadline
                                and self._device_lock.is_locked
                                and self._state_lock.is_locked
                                and self.principal_valid is not None
                                and self.principal_valid(
                                    capture_principal, lease.owner, frozenset({"read", "control"})
                                )
                                is True
                            )

                        result = await self.firmware_source.execute(plan, on_stage, source_live)
                    elif plan["test"] == TestName.BUILD.value:
                        if capture_principal != plan.get("build_principal"):
                            raise BrokerError("build_principal_mismatch")
                        result = await self.firmware_build.execute(plan, on_stage)
                    elif plan["test"] == TestName.DERBY.value:
                        from .serial_backend import SerialBackend

                        if not isinstance(self.backend, SerialBackend):
                            raise BrokerError("backend_not_available")

                        def smoke_live(
                            lease: Session = lease,
                            capture_principal: str | None = capture_principal,
                        ) -> bool:
                            return (
                                not self.closing
                                and self.session is lease
                                and capture_principal is not None
                                and lease.principal_id == capture_principal
                                and math.isfinite(self.clock())
                                and self.clock() < lease.deadline
                                and self._device_lock.is_locked
                                and self._state_lock.is_locked
                                and self.principal_valid is not None
                                and self.principal_valid(
                                    capture_principal, lease.owner, frozenset({"read", "control"})
                                )
                                is True
                            )

                        result = await self.backend.execute_smoke(plan, on_stage, smoke_live)
                        if not smoke_live() or self.backend.smoke_stop.is_set():
                            raise BrokerError("operation_interrupted_review_required")
                    elif local_home_probe is None:
                        result = await self.backend.execute(plan, on_stage)
                    else:
                        from .serial_backend import SerialBackend

                        if not isinstance(self.backend, SerialBackend):
                            raise BrokerError("local_home_diagnostic_not_available")
                        result = await self.backend.execute_with_local_probe(
                            plan, on_stage, local_home_probe
                        )
                    if plan["test"] in (TestName.CAPTURE.value, TestName.DERBY.value) and (
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
                        self._prune_captures()
                        while len(self.captures) >= 8:
                            del self.captures[next(iter(self.captures))]
                        self.captures[job_id] = RetainedCapture(
                            capture_principal,
                            result.image,
                            self.clock() + 300,
                            self.session.owner if self.session is not None else "",
                            datetime.now(UTC).isoformat(),
                            delivery_state="approved"
                            if self._consume_capture_delivery(plan)
                            else "pending",
                            policy_target=capture_target,
                        )
                except (FlashFailure, FlashAdmissionError) as failure:
                    store.set_job(job_id, "needs_review", str(failure))
                    self.logs.append(Event.REVIEW)
                    self._cancel_queued("recovery_review_required")
                except (BuildPreflightFailure, BuildLocationError) as failure:
                    store.set_job(job_id, "needs_review", failure.code, {"code": failure.code})
                    self.logs.append(Event.REVIEW)
                    self._cancel_queued("recovery_review_required")
                except SourceError as failure:
                    if plan["test"] == TestName.SOURCE.value:
                        self.firmware_source.record_failure(plan, failure.code)
                        store.set_job(job_id, "needs_review", failure.code, {"code": failure.code})
                    else:
                        store.set_job(job_id, "needs_review", "backend_error_no_retry")
                    if self.capture_policies is not None:
                        self.capture_policies.invalidate_session()
                    self.logs.append(Event.REVIEW)
                    self._cancel_queued("recovery_review_required")
                except DeviceOperationFailure as failure:
                    store.set_job(job_id, "needs_review", failure.code, failure.diagnostic)
                    if self.capture_policies is not None:
                        self.capture_policies.invalidate_session()
                    self.logs.append(Event.REVIEW)
                    self._cancel_queued("recovery_review_required")
                except DeviceDisconnected:
                    store.set_job(job_id, "needs_review", "device_disconnected_no_retry")
                    if self.capture_policies is not None:
                        self.capture_policies.invalidate_session()
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
                    if self.capture_policies is not None:
                        self.capture_policies.invalidate_session()
                    self.logs.append(Event.REVIEW)
                    self._cancel_queued("recovery_review_required")
                else:
                    try:
                        completed = result.code if isinstance(result, BackendResult) else result
                        if plan["test"] in {item.value for item in FLASH_TESTS}:
                            self.firmware_flash.commit_success(
                                plan, completed, partial(flash_live, committing=True)
                            )
                        elif plan["test"] == TestName.SOURCE.value:
                            self.firmware_source.commit_success(plan, completed, source_live)
                        else:
                            store.set_job(job_id, "succeeded", completed)
                    except Exception as failure:
                        if plan["test"] == TestName.SOURCE.value:
                            code = (
                                failure.code
                                if isinstance(failure, SourceError)
                                else "source_fetch_failed"
                            )
                            self.firmware_source.record_failure(plan, code)
                            store.set_job(job_id, "needs_review", code, {"code": code})
                        else:
                            store.set_job(job_id, "needs_review", "firmware_final_commit_failed")
                        self.logs.append(Event.REVIEW)
                        self._cancel_queued("recovery_review_required")
                    else:
                        self.logs.append(Event.PASSED)
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
                    self._expire()
            finally:
                if job_id is not None:
                    self.capture_delivery_consents.pop(job_id, None)
                    self.policy_jobs.pop(job_id, None)
                    self.flash_policy_jobs.pop(job_id, None)
                    self._execution_intents.pop(job_id, None)
                self.queue.task_done()
