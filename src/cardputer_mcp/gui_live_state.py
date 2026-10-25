"""Pure reconciliation of the operator live projection; no IO or clock reads."""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Literal

from .gui_timeline import MAX_CARDS, MAX_RETENTION_IDENTITIES, TimelineCard, TimelineSnapshot

if TYPE_CHECKING:
    from .gui import QueueSnapshot

CONNECTING = "Connecting to live updates."
LIVE = "Live updates connected."
DISCONNECTED = "Live updates disconnected. Showing the last validated status."
RESYNC_REQUIRED = "Live updates lost continuity. Reconnect to refresh status."
CLOSED = "Live view closed."
DISCONNECT_MESSAGES: Mapping[str, str] = MappingProxyType(
    {
        "authentication_failed": (
            "Local broker authentication failed. Check the connection credentials, then reconnect."
        ),
        "delivery_gap": "Live updates exceeded the delivery limit. Reconnect to refresh status.",
        "invalid_response": (
            "The broker returned an invalid live response. Reconnect to refresh status."
        ),
        "unreachable": ("Cannot reach the local broker. Check that it is running, then reconnect."),
        "rejected": (
            "The broker rejected live updates. Check access and broker status, then reconnect."
        ),
    }
)


@dataclass(frozen=True)
class _Retention:
    key: tuple[str, ...]
    expires_at: float
    metadata_hash: str = ""


@dataclass(frozen=True)
class LiveState:
    connection: Literal["connecting", "live", "disconnected", "closed"] = "disconnected"
    generation: int = 0
    cursor: dict[str, Any] | None = None
    snapshot: QueueSnapshot | None = None
    timeline: TimelineSnapshot | None = None
    selected_job_id: str = ""
    message: str = DISCONNECTED
    _retention: tuple[_Retention, ...] = field(default=(), repr=False)

    def card(self, job_id: str) -> TimelineCard | None:
        if self.timeline is None:
            return None
        return next((card for card in self.timeline.cards if card.job_id == job_id), None)

    @property
    def selected_card(self) -> TimelineCard | None:
        return self.card(self.selected_job_id)

    @property
    def selection_missing(self) -> bool:
        """A retained local selection can outlive its bounded history row."""
        return bool(self.selected_job_id and self.selected_card is None)

    @property
    def resync_required(self) -> bool:
        return self.connection == "disconnected" and self.message == RESYNC_REQUIRED


def begin(state: LiveState) -> LiveState:
    """Start a new generation; its runner opens a new event stream (a full snapshot)."""
    if state.connection == "closed":
        return state
    return replace(
        state, generation=state.generation + 1, connection="connecting", message=CONNECTING
    )


def select(state: LiveState, job_id: str) -> LiveState:
    if state.connection == "closed" or (
        job_id != ""
        and (type(job_id) is not str or re.fullmatch(r"[\w-]{1,64}", job_id, re.ASCII) is None)
    ):
        return state
    return replace(state, selected_job_id=job_id)


def disconnect(state: LiveState, generation: int, reason: str | None = None) -> LiveState:
    """Select fixed local guidance from a reason code; never echo transport text."""
    if state.connection == "closed" or generation != state.generation:
        return state
    message = DISCONNECT_MESSAGES.get(reason, DISCONNECTED) if type(reason) is str else DISCONNECTED
    return replace(state, connection="disconnected", message=message)


def close(state: LiveState) -> LiveState:
    if state.connection == "closed":
        return state
    return replace(state, generation=state.generation + 1, connection="closed", message=CLOSED)


def _cursor(value: Any) -> dict[str, Any]:
    if (
        type(value) is not dict
        or set(value) != {"epoch", "sequence"}
        or type(value["epoch"]) is not str
        or re.fullmatch(r"[a-f0-9]{32}", value["epoch"]) is None
        or type(value["sequence"]) is not int
        or value["sequence"] < 0
    ):
        raise ValueError
    return {"epoch": value["epoch"], "sequence": value["sequence"]}


def _ids(value: Any) -> list[str]:
    if type(value) is not list or len(value) > MAX_CARDS:
        raise ValueError
    if any(
        type(job) is not str or re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", job) is None for job in value
    ) or len(set(value)) != len(value):
        raise ValueError
    return value


