"""Application jobs inside the resident worker's existing ownership boundary."""

import asyncio
import hashlib
import json
import queue
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from .app_events import AppEvents
from .app_models import AppCommandRequest, AppSessionOptions
from .app_transport import ControlConnection
from .app_wire import Op, Reply, Result, State
from .backend import Backend, DeviceOperationFailure, MockBackend

ACTIONS = {"accept": 1, "back": 2, "up": 3, "right": 4, "down": 5, "left": 6}
EFFECTS = [
    "Open/close the selected USB serial port; drivers may glitch control lines",
    "Launch the registered app; fresh destroys the dormant guest and unsaved state",
    "Typed action.tap only within the fixed action/count/rate/time limits",
    "Suspend preserves the guest; stop and final device TTL destroy the controlled guest",
    "Cancellation/timeout/lease loss permits bounded STOP/CLOSE cleanup only",
    "Redacted bounded logs go only to the requesting principal; no automatic replay/reopen",
    "Transport success does not certify JavaScript, rendering, saving or benchmark quality",
]


def catalog_hash(descriptors: list[dict[str, Any]]) -> str:
    encoded = json.dumps(descriptors, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


class Link(Protocol):
    def catalog(self) -> list[Reply]: ...
    def send(
        self,
        op: Op,
        session: int,
        command_id: int,
        *,
        boot: int = 0,
        run: int = 0,
        seq: int = 0,
        fence: int = 0,
        ttl: int = 0,
        app_id: str = "",
        arg: int = 0,
    ) -> Reply: ...
    def poll(self, timeout: float = 0.05) -> None: ...


class AppFailure(DeviceOperationFailure):
    def __init__(self, code: str) -> None:
        super().__init__("serial_operation_uncertain")
        self.code = (
            code
            if code
            in {
                "outcome_unknown",
                "catalog_changed",
                "unknown_app",
                "unsupported",
                "app_authority_lost",
                "app_teardown_unconfirmed",
                "serial_close_unconfirmed",
                "stale_boot",
                "stale_run",
                "app_open_failed",
            }
            else "outcome_unknown"
        )
        self.diagnostic = {"code": self.code}


@dataclass
class RunningApp:
    principal: str
    options: AppSessionOptions
    events: AppEvents
    pending: queue.Queue[AppCommandRequest] = field(default_factory=lambda: queue.Queue(5))
    cancelled: threading.Event = field(default_factory=threading.Event)
    lock: threading.RLock = field(default_factory=threading.RLock)
    commands: dict[int, tuple[str, dict[str, Any]]] = field(default_factory=dict)
    boot: int = 0
    run: int = 0
    session: int = 0
    fence: int = 0
    next_seq: int = 2
    submitted_seq: int = 2
    state: str = "opening"
    running: bool = True
    last_input: float = float("-inf")

    def snapshot(self) -> dict[str, Any]:
        return dict(
            boot_id=self.boot,
            run_id=self.run,
            control_session=self.session,
            next_seq=self.next_seq,
            next_submit_seq=self.submitted_seq,
            state=self.state,
        )

    def command(self, request: AppCommandRequest) -> dict[str, Any]:
        with self.lock:
            if request.boot_id != self.boot:
                raise ValueError("stale_boot")
            if request.run_id != self.run:
                raise ValueError("stale_run")
            if request.control_session != self.session:
                raise ValueError("stale_session")
            if request.operation == "status":
                if request.command_id == 0:
                    return self.snapshot()
                status_entry = self.commands.get(request.command_id)
                if status_entry:
                    return dict(status_entry[1])
                return dict(command_id=request.command_id, state="outcome_unknown")
            content = request.model_dump_json(exclude={"session"})
            old = self.commands.get(request.command_id)
            if old is not None:
                if old[0] != content:
                    raise ValueError("conflict")
                return {**old[1], "duplicate": True}
            if (
                not self.running
                or self.cancelled.is_set()
                or self.state not in ("running", "suspended")
            ):
                raise ValueError("app_input_stopped")
            if (
                request.operation == "action.tap"
                and request.action not in self.options.allowed_actions
            ):
                raise ValueError("action_not_in_plan")
            if request.seq != self.submitted_seq or request.command_id == 1:
                raise ValueError("out_of_order")
            cleanup = request.operation == "stop"
            if not cleanup and len(self.commands) >= self.options.max_commands:
                raise ValueError("limit")
            if self.pending.full() or (not cleanup and self.pending.qsize() >= 4):
                raise ValueError("queue_full")
            if (
                not cleanup
                and time.monotonic() - self.last_input < 1 / self.options.rate_per_second
            ):
                raise ValueError("rate_limit")
            entry = dict(command_id=request.command_id, state="queued", sent=False, duplicate=False)
            self.pending.put_nowait(request)
            self.commands[request.command_id] = (content, entry)
            self.submitted_seq += 1
            self.last_input = time.monotonic()
            return dict(entry)

    def reply(self, reply: Reply) -> None:
        result = reply.result.name.lower()
        self.events.emit(
            "ack",
            command_id=reply.command_id,
            result=result,
            operation=reply.op.name.lower(),
            next_seq=reply.next_seq,
            state=reply.state.name.lower(),
            boot_id=reply.boot,
            run_id=reply.run,
            control_session=reply.session,
        )
        with self.lock:
            if reply.command_id in self.commands:
                old, _ = self.commands[reply.command_id]
                self.commands[reply.command_id] = (
                    old,
                    dict(
                        command_id=reply.command_id,
                        state=result,
                        sent=True,
                        duplicate=False,
                        next_seq=reply.next_seq,
                        result=result,
                    ),
                )


class MockLink:
    """Synthetic endpoint using the same client/job path; never hardware evidence."""

    def __init__(self, control: Callable[[Reply], None]) -> None:
        self.control = control
        self.boot = secrets.randbits(63) or 1
        self.offer = 1
        self.run = 0
        self.generation = 0
        self.state = State.NO_SESSION
        self.fence = 0
        self.seq = 1
        self.app = ""
        self.ledger: dict[int, Reply] = {}

    def catalog(self) -> list[Reply]:
        return [
            Reply(
                self.offer,
                0,
                self.boot,
                self.run,
                0,
                0,
                Result.OK,
                self.state,
                Op.CATALOG,
                catalog_index=i,
                catalog_count=3,
                flags=7 if i < 2 else 0,
                app_id=app,
                title=title,
            )
            for i, (app, title) in enumerate(
                [
                    ("local.hello", "HELLO"),
                    ("local.stress", "STRESS TEST"),
                    ("local.vmprobe", "VM PROBE"),
                ]
            )
        ]

    def send(self, op: Op, session: int, command_id: int, **fields: Any) -> Reply:
        boot, run, seq, fence = (fields.get(k, 0) for k in ("boot", "run", "seq", "fence"))
        result = Result.OK
        if op == Op.CATALOG:
            return Reply(
                self.offer,
                0,
                self.boot,
                self.run,
                0,
                0,
                Result.OK,
                self.state,
                op,
                catalog_index=65535,
                catalog_count=3,
                app_id="00" * 32,
                title="synthetic-not-device",
            )
        if boot != self.boot:
            result = Result.STALE_BOOT
        elif session != self.offer:
            result = Result.STALE_SESSION
        elif op == Op.OPEN:
            self.fence, self.state = fence, State.READY
        elif fence != self.fence:
            result = Result.STALE_LEASE
        elif op == Op.STATUS:
            if command_id:
                return self.ledger.get(
                    command_id,
                    Reply(
                        session,
                        command_id,
                        self.boot,
                        self.run,
                        seq,
                        fence,
                        Result.OUTCOME_UNKNOWN,
                        self.state,
                        op,
                        next_seq=self.seq,
                    ),
                )
        elif seq != self.seq:
            result = Result.OUT_OF_ORDER
        elif op == Op.START:
            if fields.get("arg") == 1:
                if (
                    self.state != State.SUSPENDED
                    or run != self.run
                    or fields.get("app_id") != self.app
                ):
                    result = Result.STALE_RUN
                else:
                    self.state = State.RUNNING
            else:
                self.generation += 1
                self.run = self.generation
                self.app, self.state = fields.get("app_id", ""), State.RUNNING
        elif run != self.run:
            result = Result.STALE_RUN
        elif op == Op.TAP:
            result = Result.CONSUMED if self.state == State.RUNNING else Result.BLOCKED
        elif op == Op.SUSPEND:
            self.state = State.SUSPENDED
        elif op == Op.STOP:
            self.state = State.STOPPED
        elif op == Op.CLOSE:
            if self.state != State.STOPPED:
                result = Result.BUSY
            else:
                self.state = State.NO_SESSION
        if command_id and op != Op.STATUS:
            self.seq += 1
        reply = Reply(
            session,
            command_id,
            self.boot,
            self.run,
            seq,
            fence,
            result,
            self.state,
            op,
            next_seq=self.seq,
        )
        if command_id and op != Op.STATUS:
            self.ledger[command_id] = reply
        self.control(reply)
        if op == Op.CLOSE and result == Result.OK:
            self.offer += 1
            self.ledger.clear()
            self.seq = 1
        return reply

    def poll(self, timeout: float = 0.05) -> None:
        time.sleep(min(timeout, 0.01))


class AppSessions:
    def __init__(self) -> None:
        self.jobs: dict[str, RunningApp] = {}
        self.on_logs_change: Callable[[], None] | None = None
        self.catalog_cache: dict[str, Any] | None = None
        self.mock_link: MockLink | None = None

    def list_apps(self) -> dict[str, Any]:
        if self.catalog_cache is None:
            return dict(
                apps=[],
                source="unobserved",
                fresh=False,
                observed_at=None,
                firmware_verification="unverified",
                protocol="app-control-v1",
            )
        fresh = time.time() - self.catalog_cache["observed_at"] <= 300
        return {**self.catalog_cache, "fresh": fresh}

    def prepare(self, backend: Backend, options: AppSessionOptions) -> dict[str, Any]:
        if options.start == "resume":
            raise ValueError("unsupported")
        binding: dict[str, Any] = {"source": "unobserved", "expected": None}
        if self.catalog_cache is not None:
            binding = dict(
                source="cached",
                expected=self.catalog_cache["catalog_sha256"],
                observed_at=self.catalog_cache["observed_at"],
                boot_id=self.catalog_cache["boot_id"],
                firmware_sha256=self.catalog_cache["firmware_sha256"],
                firmware_version=self.catalog_cache["firmware_version"],
            )
        details: dict[str, Any] = dict(
            stages=[
                "app_open",
                "app_handshake",
                "app_launch",
                "app_running",
                "app_stopping",
                "app_closing",
            ],
            app=options.model_dump(mode="json"),
            app_protocol="app-control-v1",
            effects=EFFECTS,
            catalog_binding=binding,
        )
        if backend.name == "serial":
            from .ports import resolve_port
            from .serial_backend import SerialBackend, fingerprint

            if not isinstance(backend, SerialBackend):
                raise ValueError("backend_not_available")
            port = resolve_port(backend.config.selector, backend.inventory())
            details.update(
                port=port.port,
                device_fingerprint=fingerprint(port),
                expected_firmware_sha256=backend.config.expected_firmware_sha256,
            )
        return details

    async def execute(
        self,
        backend: Backend,
        plan: dict[str, Any],
        principal: str,
        stage: Callable[[str], None],
        live: Callable[[], bool],
        durable: Any = None,
    ) -> str:
        """``durable`` is the job's open JobLog; the broker closes it on every outcome."""
        options = AppSessionOptions.model_validate(plan["app"])
        for key in tuple(self.jobs):
            old = self.jobs[key]
            if not old.running and time.monotonic() >= old.events.deadline:
                del self.jobs[key]
        if len(self.jobs) >= 8:
            raise AppFailure("outcome_unknown")
        run = RunningApp(
            principal,
            options,
            AppEvents(principal, options.log_bytes, on_logs_change=self.on_logs_change),
        )
        run.events.attach_durable(durable)
        self.jobs[plan["job_id"]] = run
        loop = asyncio.get_running_loop()

        async def check() -> bool:
            return live() is True

        def authority() -> bool:
            try:
                return asyncio.run_coroutine_threadsafe(check(), loop).result(timeout=5)
            except Exception:
                return False

        if backend.active:
            raise AppFailure("outcome_unknown")
        backend.active += 1
        backend.calls += 1  # type: ignore[attr-defined]
        backend.max_active = max(backend.max_active, backend.active)

        def emit(name: str) -> None:
            loop.call_soon_threadsafe(stage, name)

        worker = loop.run_in_executor(None, self._execute_sync, backend, plan, run, emit, authority)
        try:
            while not worker.done():
                try:
                    await asyncio.shield(worker)
                except asyncio.CancelledError:
                    run.cancelled.set()
            return worker.result()
        finally:
            run.running = False
            if not getattr(backend, "ownership_uncertain", False):
                backend.active -= 1

    def _execute_sync(
        self,
        backend: Backend,
        plan: dict[str, Any],
        run: RunningApp,
        stage: Callable[[str], None],
        live: Callable[[], bool],
    ) -> str:
        from .ports import resolve_port
        from .serial_backend import SerialBackend, fingerprint

        port = None
        stopped = False
        open_attempted = False
        control_closed = False
        link: Link | None = None
        outcome = "app_session_stopped"
        try:
            if not live():
                raise AppFailure("app_authority_lost")
            stage("app_open")
            if isinstance(backend, SerialBackend):
                info = resolve_port(backend.config.selector, backend.inventory())
                if info.port != plan["port"] or fingerprint(info) != plan["device_fingerprint"]:
                    raise AppFailure("outcome_unknown")
                port = backend.factory()
                port.dtr = port.rts = False
                port.port = info.port
                port.open()
                backend.hardware_connected = True
                link = ControlConnection(port, log=run.events.log, control=run.reply)
            elif isinstance(backend, MockBackend):
                if self.mock_link is None:
                    self.mock_link = MockLink(run.reply)
                self.mock_link.control = run.reply
                link = self.mock_link
            else:
                raise AppFailure("outcome_unknown")
            stage("app_handshake")
            catalog = link.catalog()
            if not catalog or len(catalog) > 64:
                raise AppFailure("outcome_unknown")
            first = catalog[0]
            descriptors = [
                dict(
                    app_id=r.app_id,
                    title=r.title,
                    available=bool(r.flags & 1),
                    suspend=bool(r.flags & 2),
                    action_tap=bool(r.flags & 4),
                )
                for r in catalog
            ]
            digest = catalog_hash(descriptors)
            binding = plan["catalog_binding"]
            metadata = link.send(Op.CATALOG, 0, 0, seq=65535)
            if (
                metadata.result != Result.OK
                or metadata.catalog_index != 65535
                or metadata.boot != first.boot
                or metadata.session != first.session
                or metadata.catalog_count != len(catalog)
                or len(metadata.app_id) != 64
                or any(c not in "0123456789abcdef" for c in metadata.app_id)
            ):
                raise AppFailure("outcome_unknown")
            self.catalog_cache = dict(
                apps=descriptors,
                catalog_sha256=digest,
                boot_id=first.boot,
                observed_at=time.time(),
                source="synthetic_fixture" if backend.name == "mock" else "device",
                firmware_verification="device_reported_unattested",
                firmware_sha256=metadata.app_id,
                firmware_version=metadata.title,
                protocol="app-control-v1",
            )
            if binding["expected"] is not None and (
                digest != binding["expected"]
                or first.boot != binding["boot_id"]
                or metadata.app_id != binding["firmware_sha256"]
                or metadata.title != binding["firmware_version"]
            ):
                raise AppFailure("catalog_changed")
            configured = plan.get("expected_firmware_sha256")
            if configured and metadata.app_id != configured:
                raise AppFailure("catalog_changed")
            selected = next((d for d in descriptors if d["app_id"] == run.options.app_id), None)
            if selected is None:
                raise AppFailure("unknown_app")
            if not selected["available"]:
                raise AppFailure("unsupported")
            if run.options.expected_boot is not None and run.options.expected_boot != first.boot:
                raise AppFailure("stale_boot")
            run.boot, run.session, run.fence = first.boot, first.session, secrets.randbits(32) or 1
            if not live() or run.cancelled.is_set():
                raise AppFailure("app_authority_lost")
            open_attempted = True
            reply = link.send(
                Op.OPEN,
                run.session,
                0,
                boot=run.boot,
                fence=run.fence,
                ttl=min(90000, run.options.duration_ms + 3000),
            )
            self._ok(reply)
            stage("app_launch")
            if not live() or run.cancelled.is_set():
                raise AppFailure("app_authority_lost")
            reply = link.send(
                Op.START,
                run.session,
                1,
                boot=run.boot,
                run=run.options.expected_run or 0,
                seq=1,
                fence=run.fence,
                app_id=run.options.app_id,
                arg=int(run.options.start == "resume"),
            )
            self._ok(reply)
            run.run, run.next_seq, run.state = reply.run, reply.next_seq, "running"
            run.submitted_seq = reply.next_seq
            run.events.emit(
                "started",
                app_id=run.options.app_id,
                **run.snapshot(),
                synthetic=backend.name == "mock",
                catalog_sha256=digest,
            )
            stage("app_running")
            deadline = time.monotonic() + min(run.options.duration_ms / 1000, 87)
            while True:
                if run.cancelled.is_set():
                    outcome = "app_session_cancelled"
                    break
                if not live():
                    outcome = "app_authority_lost"
                    break
                if time.monotonic() >= deadline:
                    outcome = "app_session_timeout"
                    break
                try:
                    request = run.pending.get_nowait()
                except queue.Empty:
                    link.poll(timeout=0.05)
                    continue
                if run.cancelled.is_set():
                    outcome = "app_session_cancelled"
                    break
                if not live():
                    outcome = "app_authority_lost"
                    break
                with run.lock:
                    old, entry = run.commands[request.command_id]
                    run.commands[request.command_id] = (
                        old,
                        {**entry, "state": "sent", "sent": True},
                    )
                run.events.emit("sent", command_id=request.command_id, seq=request.seq)
                operation = {
                    "action.tap": Op.TAP,
                    "suspend": Op.SUSPEND,
                    "resume": Op.START,
                    "stop": Op.STOP,
                }[request.operation]
                reply = link.send(
                    operation,
                    run.session,
                    request.command_id,
                    boot=run.boot,
                    run=run.run,
                    seq=request.seq,
                    fence=run.fence,
                    app_id=run.options.app_id if operation == Op.START else "",
                    arg=ACTIONS[request.action] if request.action else int(operation == Op.START),
                )
                with run.lock:
                    run.next_seq = reply.next_seq
                    if reply.next_seq <= request.seq:
                        # A pre-admission refusal did not consume this sequence.
                        # Pending successors cannot silently skip over that gap.
                        while True:
                            try:
                                rejected = run.pending.get_nowait()
                            except queue.Empty:
                                break
                            content, entry = run.commands[rejected.command_id]
                            run.commands[rejected.command_id] = (
                                content,
                                {**entry, "state": "rejected_before_send"},
                            )
                            run.events.emit(
                                "ack",
                                command_id=rejected.command_id,
                                result="rejected_before_send",
                                next_seq=reply.next_seq,
                            )
                        run.submitted_seq = reply.next_seq
                run.reply(reply)
                run.state = reply.state.name.lower()
                if reply.result in (
                    Result.OUTCOME_UNKNOWN,
                    Result.STALE_BOOT,
                    Result.STALE_SESSION,
                    Result.STALE_LEASE,
                    Result.STALE_RUN,
                    Result.EXPIRED,
                ):
                    raise AppFailure("outcome_unknown")
                if (
                    operation == Op.STOP
                    and reply.result == Result.OK
                    and reply.state == State.STOPPED
                ):
                    stopped = True
                    break
            stage("app_stopping")
            run.running = False
            if not stopped:
                reply = link.send(
                    Op.STOP,
                    run.session,
                    0xFFFFFFFE,
                    boot=run.boot,
                    run=run.run,
                    seq=run.next_seq,
                    fence=run.fence,
                )
                self._ok(reply)
                if reply.state != State.STOPPED:
                    raise AppFailure("app_teardown_unconfirmed")
                run.next_seq, stopped = reply.next_seq, True
            stage("app_closing")
            reply = link.send(
                Op.CLOSE,
                run.session,
                0xFFFFFFFF,
                boot=run.boot,
                run=run.run,
                seq=run.next_seq,
                fence=run.fence,
            )
            self._ok(reply)
            control_closed = True
            run.state = "stopped"
            if outcome == "app_authority_lost":
                raise AppFailure(outcome)
            return outcome
        except Exception as failure:
            run.running = False
            run.events.emit("needs_review", reason="outcome_unknown")
            if open_attempted and not control_closed and link is not None:
                try:
                    status = link.send(
                        Op.STATUS, run.session, 0, boot=run.boot, run=0, fence=run.fence
                    )
                    if (
                        status.boot != run.boot
                        or status.session != run.session
                        or status.fence != run.fence
                        or status.result not in (Result.OK, Result.STALE_RUN)
                    ):
                        raise AppFailure("app_teardown_unconfirmed")
                    run.run, run.next_seq = status.run, status.next_seq
                    if status.state != State.STOPPED:
                        cleanup = link.send(
                            Op.STOP,
                            run.session,
                            0xFFFFFFFE,
                            boot=run.boot,
                            run=run.run,
                            seq=run.next_seq,
                            fence=run.fence,
                        )
                        self._ok(cleanup)
                        if cleanup.state != State.STOPPED:
                            raise AppFailure("app_teardown_unconfirmed")
                        run.next_seq = cleanup.next_seq
                    stopped = True
                    cleanup = link.send(
                        Op.CLOSE,
                        run.session,
                        0xFFFFFFFF,
                        boot=run.boot,
                        run=run.run,
                        seq=run.next_seq,
                        fence=run.fence,
                    )
                    self._ok(cleanup)
                    control_closed = True
                    run.state = "stopped"
                except Exception:
                    run.events.emit("needs_review", reason="app_teardown_unconfirmed")
            if isinstance(failure, AppFailure):
                raise
            raise AppFailure("outcome_unknown") from None
        finally:
            run.running = False
            with run.lock:
                for command_id, (content, entry) in tuple(run.commands.items()):
                    if entry["state"] in ("queued", "sent", "accepted"):
                        result = "outcome_unknown" if entry["sent"] else "cancelled_before_send"
                        run.commands[command_id] = (content, {**entry, "state": result})
                        run.events.emit(
                            "ack",
                            command_id=command_id,
                            result=result,
                            next_seq=run.next_seq,
                            state=run.state,
                        )
            if open_attempted and not stopped:
                run.events.emit("needs_review", reason="app_teardown_unconfirmed")
            if port is not None:
                try:
                    port.close()
                    if port.is_open:
                        raise ValueError
                except Exception:
                    backend.ownership_uncertain = True  # type: ignore[attr-defined]
                    backend._unclosed_port = port  # type: ignore[attr-defined]
                    backend.hardware_connected = True
                    run.events.emit("needs_review", reason="serial_close_unconfirmed")
                    raise AppFailure("serial_close_unconfirmed") from None
                else:
                    backend.hardware_connected = False
                    run.events.emit("port_closed", teardown_confirmed=stopped)
            run.events.emit("finished", state=run.state, teardown_confirmed=stopped)

    @staticmethod
    def _ok(reply: Reply) -> None:
        if reply.result not in (Result.OK, Result.CONSUMED):
            raise AppFailure(reply.result.name.lower())
