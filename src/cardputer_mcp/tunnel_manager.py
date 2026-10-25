"""Worker-driven, bounded lifecycle for one explicitly owned Windows Tunnel job.

Call connect/poll/disconnect from the GUI's worker, never its Tk event thread.
There is no reconnect loop, broker stop, device action, credential persistence,
process-name lookup, or native-output collection in this manager. Snapshots and
callbacks contain fixed codes and validated scalar values only.

Health contracts are pinned to official openai/tunnel-client v0.0.15:
https://github.com/openai/tunnel-client/blob/v0.0.15/docs/health.md
https://github.com/openai/tunnel-client/blob/v0.0.15/pkg/healthstate/operations.go
https://github.com/openai/tunnel-client/blob/v0.0.15/pkg/controlplane/health.go
A recent successful poll is historical transport evidence, not app discovery.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import ssl
import threading
import time
from collections.abc import Callable, Iterable
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

import httpcore2
from filelock import FileLock, Timeout
from httpcore2 import SOCKET_OPTION

from .client import SURFACE_TOOLS, call
from .client import connect as connect_mcp
from .config import load_settings
from .tunnel import (
    TunnelError,
    TunnelSettings,
    check_listener,
    child_environment,
    client_arguments,
    lock_path,
    verify_release,
)
from .tunnel_checks import (
    GUI_CONFIGURATION,
    GUI_IDENTITY,
    PLACEHOLDER_TUNNEL_ID,
    configuration_refusal,
    discovery_refusal,
    gui_status_refusal,
    identity_refusal,
    read_broker,
)

CONFIG_MAX_BYTES = 64 * 1024
HEALTH_MAX_BYTES = 24 * 1024
HEALTH_TOTAL_SECONDS = 2.0
HEALTH_TIMEOUT_SECONDS = 0.5
BROKER_TIMEOUT_SECONDS = 5.0
STOP_TIMEOUT_SECONDS = 5.0
READINESS_TIMEOUT_SECONDS = 45.0
# Two seconds covers timestamp rounding; this is not an old-process grace period.
CLOCK_SKEW_SECONDS = 2.0
POLL_FRESH_SECONDS = 120.0


class OwnedProcess(Protocol):
    def poll(self) -> int | None: ...
    def active_count(self) -> int: ...
    def stop(self, timeout: float) -> bool: ...
    def close(self) -> Any: ...


class OwnershipLock(Protocol):
    def acquire(self, *, timeout: float) -> Any: ...
    def release(self) -> Any: ...


def _new_process(args: list[str], env: dict[str, str], cwd: Path) -> OwnedProcess:
    # Import lazily: configuration and offline tests remain usable off Windows.
    from .windows_process import start

    return start(args, env, cwd)


def _new_lock() -> OwnershipLock:
    # GUI worker invocations may use different threads. Lifetime ownership cannot
    # use filelock's default thread-local accounting.
    return FileLock(str(lock_path()), thread_local=False)


def _discovery_gate(catalog: Any) -> None:
    if code := discovery_refusal(catalog, SURFACE_TOOLS, "tunnel_broker_discovery_failed"):
        raise TunnelError(code)


async def _broker_probe_async(url: str, bearer: str) -> dict[str, Any]:
    # GUI policy: every read is made; only discovery is judged inside the session.
    async with asyncio.timeout(BROKER_TIMEOUT_SECONDS):
        identity, status, _ = await read_broker(
            connect_mcp, call, url, bearer, after_catalog=_discovery_gate
        )
    return {**identity, "backend": status.get("backend"), "status": status}


def broker_probe(url: str, bearer: str) -> dict[str, Any]:
    """Read-only authenticated MCP probe; active jobs and device owners are allowed."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(_broker_probe_async(url, bearer))
    raise TunnelError("tunnel_worker_required")


