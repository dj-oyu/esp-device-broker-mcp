"""Bounded principal-scoped Server-Sent Events streams on the broker event loop.

One ``GET /operator/events`` response is one stream. It starts with a full
``snapshot`` event, then sends a ``delta`` (or ``reset`` after a principal change)
for each observed revision change and an SSE comment heartbeat while idle. Every
data event is one compact JSON envelope in the shape the GUI reducer validates.

A stream keeps only its own last published baseline. ``Last-Event-ID`` is not
interpreted: every (re)connect receives a full snapshot under a new cursor.
"""

import asyncio
import json
import math
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from starlette.types import Receive, Send

from .broker import BrokerError
from .models import Origin

MAX_STREAMS = 8
HEARTBEAT_SECONDS = 25.0
HEARTBEAT = b": ping\n\n"


@dataclass(frozen=True)
class _Baseline:
    cursor: dict[str, Any]
    principal_id: str
    state: dict[str, Any]
    timeline: dict[str, Any]
    logs_revision: int
    source_revision: int
    expiry_at: float | None


def _event(payload: dict[str, Any]) -> bytes:
    """One SSE data event. Compact JSON never contains a raw CR or LF."""
    body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    cursor = payload.get("cursor")
    lines = []
    if isinstance(cursor, dict):
        lines.append(f"id: {cursor['epoch']}:{cursor['sequence']}")
    lines.append("data: " + body)
    return ("\n".join(lines) + "\n\n").encode("utf-8")


