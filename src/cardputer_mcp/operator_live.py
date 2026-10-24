"""Bounded principal-scoped JSON subscriptions on the broker event loop."""

import asyncio
import math
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from starlette.types import Receive

from .broker import BrokerError

MAX_BASELINES = 32
MAX_WAITERS = 8
HEARTBEAT_SECONDS = 25.0


class LiveCursor(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    epoch: str = Field(pattern=r"^[a-f0-9]{32}$")
    sequence: int = Field(ge=0, le=2**63 - 1)


class LiveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    cursor: LiveCursor | None


@dataclass(frozen=True)
class _Baseline:
    state: dict[str, Any]
    timeline: dict[str, Any]
    logs_revision: int
    source_revision: int
    expiry_at: float | None


class OperatorLive:
    def __init__(self, broker: Any, timeline: Callable[[Any], dict[str, Any]]) -> None:
        self.broker = broker
        self.timeline = timeline
        self.baselines: OrderedDict[tuple[str, str, int], _Baseline] = OrderedDict()
        self.waiters = 0
        self._sequence = 0

    def _baseline(self, cursor: LiveCursor | None, principal: Any) -> _Baseline | None:
        if cursor is None or cursor.epoch != self.broker.epoch:
            return None
        key = (principal.principal_id, cursor.epoch, cursor.sequence)
        baseline = self.baselines.get(key)
        if baseline is not None:
            self.baselines.move_to_end(key)
        return baseline

    @staticmethod
    async def _disconnect(receive: Receive) -> None:
        while (await receive())["type"] != "http.disconnect":
            pass

    async def wait(self, cursor: LiveCursor | None, principal: Any, receive: Receive) -> bool:
        """Subscribe before revision comparison; false means the peer disconnected."""
        baseline = self._baseline(cursor, principal)
        if baseline is None:
            return True
        event = asyncio.Event()
        unsubscribe = self.broker.operator_changes.subscribe(event.set)
        counted = False
        tasks: list[asyncio.Task[Any]] = []
        try:
            if baseline.source_revision != self.broker.operator_changes.revision()[0]:
                return True
            if self.waiters >= MAX_WAITERS:
                raise BrokerError("unavailable")
            self.waiters += 1
            counted = True
            changed = asyncio.create_task(event.wait())
            disconnected = asyncio.create_task(self._disconnect(receive))
            tasks.extend((changed, disconnected))
            timeout = HEARTBEAT_SECONDS
            if baseline.expiry_at is not None:
                timeout = min(timeout, max(0.0, baseline.expiry_at - self.broker.clock()))
            done, _ = await asyncio.wait(
                tasks, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
            )
            return disconnected not in done
        finally:
            unsubscribe()
            if counted:
                self.waiters -= 1
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

    def _capture(self, principal: Any) -> tuple[dict[str, Any], _Baseline]:
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
                cursor = {"epoch": self.broker.epoch, "sequence": self._sequence}
                key = (principal.principal_id, cursor["epoch"], cursor["sequence"])
                baseline = _Baseline(state, timeline, revision[1], revision[0], self._next_expiry())
                self.baselines[key] = baseline
                while len(self.baselines) > MAX_BASELINES:
                    self.baselines.popitem(last=False)
                return cursor, baseline
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

    def response(self, cursor: LiveCursor | None, principal: Any) -> dict[str, Any]:
        base = self._baseline(cursor, principal)
        # Maintenance prunes expiry only. It keeps the broker's existing authority
        # callback graph; report read grants are checked at this read boundary.
        # Normal heartbeats never assemble a full state/timeline projection.
        self.broker.maintenance_tick()
        if base is not None and base.source_revision == self.broker.operator_changes.revision()[0]:
            for card in base.timeline["cards"]:
                if card.get("device_observation") is not None:
                    # A report owner can lose read permission without a broker
                    # event. Only baselines with report metadata need this bounded
                    # heartbeat check (at most HEARTBEAT_SECONDS after revocation).
                    current = self.broker.operator_device_observation_metadata(card["job_id"])
                    if current is None:
                        self.broker.operator_changes.notify()
                        break
        if base is not None and base.expiry_at is not None:
            if (
                self.broker.clock() >= base.expiry_at
                and base.source_revision == self.broker.operator_changes.revision()[0]
            ):
                self.broker.operator_changes.notify_logs()
        if base is not None and cursor is not None:
            if base.source_revision == self.broker.operator_changes.revision()[0]:
                return dict(schema_version=1, kind="heartbeat", cursor=cursor.model_dump())
        next_cursor, current = self._capture(principal)
        if base is None:
            return dict(
                schema_version=1,
                kind="snapshot" if cursor is None else "reset",
                cursor=next_cursor,
                state=current.state,
                timeline=current.timeline,
            )
        previous = {card["job_id"]: card for card in base.timeline["cards"]}
        cards = current.timeline["cards"]
        present = {card["job_id"] for card in cards}
        result = dict(
            schema_version=1,
            kind="delta",
            cursor=next_cursor,
            base_cursor=cursor.model_dump() if cursor else None,
            upsert=[card for card in cards if previous.get(card["job_id"]) != card],
            remove=[job_id for job_id in previous if job_id not in present],
            order=[card["job_id"] for card in cards],
            has_more=current.timeline["has_more"],
            logs_changed=current.logs_revision != base.logs_revision,
        )
        if current.state != base.state:
            result["state"] = current.state
        return result
