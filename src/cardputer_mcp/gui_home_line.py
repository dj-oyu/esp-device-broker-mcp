"""Strict, ephemeral local HOME diagnostic display values. Never unescape text."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any

HOME_LINE_FAILED = "Local HOME diagnostic unavailable. No diagnostic line was displayed."
HOME_LINE_EXPIRED = "Local HOME diagnostic expired; the local text has been cleared."
HOME_LINE_OPT_IN = (
    "Keep one HOME diagnostic line locally for this job "
    "(up to 1024 bytes / 120 seconds; never sent to AI)"
)
HOME_LINE_STATES = frozenset(
    (
        "armed",
        "capturing",
        "available",
        "no_matching_line",
        "line_too_long",
        "line_rejected",
        "expired",
        "cleared",
    )
)
_FIELDS = frozenset(
    (
        "job_id",
        "plan_hash",
        "broker_epoch",
        "principal_id",
        "diagnostic_id",
        "state",
        "remaining_seconds",
        "wire_bytes",
        "limit_bytes",
        "shape",
    )
)
_COUNTS = {
    "standard_prefix_count": 4,
    "body_bytes": 65536,
    "home_token_count": 8,
    "token_offset": 65536,
    "before_bytes": 65536,
    "after_bytes": 65536,
    "before_spaces": 65536,
    "before_tabs": 65536,
    "after_spaces": 65536,
    "after_tabs": 65536,
}
_BOOLS = frozenset(
    ("prefix_count_capped", "token_count_capped", "before_horizontal_only", "after_horizontal_only")
)
HomeLineKey = tuple[str, str, str, str, str]


def _identifier(value: Any, pattern: str) -> str:
    if not isinstance(value, str) or re.fullmatch(pattern, value) is None:
        raise ValueError(HOME_LINE_FAILED)
    return value


def _count(value: Any, upper: int) -> int:
    if type(value) is not int or not 0 <= value <= upper:
        raise ValueError(HOME_LINE_FAILED)
    return value


@dataclass(frozen=True)
class HomeLineRow:
    job_id: str
    plan_hash: str
    broker_epoch: str
    principal_id: str
    diagnostic_id: str
    state: str
    expires_at: float
    wire_bytes: int
    limit_bytes: int
    shape: tuple[tuple[str, int | bool], ...] | None

    @property
    def key(self) -> HomeLineKey:
        return (
            self.broker_epoch,
            self.job_id,
            self.plan_hash,
            self.principal_id,
            self.diagnostic_id,
        )

    @classmethod
    def parse(cls, data: Any, started_at: float) -> HomeLineRow:
        try:
            if not isinstance(data, dict) or data.keys() != _FIELDS:
                raise ValueError(HOME_LINE_FAILED)
            state, remaining = data["state"], data["remaining_seconds"]
            if (
                state not in HOME_LINE_STATES
                or type(remaining) not in (int, float)
                or not math.isfinite(remaining)
                or not 0 <= remaining <= 300
                or type(data["limit_bytes"]) is not int
                or data["limit_bytes"] != 1024
            ):
                raise ValueError(HOME_LINE_FAILED)
            shape = data["shape"]
            parsed_shape = None
            if shape is not None:
                if not isinstance(shape, dict) or shape.keys() != _COUNTS.keys() | _BOOLS:
                    raise ValueError(HOME_LINE_FAILED)
                for key, upper in _COUNTS.items():
                    _count(shape[key], upper)
                if any(type(shape[key]) is not bool for key in _BOOLS):
                    raise ValueError(HOME_LINE_FAILED)
                parsed_shape = tuple(sorted(shape.items()))
            return cls(
                _identifier(data["job_id"], r"[a-zA-Z0-9_-]{1,64}"),
                _identifier(data["plan_hash"], r"[a-f0-9]{64}"),
                _identifier(data["broker_epoch"], r"[a-f0-9]{32}"),
                _identifier(data["principal_id"], r"[a-zA-Z0-9_.:@-]{1,128}"),
                _identifier(data["diagnostic_id"], r"[a-f0-9]{32}"),
                state,
                started_at + min(float(remaining), 120.0 if state == "available" else 300.0),
                _count(data["wire_bytes"], 65536),
                1024,
                parsed_shape,
            )
        except (KeyError, TypeError, ValueError, OverflowError):
            raise ValueError(HOME_LINE_FAILED) from None


@dataclass(frozen=True)
class HomeLinePreview:
    row: HomeLineRow
    escaped_line: str | None = field(default=None, repr=False)

    @classmethod
    def parse(cls, data: Any, expected: HomeLineRow, started_at: float) -> HomeLinePreview:
        try:
            if (
                not isinstance(data, dict)
                or data.get("ok") is not True
                or not _FIELDS <= data.keys()
                or not data.keys() <= _FIELDS | {"ok", "escaped_line"}
            ):
                raise ValueError(HOME_LINE_FAILED)
            row = HomeLineRow.parse({key: data[key] for key in _FIELDS}, started_at)
            if row.key != expected.key:
                raise ValueError(HOME_LINE_FAILED)
            line = data.get("escaped_line")
            if row.state == "available":
                if (
                    not isinstance(line, str)
                    or not 1 <= len(line) <= 4096
                    or any(not 32 <= ord(char) <= 126 for char in line)
                    or row.wire_bytes > row.limit_bytes
                ):
                    raise ValueError(HOME_LINE_FAILED)
            elif "escaped_line" in data:
                raise ValueError(HOME_LINE_FAILED)
            return cls(row, line)
        except (KeyError, TypeError, ValueError, OverflowError):
            raise ValueError(HOME_LINE_FAILED) from None