class _DeadlineStream(httpcore2.NetworkStream):
    """Clamp every socket wait to the remaining overall health budget."""

    def __init__(self, stream: httpcore2.NetworkStream, deadline: float) -> None:
        self._stream = stream
        self._deadline = deadline

    def _budget(self, timeout: float | None, error: type[Exception]) -> float:
        remaining = self._deadline - time.monotonic()
        if remaining <= 0:
            raise error("health_deadline")
        return remaining if timeout is None else min(timeout, remaining)

    def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        return self._stream.read(max_bytes, self._budget(timeout, httpcore2.ReadTimeout))

    def write(self, buffer: bytes, timeout: float | None = None) -> None:
        self._stream.write(buffer, self._budget(timeout, httpcore2.WriteTimeout))

    def close(self) -> None:
        self._stream.close()

    def start_tls(
        self,
        ssl_context: ssl.SSLContext,
        server_hostname: str | None = None,
        timeout: float | None = None,
    ) -> httpcore2.NetworkStream:
        raise httpcore2.UnsupportedProtocol("health_plain_http_only")

    def get_extra_info(self, info: str) -> Any:
        return self._stream.get_extra_info(info)


class _DeadlineBackend(httpcore2.NetworkBackend):
    """Synchronous loopback TCP only; no event loop, resolver thread, or proxy."""

    def __init__(self, deadline: float) -> None:
        self._inner = httpcore2.SyncBackend()
        self._deadline = deadline

    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[SOCKET_OPTION] | None = None,
    ) -> httpcore2.NetworkStream:
        if host != "127.0.0.1":
            raise httpcore2.ConnectError("health_loopback_only")
        remaining = self._deadline - time.monotonic()
        if remaining <= 0:
            raise httpcore2.ConnectTimeout("health_deadline")
        wait = remaining if timeout is None else min(timeout, remaining)
        stream = self._inner.connect_tcp(host, port, wait, local_address, socket_options)
        return _DeadlineStream(stream, self._deadline)

    def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Iterable[SOCKET_OPTION] | None = None,
    ) -> httpcore2.NetworkStream:
        raise httpcore2.ConnectError("health_tcp_only")

    def sleep(self, seconds: float) -> None:
        # Retries are disabled; never wait past the overall budget either way.
        time.sleep(max(0.0, min(seconds, self._deadline - time.monotonic())))


