"""Owned, read-only async effects for the GUI's main-thread live reducer.

The worker never calls Tk or mutates a broker job. Generations fence both queued
deliveries and requests, and a delivery gap requires an explicit new subscription.
"""

import asyncio
import json
import math
import re
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

import httpx2

from .gui_live_retry import RETRYABLE_CODES, RETRYABLE_HTTP, SAFE_CODES, diagnostic
from .models import BuildLogRequest, JobRequest
from .operator_client import OperatorClient, OperatorClientError, safe_refusal

MAX_RESPONSE_BYTES = 512 * 1024
MAX_DELIVERIES = 32
STARTUP_WAIT_SECONDS = 2.0
STARTUP_FAILED = "Cannot start the live reader. Reopen the local operator window."
UNREACHABLE = "Cannot reach the local broker. Reconnect to resume live updates."
AUTH_FAILED = "Local operator authentication failed. Check CARDPUTER_LOCAL_TOKEN."
INVALID_RESPONSE = "The broker returned an invalid live response. Reconnect to resynchronize."
REJECTED = "The broker rejected the live request. Reconnect to resume live updates."
DELIVERY_GAP = "Live updates exceeded the delivery limit. Reconnect to resynchronize."
IMAGE_EXPIRED = "The retained image is no longer available. Refresh before requesting its preview."


@dataclass(frozen=True)
class TransportEvent:
    kind: Literal["message", "disconnected", "logs", "image"]
    generation: int
    payload: dict[str, Any]
    started_at: float
    diagnostic: dict[str, Any] | None = None
    received_utc: str | None = None


