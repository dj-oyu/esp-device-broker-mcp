"""One authenticated, explicit DERBY exit after a proved close; never a replay."""

import asyncio
import math
import threading
from dataclasses import dataclass, field
from typing import Any

from .broker import BrokerError, digest_plan
from .models import Digest, Identifier, StrictModel
from .ports import resolve_port
from .serial_backend import Reader, SerialBackend, SerialFailure, fingerprint
from .smoke_port import SmokePort

CAPTURE_FAILURES = frozenset({"serial_capture_invalid", "serial_capture_timeout"})


class RequestHomeRecovery(StrictModel):
    job_id: Identifier
    source_job_id: Identifier


class CancelHomeRecovery(StrictModel):
    job_id: Identifier
    plan_hash: Digest


class RecoveryPort(SmokePort):
    """A fixed one-q capability and strict aggregate receive budget, even while draining."""

    def __init__(self, raw: Any, live: Any, stop: threading.Event, budget: int) -> None:
        super().__init__(raw, live, stop)
        self.remaining = budget
        self.sent = False

    def read(self, size: int = 1) -> bytes:
        if self.remaining <= 0:
            raise SerialFailure("serial_capture_limit")
        wanted = min(size, self.remaining)
        data = super().read(wanted)
        self.remaining -= len(data)
        if len(data) > wanted:
            raise SerialFailure("serial_capture_limit")
        return data

    def write(self, data: bytes) -> int | None:
        self.check()
        if self.sent or data != b"q":
            raise SerialFailure("command_not_allowlisted")
        self.sent = True
        return super().write(data)


@dataclass
class Recovery:
    plan: dict[str, Any]
    proof: dict[str, Any]
    backend: SerialBackend
    deadline: float
    execution_deadline: float | None = None
    state: str = "queued"
    stop: threading.Event = field(default_factory=threading.Event)
    task: asyncio.Task[None] | None = None