def health_probe(port: int) -> dict[str, Any]:
    """Bound the entire loopback observation, including headers, to two seconds.

    Synchronous on the calling worker thread: no per-poll event loop is created,
    so a stalled Windows event-loop setup or teardown cannot wedge status polls.
    Every connect/read/write wait is clamped to the remaining overall budget, so
    a peer that drips header or body bytes is still cut off at the deadline.
    Uses no proxy, redirect, compressed body, credential, or abandoned worker.
    Raw component bytes are capped at the pinned native contract's 24 KiB.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        # Blocking I/O would stall the caller's loop; keep the worker contract.
        raise TunnelError("tunnel_worker_required")
    result: dict[str, Any] = {"ready_status": 0, "control_status": 0, "control_plane": None}
    deadline = time.monotonic() + HEALTH_TOTAL_SECONDS
    timeouts = {key: HEALTH_TIMEOUT_SECONDS for key in ("connect", "read", "write", "pool")}
    try:
        with httpcore2.ConnectionPool(
            network_backend=_DeadlineBackend(deadline), max_connections=1, retries=0
        ) as pool:
            for path, key, limit in (
                ("/readyz", "ready_status", 512),
                ("/health/control-plane", "control_status", HEALTH_MAX_BYTES),
            ):
                try:
                    with pool.stream(
                        "GET",
                        f"http://127.0.0.1:{port}{path}",
                        headers=[(b"Accept", b"*/*"), (b"Accept-Encoding", b"identity")],
                        extensions={"timeout": timeouts},
                    ) as response:
                        encodings = [
                            value.strip().lower()
                            for name, value in response.headers
                            if name.lower() == b"content-encoding"
                        ]
                        if any(value != b"identity" for value in encodings):
                            continue
                        data = bytearray()
                        for chunk in response.iter_stream():
                            if time.monotonic() >= deadline:
                                raise TimeoutError("health_deadline")
                            if len(data) + len(chunk) > limit:
                                raise ValueError("health_body_limit")
                            data.extend(chunk)
                        result[key] = response.status
                        if key == "control_status" and response.status == 200:
                            payload = json.loads(data)
                            if isinstance(payload, dict):
                                result["control_plane"] = payload
                except Exception:
                    result[key] = 0
    except Exception:
        # Never return raw HTTP exception, body, error text, or header.
        pass
    return result


def _timestamp(value: Any) -> float | None:
    if not isinstance(value, str) or len(value) > 40 or not value.endswith("Z"):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        return parsed if math.isfinite(parsed) else None
    except (ValueError, OverflowError, OSError):
        return None


def classify_control_plane(payload: Any, *, now: float, started_at: float) -> tuple[bool, str]:
    """Reduce the pinned schema to safe evidence, never forward native messages."""
    if not isinstance(payload, dict):
        return False, "tunnel_cloud_poll_unobserved"
    if (
        type(payload.get("schema_version")) is not int
        or payload["schema_version"] != 1
        or payload.get("component") != "control-plane"
        or payload.get("limited", False) is not False
    ):
        return False, "tunnel_health_schema_unsupported"
    snapshot_at = _timestamp(payload.get("snapshot_at"))
    if snapshot_at is None or not -CLOCK_SKEW_SECONDS <= now - snapshot_at <= 10:
        return False, "tunnel_health_stale"
    details = payload.get("details")
    if not isinstance(details, dict):
        return False, "tunnel_cloud_poll_unobserved"
    failures = details.get("consecutive_failures")
    if type(failures) is not int or failures < 0:
        return False, "tunnel_health_schema_unsupported"
    status, state = payload.get("status"), payload.get("state")
    if failures > 0:
        if (
            status == "degraded"
            and state == "backoff"
            and type(details.get("http_status")) is int
            and details["http_status"] in (401, 403)
        ):
            return False, "tunnel_cloud_auth_failed"
        return False, "tunnel_cloud_poll_degraded"
    if (
        status != "ok"
        or state not in ("polling", "idle", "backpressured")
        or details.get("failure_category", "") != ""
        or details.get("http_status", 0) != 0
    ):
        return False, "tunnel_cloud_poll_unobserved"
    success = _timestamp(details.get("last_success"))
    if success is None:
        return False, "tunnel_cloud_poll_unobserved"
    # Native long polls can legitimately last up to ten minutes. Extend the
    # evidence window only to a validated, bounded configured deadline.
    deadline = details.get("deadline_seconds", 0)
    freshness = POLL_FRESH_SECONDS
    if type(deadline) in (int, float) and math.isfinite(deadline) and 0 <= deadline <= 600:
        freshness = max(freshness, deadline + 10)
    if success < started_at - CLOCK_SKEW_SECONDS or not (
        -CLOCK_SKEW_SECONDS <= now - success <= freshness
    ):
        return False, "tunnel_cloud_poll_stale"
    return True, "tunnel_cloud_poll_observed"


class TunnelManager:
    """One owned job, one lifetime lock, no stored runtime credentials.

    Operations serialize without queuing repeated clicks: competing operations
    immediately return tunnel_busy. snapshot() never waits for network/process
    I/O. Injected dependencies support deterministic offline lifecycle tests.
    """

    def __init__(
        self,
        *,
        process_factory: Callable[[list[str], dict[str, str], Path], OwnedProcess] | None = None,
        broker_probe: Callable[[str, str], dict[str, Any]] = broker_probe,
        health_probe: Callable[[int], dict[str, Any]] = health_probe,
        verify_client: Callable[[TunnelSettings], None] = verify_release,
        listener_check: Callable[[int], None] = check_listener,
        lock_factory: Callable[[], OwnershipLock] = _new_lock,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
        on_event: Callable[[str], None] | None = None,
    ) -> None:
        self._operation = threading.Lock()
        self._state_lock = threading.RLock()
        self._factory = process_factory or _new_process
        self._requires_windows = process_factory is None
        self._broker_probe = broker_probe
        self._health_probe = health_probe
        self._verify = verify_client
        self._listener_check = listener_check
        self._lock_factory = lock_factory
        self._clock = clock
        self._monotonic = monotonic
        self._event = on_event
        self._settings: TunnelSettings | None = None
        self._config_digest: str | None = None
        self._url = ""
        self._enable_serial = False
        self._process: OwnedProcess | None = None
        self._ownership_lock: OwnershipLock | None = None
        self._started_at = 0.0
        self._started_monotonic = 0.0
        self._state = "disconnected"
        self._code = "tunnel_not_configured"
        self._alive = False
        self._ready = False
        self._cloud = False
        self._uncertain = False
        self._backend: str | None = None
        self._exit_code: int | None = None
        # True while poll's network probe runs outside the operation lock.
        self._probing = False

    def __repr__(self) -> str:
        return "TunnelManager(<private configuration; credentials not retained>)"

    def snapshot(self) -> dict[str, Any]:
        with self._state_lock:
            return {
                "state": self._state,
                "status_code": self._code,
                "configured": self._settings is not None,
                "backend": self._backend,
                "process_alive": self._alive,
                "owned": self._process is not None or self._ownership_lock is not None,
                "exit_code": self._exit_code,
                "local_readiness": self._ready,
                "cloud_poll_observed": self._cloud,
                "app_verified": False,
                "ownership_uncertain": self._uncertain,
                "busy": self._operation.locked() or self._probing,
            }

    def _result(self) -> dict[str, Any]:
        return {**self.snapshot(), "busy": False}

    def _busy(self) -> dict[str, Any]:
        return {**self.snapshot(), "status_code": "tunnel_busy", "busy": True}

    def _set(self, code: str, *, state: str | None = None) -> None:
        with self._state_lock:
            changed = self._code != code
            self._code = code
            if state is not None:
                self._state = state
        if changed and self._event is not None:
            try:
                self._event(code)
            except Exception:
                pass

    @staticmethod
    def _configuration_digest(settings: TunnelSettings) -> str:
        path = settings.broker_config
        if not path.is_file() or path.stat().st_size > CONFIG_MAX_BYTES:
            raise TunnelError("invalid_private_tunnel_config")
        with path.open("rb") as handle:
            content = handle.read(CONFIG_MAX_BYTES + 1)
        if len(content) > CONFIG_MAX_BYTES:
            raise TunnelError("invalid_private_tunnel_config")
        return hashlib.sha256(content).hexdigest()

    def _check_configuration_unchanged(self, settings: TunnelSettings) -> None:
        try:
            unchanged = self._config_digest == self._configuration_digest(settings)
        except Exception:
            unchanged = False
        if not unchanged:
            raise TunnelError("tunnel_configuration_changed")

    def _validate_configuration(self, settings: TunnelSettings, url: str) -> str:
        if settings.tunnel_id == PLACEHOLDER_TUNNEL_ID:
            raise TunnelError("tunnel_id_placeholder")
        broker = load_settings(settings.broker_config)
        if code := configuration_refusal(
            settings, broker, GUI_CONFIGURATION, enable_serial=self._enable_serial, url=url
        ):
            raise TunnelError(code)
        return broker.backend

    def configure(
        self, settings: TunnelSettings, broker_url: str, *, enable_serial: bool = False
    ) -> dict[str, Any]:
        if not self._operation.acquire(blocking=False):
            return self._busy()
        try:
            if self._state == "closed":
                self._set("tunnel_manager_closed")
            elif self._process is not None or self._ownership_lock is not None:
                self._set("tunnel_configuration_locked")
            else:
                try:
                    # Revalidate even model_copy/model_construct inputs; metadata only.
                    validated = TunnelSettings.model_validate(settings.model_dump())
                    self._enable_serial = enable_serial is True
                    digest = self._configuration_digest(validated)
                    backend = self._validate_configuration(validated, broker_url)
                    if digest != self._configuration_digest(validated):
                        raise TunnelError("tunnel_configuration_changed")
                    with self._state_lock:
                        self._settings = validated
                        self._config_digest = digest
                        self._url = broker_url
                        self._backend = backend
                    self._set("tunnel_configured", state="disconnected")
                except Exception as error:
                    with self._state_lock:
                        self._settings = None
                        self._config_digest = None
                        self._url = ""
                        self._backend = None
                    self._set(self._safe_error(error, "invalid_private_tunnel_config"))
        finally:
            self._operation.release()
        return self._result()

    @staticmethod
    def _safe_error(error: Exception, fallback: str) -> str:
        allowed = {
            "invalid_private_tunnel_config",
            "tunnel_id_placeholder",
            "tunnel_configuration_changed",
            "tunnel_broker_url_mismatch",
            "health_port_conflicts_with_broker",
            "serial_tunnel_requires_explicit_enable_serial",
            "client_release_verification_failed",
            "tunnel_health_listener_exists",
            "invalid_runtime_key",
            "invalid_mcp_bearer",
            "tunnel_worker_required",
            "tunnel_broker_discovery_failed",
            "tunnel_broker_auth_failed",
            "tunnel_launcher_already_running",
            "tunnel_broker_origin_rejected",
            "tunnel_broker_scope_rejected",
            "tunnel_broker_backend_mismatch",
            "tunnel_broker_ingress_mismatch",
            "tunnel_config_conflict",
            "tunnel_broker_status_invalid",
        }
        if isinstance(error, TunnelError) and str(error) in allowed:
            return str(error)
        return fallback

    def _check_broker(self, result: dict[str, Any], backend: str, ingress_enabled: bool) -> None:
        if code := identity_refusal(
            result, GUI_IDENTITY, ingress_enabled=ingress_enabled, backend=backend
        ) or gui_status_refusal(result.get("status")):
            raise TunnelError(code)
        # No idle, owner, job, hardware, queue, lease, or session gate. Restarting
        # the transport must not replay/cancel/pause any device work.

    def connect(self, api_key: str, bearer: str) -> dict[str, Any]:
        if not self._operation.acquire(blocking=False):
            return self._busy()
        env: dict[str, str] = {}
        try:
            if self._state == "closed":
                self._set("tunnel_manager_closed")
                return self._result()
            if self._process is not None or self._ownership_lock is not None:
                return self._result()  # Idempotent; never spawn a second job.
            settings = self._settings
            if settings is None:
                self._set("tunnel_not_configured")
                return self._result()
            if self._requires_windows and os.name != "nt":
                self._set("tunnel_windows_required")
                return self._result()
            self._set("tunnel_connecting", state="connecting")
            try:
                self._check_configuration_unchanged(settings)
                env = child_environment(dict(os.environ), api_key, bearer)
                backend = self._validate_configuration(settings, self._url)
                self._verify(settings)
                ownership = self._lock_factory()
                try:
                    ownership.acquire(timeout=0)
                except Timeout:
                    raise TunnelError("tunnel_launcher_already_running") from None
                self._ownership_lock = ownership
                self._listener_check(settings.health_port)
                result = self._broker_probe(self._url, bearer)
                self._check_broker(result, backend, settings.ingress_enabled)
                # Drop the structured broker result too: it may contain private
                # job/session metadata that the Tunnel UI does not need.
                del result
                self._check_configuration_unchanged(settings)
                self._started_at = self._clock()
                self._started_monotonic = self._monotonic()
                self._exit_code = None
                self._process = self._factory(
                    client_arguments(settings, self._url), env, settings.client_exe.parent
                )
                alive = self._process.poll() is None
                with self._state_lock:
                    self._alive = alive
                    self._ready = self._cloud = self._uncertain = False
                    self._backend = backend
                if not self._alive:
                    self._cleanup("tunnel_client_exited")
                else:
                    self._set("tunnel_started", state="running")
            except Exception as error:
                code = self._safe_error(error, "tunnel_connect_failed")
                from .windows_process import WindowsProcessError

                if isinstance(error, WindowsProcessError) and error.owned is not None:
                    self._process = error.owned
                self._cleanup(code)
        finally:
            env.clear()
            # Python strings cannot be reliably zeroized; no reference is kept
            # in manager state, a retry queue, a callback, or a persisted file.
            api_key = bearer = ""
            self._operation.release()
        return self._result()

    def _cleanup(self, code: str) -> bool:
        """Retain both ownership handles on any unconfirmed teardown."""
        confirmed = True
        if self._process is not None:
            try:
                confirmed = self._process.stop(STOP_TIMEOUT_SECONDS) is True
                confirmed = confirmed and self._process.active_count() == 0
                if confirmed:
                    exit_code = self._process.poll()
                    if type(exit_code) is int and self._exit_code is None:
                        self._exit_code = exit_code
                if confirmed:
                    self._process.close()
            except Exception:
                confirmed = False
        if confirmed and self._ownership_lock is not None:
            try:
                self._ownership_lock.release()
            except Exception:
                confirmed = False
        alive = False
        if not confirmed and self._process is not None:
            try:
                alive = self._process.poll() is None
            except Exception:
                pass
        with self._state_lock:
            self._ready = self._cloud = False
            self._uncertain = not confirmed
            if confirmed:
                self._process = None
                self._ownership_lock = None
                self._alive = False
            else:
                self._alive = alive
        if confirmed:
            self._set(code, state="disconnected")
        else:
            self._set("tunnel_stop_unconfirmed", state="stop_uncertain")
        return confirmed

    def _check_exit(self) -> bool:
        assert self._process is not None
        try:
            exit_code = self._process.poll()
        except Exception:
            self._cleanup("tunnel_process_status_failed")
            return True
        if exit_code is None:
            return False
        if type(exit_code) is int:
            self._exit_code = exit_code
        self._cleanup("tunnel_client_exited")
        return True

    def poll(self) -> dict[str, Any]:
        """Observe the owned client without holding the operation lock over HTTP.

        The bounded loopback probe runs between two short critical sections, so
        a slow or wedged probe can never block disconnect/close of the owned
        client. Evidence from a probe whose client was stopped or replaced while
        it ran is discarded; the probe never touches process or ownership state.
        """
        if not self._operation.acquire(blocking=False):
            return self._busy()
        try:
            if self._probing:
                return self._busy()
            if self._process is None or self._state != "running":
                return self._result()
            try:
                if self._check_exit():
                    return self._result()
                assert self._settings is not None
                port = self._settings.health_port
            except Exception:
                with self._state_lock:
                    self._ready = self._cloud = False
                self._set("tunnel_health_unavailable")
                return self._result()
            process = self._process
            with self._state_lock:
                self._probing = True
        finally:
            self._operation.release()
        try:
            health: dict[str, Any] | None = self._health_probe(port)
        except Exception:
            health = None
        try:
            if not self._operation.acquire(blocking=False):
                # A disconnect/close owns the client now; its result is authoritative.
                return self._busy()
            try:
                if self._process is not process or self._state != "running":
                    return self._result()  # Stale evidence for a stopped/replaced client.
                self._apply_health(health)
            finally:
                self._operation.release()
        finally:
            with self._state_lock:
                self._probing = False
        return self._result()

    def _apply_health(self, health: dict[str, Any] | None) -> None:
        try:
            if health is None:
                raise TunnelError("tunnel_health_unavailable")
            if self._check_exit():
                return
            ready = health.get("ready_status") == 200
            cloud, code = classify_control_plane(
                health.get("control_plane") if health.get("control_status") == 200 else None,
                now=self._clock(),
                started_at=self._started_at,
            )
            with self._state_lock:
                self._alive = True
                self._ready = ready
                self._cloud = cloud
            if code == "tunnel_cloud_auth_failed":
                self._cleanup(code)
            else:
                if code == "tunnel_cloud_poll_unobserved":
                    if ready:
                        code = "tunnel_local_ready"
                    elif self._monotonic() - self._started_monotonic >= READINESS_TIMEOUT_SECONDS:
                        code = "tunnel_readiness_unconfirmed"
                    else:
                        code = "tunnel_starting"
                self._set(code)
        except Exception:
            with self._state_lock:
                self._ready = self._cloud = False
            self._set("tunnel_health_unavailable")

    def disconnect(self) -> dict[str, Any]:
        if not self._operation.acquire(blocking=False):
            return self._busy()
        try:
            if self._state != "closed":
                self._set("tunnel_disconnecting", state="disconnecting")
                self._cleanup("tunnel_disconnected")
        finally:
            self._operation.release()
        return self._result()

    def close(self) -> bool:
        """GUI may destroy its window only after True; False retains ownership."""
        if not self._operation.acquire(blocking=False):
            return False
        try:
            if self._state == "closed":
                return True
            if not self._cleanup("tunnel_disconnected"):
                return False
            self._set("tunnel_closed", state="closed")
            return True
        finally:
            self._operation.release()