def _retained(
    state: LiveState, timeline: TimelineSnapshot
) -> tuple[TimelineSnapshot, tuple[_Retention, ...]]:
    """Never renew an identity's deadline, including after same-epoch removal."""
    restarted = state.timeline is not None and state.timeline.broker_epoch != timeline.broker_epoch
    retained = {} if restarted else {item.key: item for item in state._retention}
    cards = []
    key: tuple[str, ...]
    for original in timeline.cards:
        card = original
        image = card.image
        if image is not None:
            key = (
                "image",
                image.job_id,
                image.sha256,
                image.source,
                image.recipient_principal,
                image.born_utc,
            )
            previous = retained.get(key)
            expiry = min(image.expires_at, previous.expires_at) if previous else image.expires_at
            retained[key] = _Retention(key, expiry)
            card = replace(card, image=replace(image, expires_at=expiry))
        report = card.device_observation
        if report is not None:
            key = ("report", *report.key)
            digest = hashlib.sha256(
                json.dumps(report.metadata, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            previous = retained.get(key)
            if previous is not None and previous.metadata_hash != digest:
                raise ValueError
            expiry = min(report.expires_at, previous.expires_at) if previous else report.expires_at
            retained[key] = _Retention(key, expiry, digest)
            card = replace(card, device_observation=replace(report, expires_at=expiry))
        cards.append(card)
    if len(retained) > MAX_RETENTION_IDENTITIES:
        raise ValueError
    return replace(timeline, cards=tuple(cards)), tuple(retained.values())


def _delta(
    state: LiveState, payload: dict[str, Any], cursor: dict[str, Any], started_at: float
) -> TimelineSnapshot:
    if (
        state.connection != "live"
        or state.timeline is None
        or state.snapshot is None
        or state.cursor is None
        or _cursor(payload["base_cursor"]) != state.cursor
        or cursor["epoch"] != state.cursor["epoch"]
        or cursor["sequence"] <= state.cursor["sequence"]
        or type(payload["logs_changed"]) is not bool
        or type(payload["has_more"]) is not bool
    ):
        raise ValueError
    # Reuse the bounded timeline parser for all upsert cards, not a weaker delta schema.
    upserts = TimelineSnapshot.parse(
        {
            "ok": True,
            "broker_epoch": cursor["epoch"],
            "cards": payload["upsert"],
            "has_more": payload["has_more"],
        },
        started_at,
    )
    removed, order = _ids(payload["remove"]), _ids(payload["order"])
    if set(removed) & {card.job_id for card in upserts.cards}:
        raise ValueError
    cards = {card.job_id: card for card in state.timeline.cards if card.job_id not in removed}
    cards.update((card.job_id, card) for card in upserts.cards)
    if set(cards) != set(order):
        raise ValueError
    return TimelineSnapshot(cursor["epoch"], tuple(cards[job] for job in order), upserts.has_more)


def apply(state: LiveState, payload: Any, generation: int, started_at: float) -> LiveState:
    """Validate and atomically apply one response from the active subscription.

    Failure keeps the validated projection and local selection, while requiring a
    fresh subscription. ``started_at`` is supplied by the runner's monotonic clock.
    """
    if state.connection not in ("connecting", "live") or generation != state.generation:
        return state
    try:
        if (
            type(payload) is not dict
            or payload.get("ok") is not True
            or type(payload.get("schema_version")) is not int
            or payload["schema_version"] != 1
            or payload.get("kind") not in ("snapshot", "reset", "delta", "heartbeat")
            or isinstance(started_at, bool)
            or not isinstance(started_at, (int, float))
            or not math.isfinite(started_at)
        ):
            raise ValueError
        cursor = _cursor(payload["cursor"])
        kind = payload["kind"]
        if state.connection == "connecting" and kind not in ("snapshot", "reset"):
            raise ValueError
        if kind == "heartbeat":
            if cursor != state.cursor or any(
                key in payload for key in ("state", "timeline", "upsert", "remove", "order")
            ):
                raise ValueError
            return state
        if state.connection == "live" and cursor == state.cursor:
            return state
        if (
            state.cursor is not None
            and cursor["epoch"] == state.cursor["epoch"]
            and cursor["sequence"] < state.cursor["sequence"]
        ):
            raise ValueError
        # Parsers must never share input metadata dictionaries with presentation state.
        data = copy.deepcopy(payload)
        from .gui import QueueSnapshot

        snapshot = state.snapshot
        if kind in ("snapshot", "reset"):
            if type(data["state"]) is not dict or data["state"].get("ok") is not True:
                raise ValueError
            snapshot = QueueSnapshot.parse(data["state"])
            timeline = TimelineSnapshot.parse(data["timeline"], started_at)
            if timeline.broker_epoch != cursor["epoch"]:
                raise ValueError
        else:
            timeline = _delta(state, data, cursor, started_at)
            if "state" in data:
                if type(data["state"]) is not dict or data["state"].get("ok") is not True:
                    raise ValueError
                snapshot = QueueSnapshot.parse(data["state"])
        timeline, retained = _retained(state, timeline)
        restarted = (
            state.timeline is not None and state.timeline.broker_epoch != timeline.broker_epoch
        )
        return replace(
            state,
            connection="live",
            cursor=cursor,
            snapshot=snapshot,
            timeline=timeline,
            selected_job_id="" if restarted else state.selected_job_id,
            message=LIVE,
            _retention=retained,
        )
    except (KeyError, TypeError, ValueError, OverflowError, AttributeError):
        return replace(state, connection="disconnected", message=RESYNC_REQUIRED)