class HomeRecovery:
    def __init__(self, broker: Any) -> None:
        self.broker = broker
        self.entries: dict[str, Recovery] = {}
        self.checking = False

    def record_closed(self, plan: dict[str, Any], principal: str, origin: str) -> None:
        b, s = self.broker, self.broker._store()
        backend = b.backend
        job = s.job(plan["job_id"])
        if not isinstance(backend, SerialBackend) or job is None:
            return
        diagnostic = job["diagnostic"] or {}
        summary = diagnostic.get("receive_summary", {})
        if not (
            plan.get("test") == "derby_smoke"
            and plan.get("backend") == "serial"
            and job["state"] == "needs_review"
            and job["result"] in CAPTURE_FAILURES
            and job["stage"] == "serial_close"
            and job["reviewed"] == 0
            and diagnostic.get("expected_marker") == "CAPTURE_END"
            and summary.get("last_command") == "capture_frame"
            and summary.get("command_write_completed") is True
            and backend.last_closed_plan == plan
            and not backend.active
            and backend.hardware_connected is False
            and backend.ownership_uncertain is False
            and b.active_job is None
            and principal
        ):
            return
        s.record_home_closed(
            plan["job_id"],
            dict(
                plan_hash=digest_plan(plan),
                principal_id=principal,
                origin=origin,
                device_fingerprint=plan["device_fingerprint"],
                expected_firmware_sha256=plan["expected_firmware_sha256"],
                port=plan["port"],
            ),
        )

    def _state(self, e: Recovery, *, running: bool) -> None:
        b, s, p = self.broker, self.broker._store(), e.plan
        now = b.clock()
        source = s.job(p["source_job_id"])
        current = s.job(p["job_id"])
        source_plan = s.plan(e.proof["plan_hash"])
        if (
            b.closing
            or not math.isfinite(now)
            or not math.isfinite(e.deadline)
            or now >= e.deadline
            or (
                running
                and (
                    e.execution_deadline is None
                    or not math.isfinite(e.execution_deadline)
                    or now >= e.execution_deadline
                )
            )
            or e.stop.is_set()
            or p["broker_epoch"] != b.epoch
            or b.backend is not e.backend
            or not b._device_lock.is_locked
            or not b._state_lock.is_locked
            or b.session is not None
            or b.waiters
            or not b.queue.empty()
            or b.profile_jobs.active is not None
            or b.profile_jobs.uncertain
            or b.firmware_source.active
            or b.firmware_build.process.active
            or b.firmware_build.process.uncertain
            or b.firmware_build.output_uncertain
            or b.firmware_flash.active
            or b.firmware_flash.uncertain
            or e.backend.ownership_uncertain is not False
            or type(e.backend.hardware_connected) is not bool
            or (b.active_job != p["job_id"] if running else b._physical_busy())
            or (
                e.backend.active not in (0, 1)
                if running
                else e.backend.hardware_connected is not False
            )
            or source is None
            or source["state"] != "needs_review"
            or source["reviewed"] != 0
            or source["plan_hash"] != e.proof["plan_hash"]
            or source["result"] not in CAPTURE_FAILURES
            or current is None
            or current["plan_hash"] != digest_plan(p)
            or current["state"] != ("running" if running else "queued")
            or e.state != ("running" if running else "queued")
            or not isinstance(source_plan, dict)
            or digest_plan(source_plan) != e.proof["plan_hash"]
            or source_plan != p["source_plan"]
            or source_plan.get("test") != "derby_smoke"
            or source_plan.get("backend") != "serial"
            or any(
                source_plan.get(k) != p[k]
                for k in ("device_fingerprint", "expected_firmware_sha256", "port")
            )
            or s.plan(digest_plan(p)) != p
            or s.home_source(p["source_job_id"]) != dict(payload=e.proof, attempted=int(running))
            or p["expected_firmware_sha256"] is None
            or e.backend.config.expected_firmware_sha256 != p["expected_firmware_sha256"]
        ):
            raise BrokerError("home_recovery_unavailable")
        other = s.db.execute(
            "SELECT id FROM jobs WHERE (state='needs_review' AND reviewed=0 "
            "OR state IN ('queued','running')) AND id NOT IN (?,?) LIMIT 1",
            (p["source_job_id"], p["job_id"]),
        ).fetchone()
        unresolved = s.db.execute(
            "SELECT 1 FROM device_requests WHERE state='needs_review' AND reviewed=0 LIMIT 1"
        ).fetchone()
        if other or unresolved:
            raise BrokerError("home_recovery_other_review_or_work")

    def _validate(self, e: Recovery, *, running: bool = False) -> None:
        if self.checking:
            raise BrokerError("home_recovery_reentrant_check")
        self.checking = True
        try:
            self._state(e, running=running)
            b, p = self.broker, e.plan
            valid = b.principal_valid
            if (
                valid is None
                or valid(p["principal_id"], p["origin"], frozenset({"read", "control"})) is not True
            ):
                raise BrokerError("home_recovery_authority_changed")
            identity = resolve_port(e.backend.config.selector, e.backend.inventory())
            if fingerprint(identity) != p["device_fingerprint"] or identity.port != p["port"]:
                raise BrokerError("home_recovery_target_changed")
            if b.principal_valid is not valid:
                raise BrokerError("home_recovery_authority_changed")
            # Enumeration is an opaque callback too; do not use an earlier grant.
            if valid(p["principal_id"], p["origin"], frozenset({"read", "control"})) is not True:
                raise BrokerError("home_recovery_authority_changed")
            self._state(e, running=running)
        except (ValueError, OSError):
            raise BrokerError("home_recovery_unavailable") from None
        finally:
            self.checking = False

    def request(self, request: RequestHomeRecovery, principal: str, origin: str) -> dict[str, Any]:
        b, s = self.broker, self.broker._store()
        backend = b.backend
        record = s.home_source(request.source_job_id)
        if (
            not isinstance(backend, SerialBackend)
            or record is None
            or record["attempted"]
            or record["payload"]["principal_id"] != principal
            or record["payload"]["origin"] != origin
            or s.job(request.job_id) is not None
            or not math.isfinite(b.clock())
        ):
            raise BrokerError("home_recovery_source_not_eligible")
        self.maintain()
        self.entries = {k: v for k, v in self.entries.items() if v.state in ("queued", "running")}
        if len(self.entries) >= 16 or s.record_count("jobs") >= 10000:
            raise BrokerError("home_recovery_history_full")
        if any(v.plan["source_job_id"] == request.source_job_id for v in self.entries.values()):
            raise BrokerError("home_recovery_already_requested")
        proof = record["payload"]
        plan = dict(
            version=1,
            operation="home_recovery",
            broker_epoch=b.epoch,
            job_id=request.job_id,
            source_job_id=request.source_job_id,
            principal_id=principal,
            origin=origin,
            **{k: proof[k] for k in ("device_fingerprint", "expected_firmware_sha256", "port")},
            source_plan=s.plan(proof["plan_hash"]),
            command="one_q",
            fresh_response="APP_STOPPED_then_HOME_READY",
            max_seconds=15,
            max_receive_bytes=16384,
            effects=[
                "One q exits DERBY and may save app state. "
                "Serial open/close may reset via drivers.",
                "No capture, reset command, flash, reconnect or retry. "
                "Resolve only this DERBY capture review after fresh HOME and confirmed close.",
            ],
        )
        loop = asyncio.get_running_loop()
        e = Recovery(plan, proof, backend, b.clock() + 15)
        digest = digest_plan(plan)
        s.put_plan(digest, plan)
        s.enqueue_requested(request.job_id, digest, principal)
        try:
            self._validate(e)
            e.execution_deadline = e.deadline
            # The explicit request consumes the source before starting any USB
            # worker. A lost response, cancellation or restart cannot re-arm it.
            s.consume_home_attempt(request.source_job_id, request.job_id, proof, digest, principal)
        except Exception:
            s.ledger.transition(request.job_id, "cancelled", "home_recovery_not_admitted")
            raise
        e.state = "running"
        b.active_job = request.job_id
        self.entries[digest] = e
        e.task = loop.create_task(self._run(e), name="bounded-home-recovery")
        return dict(ok=True, plan_hash=digest, plan=plan, job_id=request.job_id, state="running")

    def _entry(self, digest: str) -> Recovery:
        if digest not in self.entries:
            raise BrokerError("home_recovery_not_available")
        return self.entries[digest]

    def cancel(self, request: CancelHomeRecovery, principal: str) -> dict[str, Any]:
        e = self._entry(request.plan_hash)
        if e.plan["job_id"] != request.job_id or e.plan["principal_id"] != principal:
            raise BrokerError("home_recovery_scope_mismatch")
        already = e.stop.is_set()
        e.stop.set()
        if not already:
            with self.broker._store().db:
                self.broker._store().home_audit(
                    e.plan["source_job_id"], request.job_id, "cancel_requested"
                )
        if e.state == "queued":
            e.state = "cancelled"
            self.broker._store().ledger.transition(
                request.job_id, "cancelled", "home_recovery_cancelled", expect={"queued"}
            )
        return dict(ok=True, stop_requested=True, state=e.state)

    def maintain(self) -> None:
        for e in self.entries.values():
            if e.state == "queued" and (
                not math.isfinite(self.broker.clock()) or self.broker.clock() >= e.deadline
            ):
                e.stop.set()
                e.state = "expired"
                self.broker._store().ledger.transition(
                    e.plan["job_id"], "cancelled", "home_recovery_expired", expect={"queued"}
                )
                with self.broker._store().db:
                    self.broker._store().home_audit(
                        e.plan["source_job_id"], e.plan["job_id"], "expired"
                    )

    async def close(self) -> None:
        for e in self.entries.values():
            e.stop.set()
            if e.state == "queued":
                e.state = "cancelled"
                self.broker._store().ledger.transition(
                    e.plan["job_id"], "cancelled", "home_recovery_shutdown", expect={"queued"}
                )
                with self.broker._store().db:
                    self.broker._store().home_audit(
                        e.plan["source_job_id"], e.plan["job_id"], "shutdown"
                    )
        for e in self.entries.values():
            if e.task is not None:
                await asyncio.shield(e.task)

    async def _run(self, e: Recovery) -> None:
        b, s, p = self.broker, self.broker._store(), e.plan
        loop = asyncio.get_running_loop()

        def live() -> bool:
            try:
                self._validate(e, running=True)
                return True
            except Exception:
                return False

        async def check() -> bool:
            return live()

        def thread_live() -> bool:
            return asyncio.run_coroutine_threadsafe(check(), loop).result(timeout=5)

        def record_stage(name: str) -> None:
            if name not in {
                "serial_open",
                "q_written",
                "fresh_home_observed",
                "serial_close",
                "serial_close_unconfirmed",
            }:
                return
            try:
                s.set_stage(p["job_id"], name)
                with s.db:
                    s.home_audit(p["source_job_id"], p["job_id"], name)
            except Exception:
                # A queued audit failure must invalidate the eventual commit too.
                e.stop.set()

        def emit(name: str) -> None:
            loop.call_soon_threadsafe(record_stage, name)

        try:
            if not live():
                raise BrokerError("home_recovery_authority_changed")
            e.backend.active += 1
            worker = asyncio.create_task(
                asyncio.to_thread(recover_serial, e.backend, p, thread_live, e.stop, emit)
            )
            while not worker.done():
                try:
                    await asyncio.shield(worker)
                except asyncio.CancelledError:
                    e.stop.set()
                except Exception:
                    if not worker.done():
                        raise
            drained = loop.create_future()
            loop.call_soon(drained.set_result, None)
            await drained
            worker.result()
            if not e.backend.ownership_uncertain:
                e.backend.active -= 1
            self._validate(e, running=True)
            if e.backend.active or e.backend.hardware_connected:
                raise BrokerError("home_recovery_close_unconfirmed")
            s.finish_home_recovery(
                p["source_job_id"], p["job_id"], e.proof["plan_hash"], digest_plan(p), live
            )
            e.state = "succeeded"
        except Exception:
            if not e.backend.ownership_uncertain:
                e.backend.active = 0
            e.state = "needs_review"
            s.ledger.transition(p["job_id"], "needs_review", "home_recovery_failed_no_retry")
            with s.db:
                s.home_audit(p["source_job_id"], p["job_id"], "failed_no_retry")
        finally:
            if (
                not e.backend.active
                and e.backend.hardware_connected is False
                and e.backend.ownership_uncertain is False
            ):
                b.active_job = None