class _TransportFailure(Exception):
    """Carries only fixed, safe diagnostics."""

    def __init__(
        self,
        error: str,
        *,
        reason: str | None = None,
        status: int | None = None,
        code: str | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(error)
        self.reason = reason or ("invalid_response" if error == INVALID_RESPONSE else "rejected")
        self.status, self.code, self.retryable = status, code, retryable


def _cursor(value: Any) -> dict[str, Any]:
    if (
        not isinstance(value, dict)
        or set(value) != {"epoch", "sequence"}
        or not isinstance(value.get("epoch"), str)
        or re.fullmatch("[a-f0-9]{32}", value["epoch"]) is None
        or type(value.get("sequence")) is not int
        or not 0 <= value["sequence"] <= 2**63 - 1
    ):
        raise _TransportFailure(INVALID_RESPONSE, status=200)
    return {"epoch": value["epoch"], "sequence": value["sequence"]}


def _image_available(expires_at: float | None) -> bool:
    if expires_at is None:
        return True
    if type(expires_at) not in (int, float):
        return False
    try:
        return math.isfinite(expires_at) and time.monotonic() < expires_at
    except OverflowError:
        return False


class LiveRunner:
    """One event loop, one held live read, one log read, and one explicit image read.

    ``drain`` belongs to the GUI thread. A caller must increment its generation
    when reconnecting; old generations and closed-view results are discarded.
    """

    def __init__(self, api: OperatorClient) -> None:
        # Revalidate even when a caller supplies a duck-typed OperatorClient.
        credentials = OperatorClient(api.base_url, api._token)
        self._base_url = credentials.base_url
        self._token = credentials._token
        self._lock = threading.Lock()
        self._events: deque[TransportEvent] = deque()
        self._generation: int | None = None
        self._gap_generation: int | None = None
        self._paused_generation: int | None = None
        self._closed = False
        self._startup_failed = False
        self._ready = threading.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._client: httpx2.AsyncClient | None = None
        self._live_task: asyncio.Task[None] | None = None
        self._log_task: asyncio.Task[None] | None = None
        self._image_task: asyncio.Task[None] | None = None
        self._log_serial = 0
        self._image_serial = 0
        self._thread = threading.Thread(target=self._run, name="operator-live-reader", daemon=True)
        self._thread.start()
        ready = self._ready.wait(timeout=STARTUP_WAIT_SECONDS)
        with self._lock:
            failed = not ready or self._startup_failed or self._client is None
            if failed:
                self._closed = True
        if failed:
            raise OperatorClientError(STARTUP_FAILED) from None

    def _make_client(self) -> httpx2.AsyncClient:
        return httpx2.AsyncClient(
            trust_env=False,
            follow_redirects=False,
            timeout=httpx2.Timeout(35.0, connect=2.0, write=5.0, pool=2.0),
            limits=httpx2.Limits(max_connections=3, max_keepalive_connections=3),
        )

    def _run(self) -> None:
        loop = None
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._loop = loop
            self._client = self._make_client()
            with self._lock:
                if self._closed:
                    return
            self._ready.set()
            loop.run_forever()
        except Exception:
            # Thread tracebacks can contain transport URLs, headers or tokens.
            with self._lock:
                self._startup_failed = True
        finally:
            self._ready.set()
            if loop is not None:
                try:
                    pending = asyncio.all_tasks(loop)
                    for task in pending:
                        task.cancel()
                    if pending:
                        loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
                    if self._client is not None:
                        loop.run_until_complete(self._client.aclose())
                    loop.run_until_complete(loop.shutdown_asyncgens())
                except Exception:
                    pass
                finally:
                    loop.close()

    def start(self, generation: int) -> None:
        self.reconnect(generation)

    def reconnect(self, generation: int) -> None:
        if type(generation) is not int:
            raise ValueError("generation must be an integer")
        with self._lock:
            if self._closed or (self._generation is not None and generation <= self._generation):
                return
            self._generation = generation
            self._gap_generation = None
            self._paused_generation = None
            self._events.clear()
            self._log_serial += 1
            self._image_serial += 1
            loop = self._loop
            if loop is not None:
                loop.call_soon_threadsafe(self._begin, generation)

    def _current(self, generation: int) -> bool:
        with self._lock:
            return (
                not self._closed
                and self._generation == generation
                and self._gap_generation != generation
                and self._paused_generation != generation
            )

    def pause(self, generation: int) -> None:
        """Cancel reads after reducer validation fails; reconnect explicitly resumes."""
        with self._lock:
            if self._closed or self._generation != generation:
                return
            self._paused_generation = generation
            self._events.clear()
            self._log_serial += 1
            self._image_serial += 1
            loop = self._loop
            if loop is not None:
                loop.call_soon_threadsafe(self._pause_reads, generation)

    def _pause_reads(self, generation: int) -> None:
        with self._lock:
            if self._generation != generation or self._paused_generation != generation:
                return
        self._cancel_reads()

    def _cancel_reads(self) -> None:
        for task in (self._live_task, self._log_task, self._image_task):
            if task is not None:
                task.cancel()

    def _begin(self, generation: int) -> None:
        if not self._current(generation):
            return
        self._cancel_reads()
        self._live_task = asyncio.create_task(self._subscribe(generation))
        self._log_task = None
        self._image_task = None

    def read_logs(self, generation: int, job_id: str, cursor: dict[str, Any] | None = None) -> int:
        request = BuildLogRequest.model_validate(
            {
                "job_id": job_id,
                "include_source_cache": False,
                "cursor": cursor,
                "levels": ["error", "warning", "info", "debug", "unknown"],
                "context": 0,
                "limit": 100,
            }
        ).model_dump(mode="json")
        with self._lock:
            self._log_serial += 1
            serial = self._log_serial
            if (
                self._closed
                or self._generation != generation
                or self._gap_generation == generation
                or self._paused_generation == generation
            ):
                return serial
            loop = self._loop
            if loop is not None:
                loop.call_soon_threadsafe(self._begin_logs, generation, serial, request)
            return serial

    def _begin_logs(self, generation: int, serial: int, request: dict[str, Any]) -> None:
        if not self._current(generation):
            return
        with self._lock:
            if serial != self._log_serial:
                return
        if self._log_task is not None:
            self._log_task.cancel()
        self._log_task = asyncio.create_task(self._read_logs(generation, serial, request))

    def read_image(self, generation: int, job_id: str, *, expires_at: float | None = None) -> int:
        """Read a retained preview once; never request another capture or device job."""
        request = JobRequest(job_id=job_id).model_dump(mode="json")
        expired = not _image_available(expires_at)
        with self._lock:
            if (
                self._closed
                or self._generation != generation
                or self._gap_generation == generation
                or self._paused_generation == generation
            ):
                return 0
            self._image_serial += 1
            serial = self._image_serial
            loop = self._loop
            if loop is not None:
                loop.call_soon_threadsafe(
                    self._begin_image, generation, serial, request, expires_at, expired
                )
            return serial

    def _begin_image(
        self,
        generation: int,
        serial: int,
        request: dict[str, Any],
        expires_at: float | None,
        expired: bool,
    ) -> None:
        if not self._current(generation):
            return
        with self._lock:
            if serial != self._image_serial:
                return
        if self._image_task is not None:
            self._image_task.cancel()
        if expired:
            self._image_task = None
            self._emit(
                TransportEvent(
                    "image",
                    generation,
                    {"job_id": request["job_id"], "request_id": serial, "error": IMAGE_EXPIRED},
                    time.monotonic(),
                )
            )
            return
        self._image_task = asyncio.create_task(
            self._read_image(generation, serial, request, expires_at)
        )

    def drain(self) -> list[TransportEvent]:
        with self._lock:
            events = list(self._events)
            self._events.clear()
        return events

    def _emit(self, event: TransportEvent) -> bool:
        overflow = False
        with self._lock:
            if (
                self._closed
                or self._generation != event.generation
                or self._gap_generation == event.generation
                or self._paused_generation == event.generation
            ):
                return False
            if len(self._events) >= MAX_DELIVERIES:
                self._events.clear()
                self._gap_generation = event.generation
                self._events.append(
                    TransportEvent(
                        "disconnected",
                        event.generation,
                        {"error": DELIVERY_GAP},
                        event.started_at,
                        diagnostic("delivery_gap", event.generation, 0),
                    )
                )
                overflow = True
            else:
                self._events.append(event)
        if overflow:
            self._cancel_reads()
        return not overflow

    async def _request(
        self,
        path: str,
        payload: dict[str, Any],
        *,
        read_once: bool = False,
        expires_at: float | None = None,
    ) -> dict[str, Any]:
        client = self._client
        if client is None:
            raise _TransportFailure(UNREACHABLE)
        request = client.build_request(
            "POST",
            self._base_url + path,
            json=payload,
            headers={"Authorization": f"Bearer {self._token}", "Accept": "application/json"},
            timeout=httpx2.Timeout(5.0, connect=2.0) if read_once else client.timeout,
        )
        # AsyncClient owns a jar, but no response cookie is an operator credential.
        request.headers.pop("cookie", None)
        if not _image_available(expires_at):
            raise _TransportFailure(IMAGE_EXPIRED)
        try:
            response = await client.send(request, stream=True, follow_redirects=False)
            refusal_received = False
            try:
                client.cookies.clear()
                if response.status_code in (401, 403):
                    if read_once:
                        refusal_received = True
                        return safe_refusal(
                            {
                                "ok": False,
                                "error": "authentication_required"
                                if response.status_code == 401
                                else "local_operator_required",
                            }
                        )
                    raise _TransportFailure(
                        AUTH_FAILED, reason="authentication_failed", status=response.status_code
                    )
                if response.status_code != 200:
                    raise _TransportFailure(
                        REJECTED,
                        reason="http_rejected",
                        status=response.status_code,
                        retryable=response.status_code in RETRYABLE_HTTP,
                    )
                declared = response.headers.get("content-length")
                if declared is not None:
                    try:
                        if int(declared) > MAX_RESPONSE_BYTES:
                            raise _TransportFailure(INVALID_RESPONSE, status=200)
                    except ValueError:
                        raise _TransportFailure(INVALID_RESPONSE, status=200) from None
                body = bytearray()
                async for chunk in response.aiter_bytes(chunk_size=8192):
                    if len(body) + len(chunk) > MAX_RESPONSE_BYTES:
                        raise _TransportFailure(INVALID_RESPONSE, status=200)
                    body.extend(chunk)
                try:
                    data = json.loads(body)
                except (ValueError, RecursionError):
                    raise _TransportFailure(INVALID_RESPONSE, status=200) from None
                if not isinstance(data, dict) or type(data.get("ok")) is not bool:
                    raise _TransportFailure(INVALID_RESPONSE, status=200)
                if read_once and data["ok"] is False:
                    refusal_received = True
                    return safe_refusal(data)
                if not read_once and data["ok"] is not True:
                    code = data.get("error")
                    safe_code = code if type(code) is str and code in SAFE_CODES else None
                    raise _TransportFailure(
                        REJECTED,
                        reason="broker_rejected",
                        status=200,
                        code=safe_code,
                        retryable=safe_code in RETRYABLE_CODES,
                    )
                return data
            finally:
                primary_failure = sys.exception()
                try:
                    await response.aclose()
                except httpx2.HTTPError:
                    # Cleanup must not turn a known auth/permanent rejection
                    # into a retryable network error. Cancellation still wins.
                    if primary_failure is None and not refusal_received:
                        raise
        except httpx2.HTTPError as error:
            reasons = {
                "ConnectTimeout": "connect_timeout",
                "ReadTimeout": "read_timeout",
                "WriteTimeout": "write_timeout",
                "PoolTimeout": "pool_timeout",
                "ConnectError": "connect_failed",
                "ReadError": "read_failed",
                "WriteError": "write_failed",
                "RemoteProtocolError": "peer_disconnected",
            }
            reason = reasons.get(type(error).__name__, "transport_error")
            raise _TransportFailure(
                UNREACHABLE, reason=reason, retryable=reason != "transport_error"
            ) from None

    async def _subscribe(self, generation: int) -> None:
        cursor = None
        started_at = time.monotonic()
        try:
            while self._current(generation):
                started_at = time.monotonic()
                data = await self._request("/operator/live", {"cursor": cursor})
                if (
                    type(data.get("schema_version")) is not int
                    or data["schema_version"] != 1
                    or data.get("kind") not in ("snapshot", "reset", "delta", "heartbeat")
                ):
                    raise _TransportFailure(INVALID_RESPONSE, status=200)
                next_cursor = _cursor(data.get("cursor"))
                kind = data["kind"]
                if (
                    (cursor is None and kind not in ("snapshot", "reset"))
                    or (kind == "delta" and _cursor(data.get("base_cursor")) != cursor)
                    or (kind == "heartbeat" and next_cursor != cursor)
                ):
                    raise _TransportFailure(INVALID_RESPONSE, status=200)
                if not self._emit(TransportEvent("message", generation, data, started_at)):
                    return
                cursor = next_cursor
        except _TransportFailure as error:
            self._emit(
                TransportEvent(
                    "disconnected",
                    generation,
                    {"error": str(error)},
                    started_at,
                    diagnostic(
                        error.reason,
                        generation,
                        int((time.monotonic() - started_at) * 1000),
                        status=error.status,
                        code=error.code,
                        retryable=error.retryable,
                    ),
                )
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            # Never deliver exception text: transports may include URLs or headers.
            self._emit(
                TransportEvent(
                    "disconnected",
                    generation,
                    {"error": UNREACHABLE},
                    started_at,
                    diagnostic(
                        "unexpected_exception",
                        generation,
                        int((time.monotonic() - started_at) * 1000),
                    ),
                )
            )

    async def _read_logs(self, generation: int, serial: int, request: dict[str, Any]) -> None:
        started_at = time.monotonic()
        received_utc = None
        payload: dict[str, Any] = {"job_id": request["job_id"], "request_id": serial}
        try:
            payload["response"] = await self._request("/operator/job-logs", request, read_once=True)
            received_utc = datetime.now(UTC).isoformat(timespec="seconds")
        except _TransportFailure as error:
            payload["error"] = str(error)
        except asyncio.CancelledError:
            raise
        except Exception:
            payload["error"] = UNREACHABLE
        with self._lock:
            current = serial == self._log_serial
        if current:
            self._emit(
                TransportEvent("logs", generation, payload, started_at, received_utc=received_utc)
            )

    async def _read_image(
        self,
        generation: int,
        serial: int,
        request: dict[str, Any],
        expires_at: float | None,
    ) -> None:
        started_at = time.monotonic()
        payload: dict[str, Any] = {"job_id": request["job_id"], "request_id": serial}
        try:
            payload["response"] = await self._request(
                "/operator/image", request, read_once=True, expires_at=expires_at
            )
        except _TransportFailure as error:
            payload["error"] = str(error)
        except asyncio.CancelledError:
            raise
        except Exception:
            payload["error"] = UNREACHABLE
        with self._lock:
            current = serial == self._image_serial
        if current:
            self._emit(TransportEvent("image", generation, payload, started_at))

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._events.clear()
            loop = self._loop
            if loop is not None and not loop.is_closed():
                loop.call_soon_threadsafe(self._stop)
        self._thread.join(timeout=1.0)

    def _stop(self) -> None:
        self._cancel_reads()
        assert self._loop is not None
        self._loop.stop()