class OperatorLive:
    def __init__(self, broker: Any, timeline: Callable[[Any], dict[str, Any]]) -> None:
        self.broker = broker
        self.timeline = timeline
        self.streams = 0
        self.closed = False
        self._wakes: set[Callable[[], None]] = set()
        self._sequence = 0

    def close(self) -> None:
        """End every open stream and refuse new ones (server shutdown begins)."""
        self.closed = True
        for wake in tuple(self._wakes):
            wake()

    def _ending(self) -> bool:
        return self.closed or bool(self.broker.closing)

    @staticmethod
    async def _disconnect(receive: Receive) -> None:
        while (await receive())["type"] != "http.disconnect":
            pass

    def _capture(self, principal: Any) -> _Baseline:
        for _ in range(3):
            revision = self.broker.operator_changes.revision()
            # Both projections and the revision checks are synchronous. A maintenance
            # commit inside either projection retries the pair before publication.
            state = {"ok": True, **self.broker.operator_snapshot()}
            timeline = {"ok": True, **self.timeline(principal)}
            if revision == self.broker.operator_changes.revision():
                # A publication has its own cursor identity. Reconnect snapshots
                # can differ as TTLs elapse even without a committed notification;
                # never reuse a cursor for a different projected baseline.
                self._sequence += 1
                return _Baseline(
                    {"epoch": self.broker.epoch, "sequence": self._sequence},
                    principal.principal_id,
                    state,
                    timeline,
                    revision[1],
                    revision[0],
                    self._next_expiry(),
                )
        raise BrokerError("unavailable")

    def _next_expiry(self) -> float | None:
        broker = self.broker
        now = broker.clock()
        deadlines = [retained.deadline for retained in broker.captures.values()]
        deadlines.extend(retained.deadline for retained in broker.device_observations.values())
        deadlines.extend(
            work.deadline for work in broker.work_sessions.values() if work.state == "open"
        )
        deadlines.extend(broker.device_requests[key].deadline for key in broker.waiters)
        if broker.session is not None:
            deadlines.append(broker.session.deadline)
        log_now = time.monotonic()
        for service in (broker.firmware_source, broker.firmware_build, broker.firmware_flash):
            deadlines.extend(now + log.deadline - log_now for log in tuple(service.logs.values()))
        deadlines.extend(
            now + run.events.logs.deadline - log_now
            for run in tuple(broker.app_sessions.jobs.values())
        )
        future = [deadline for deadline in deadlines if math.isfinite(deadline) and deadline > now]
        return min(future) if future else None

    def _refresh(self, base: _Baseline) -> None:
        """Changes that commit without a notification, checked on every wake."""
        # Maintenance prunes expiry only. It keeps the broker's existing authority
        # callback graph; report read grants are checked at this read boundary.
        self.broker.maintenance_tick()
        if base.source_revision == self.broker.operator_changes.revision()[0]:
            for card in base.timeline["cards"]:
                if card.get("device_observation") is not None:
                    # A report owner can lose read permission without a broker
                    # event. Only baselines with report metadata need this bounded
                    # check (at most HEARTBEAT_SECONDS after revocation).
                    current = self.broker.operator_device_observation_metadata(card["job_id"])
                    if current is None:
                        self.broker.operator_changes.notify()
                        break
        if (
            base.expiry_at is not None
            and self.broker.clock() >= base.expiry_at
            and base.source_revision == self.broker.operator_changes.revision()[0]
        ):
            self.broker.operator_changes.notify_logs()

    @staticmethod
    def _full(kind: str, current: _Baseline) -> dict[str, Any]:
        return dict(
            schema_version=1,
            kind=kind,
            cursor=current.cursor,
            state=current.state,
            timeline=current.timeline,
        )

    @staticmethod
    def _delta(base: _Baseline, current: _Baseline) -> dict[str, Any]:
        previous = {card["job_id"]: card for card in base.timeline["cards"]}
        cards = current.timeline["cards"]
        present = {card["job_id"] for card in cards}
        result = dict(
            schema_version=1,
            kind="delta",
            cursor=current.cursor,
            base_cursor=base.cursor,
            upsert=[card for card in cards if previous.get(card["job_id"]) != card],
            remove=[job_id for job_id in previous if job_id not in present],
            order=[card["job_id"] for card in cards],
            has_more=current.timeline["has_more"],
            logs_changed=current.logs_revision != base.logs_revision,
        )
        if current.state != base.state:
            result["state"] = current.state
        return result

    async def stream(
        self,
        principal: Any,
        authenticate: Callable[[], Any | None],
        receive: Receive,
        send: Send,
    ) -> None:
        """Serve one stream until disconnect, refusal, closing or shutdown.

        A ``BrokerError`` raised here means no response has started, so the caller's
        JSON error boundary still applies. After the response starts, a refusal is
        one final ``{"ok": false, "error": code}`` event and the stream ends.
        """
        if self._ending() or self.streams >= MAX_STREAMS:
            raise BrokerError("unavailable")
        event = asyncio.Event()
        # Subscribe before the first capture so no commit falls between them.
        unsubscribe = self.broker.operator_changes.subscribe(event.set)
        self._wakes.add(event.set)
        self.streams += 1
        disconnected: asyncio.Task[None] | None = None
        try:
            self.broker.maintenance_tick()
            base = self._capture(principal)
            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [
                        (b"content-type", b"text/event-stream; charset=utf-8"),
                        (b"cache-control", b"no-store"),
                    ],
                }
            )
            disconnected = asyncio.create_task(self._disconnect(receive))
            await self._body(send, _event({"ok": True, **self._full("snapshot", base)}))
            try:
                await self._follow(base, authenticate, event, disconnected, send)
            except BrokerError as error:
                await self._body(send, _event({"ok": False, "error": str(error)}))
            except Exception:
                await self._body(send, _event({"ok": False, "error": "operator_request_failed"}))
            await send({"type": "http.response.body", "body": b"", "more_body": False})
        finally:
            unsubscribe()
            self._wakes.discard(event.set)
            self.streams -= 1
            if disconnected is not None:
                disconnected.cancel()
                await asyncio.gather(disconnected, return_exceptions=True)

    @staticmethod
    async def _body(send: Send, data: bytes) -> None:
        await send({"type": "http.response.body", "body": data, "more_body": True})

    async def _follow(
        self,
        base: _Baseline,
        authenticate: Callable[[], Any | None],
        event: asyncio.Event,
        disconnected: asyncio.Task[None],
        send: Send,
    ) -> None:
        loop = asyncio.get_running_loop()
        last_sent = loop.time()
        while True:
            timeout = max(0.0, HEARTBEAT_SECONDS - (loop.time() - last_sent))
            if base.expiry_at is not None:
                timeout = min(timeout, max(0.0, base.expiry_at - self.broker.clock()))
            if not event.is_set():
                changed = asyncio.create_task(event.wait())
                try:
                    await asyncio.wait(
                        (changed, disconnected),
                        timeout=timeout,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                finally:
                    changed.cancel()
                    await asyncio.gather(changed, return_exceptions=True)
            if disconnected.done() or self._ending():
                return
            # Coalesce: every notification so far is covered by one capture below.
            event.clear()
            principal = authenticate()
            if principal is None:
                raise BrokerError("authentication_required")
            if principal.origin != Origin.LOCAL or "control" not in principal.scopes:
                raise BrokerError("local_operator_required")
            self._refresh(base)
            if (
                principal.principal_id != base.principal_id
                or base.source_revision != self.broker.operator_changes.revision()[0]
            ):
                current = self._capture(principal)
                if current.principal_id != base.principal_id:
                    message = self._full("reset", current)
                else:
                    message = self._delta(base, current)
                await self._body(send, _event({"ok": True, **message}))
                base = current
                last_sent = loop.time()
            elif loop.time() - last_sent >= HEARTBEAT_SECONDS:
                await self._body(send, HEARTBEAT)
                last_sent = loop.time()