def recover_serial(
    backend: SerialBackend,
    plan: dict[str, Any],
    live: Any,
    stop: threading.Event,
    emit: Any = lambda _: None,
) -> None:
    """One port, one q, no reset/reopen; stale data is drained, never discarded."""
    if live() is not True or stop.is_set():
        raise SerialFailure("operation_interrupted_review_required")
    if not math.isfinite(backend.clock()):
        raise SerialFailure("serial_deadline")
    raw = backend.factory()
    port = RecoveryPort(raw, live, stop, plan["max_receive_bytes"])
    reader: Reader | None = None
    try:
        port.dtr = False
        port.rts = False
        port.port = plan["port"]
        port.open()
        backend.hardware_connected = True
        emit("serial_open")
        identity = resolve_port(backend.config.selector, backend.inventory())
        if fingerprint(identity) != plan["device_fingerprint"]:
            raise SerialFailure("device_identity_changed")
        reader = Reader(port, lambda _: None, backend.clock, backend.clock() + 15)
        reader.derby_mode = True
        reader.capture_quiet_boundary()
        if any(line.startswith((b"ESP-ROM:", b"rst:0x", b"APP_ID")) for line in reader.lines):
            raise SerialFailure("unexpected_app_launch")
        # These were read BEFORE input, not a reply. No cached/pre-open HOME
        # survives the empty physical receive boundary into this command window.
        reader.lines.clear()
        reader.receive_diagnostics.begin_command("home_quit")
        if port.write(b"q") != 1:
            raise SerialFailure("incomplete_serial_write")
        reader.receive_diagnostics.write_completed()
        emit("q_written")
        until = min(backend.clock() + 8, reader.deadline)
        while b"HOME_READY" not in reader.lines:
            if backend.clock() >= until:
                raise SerialFailure("serial_marker_timeout")
            reader.read_once()
        reader.capture_quiet_boundary()
        lines = reader.lines
        if (
            lines.count(b"APP_STOPPED") != 1
            or lines.count(b"HOME_READY") != 1
            or lines.index(b"APP_STOPPED") >= lines.index(b"HOME_READY")
            or any(line.startswith(b"APP_ID") for line in lines)
            or any(line.startswith((b"ESP-ROM:", b"rst:0x")) for line in lines)
        ):
            raise SerialFailure("teardown_sequence_invalid")
        emit("fresh_home_observed")
    except SerialFailure as failure:
        code = failure.code
        failure.__traceback__ = failure.__context__ = failure.__cause__ = None
        raise SerialFailure(code) from None
    finally:
        if reader is not None:
            reader.clear_transient()
        try:
            backend._close_restart_port(raw)
        except SerialFailure:
            emit("serial_close_unconfirmed")
            raise
        emit("serial_close")
