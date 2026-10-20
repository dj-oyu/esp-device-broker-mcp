"""Read-subscription recovery policy; never retries an operator action."""

import random
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

RETRYABLE_HTTP = frozenset({408, 429, 500, 502, 503, 504})
RETRYABLE_CODES = frozenset({"unavailable", "operator_timeline_unavailable"})
SAFE_CODES = RETRYABLE_CODES | frozenset(
    {"operator_request_failed", "invalid_request", "authentication_required", "local_operator_required"}
)


def diagnostic(
    reason: str,
    generation: int,
    elapsed_ms: int,
    *,
    status: int | None = None,
    code: str | None = None,
    retryable: bool = False,
) -> dict[str, Any]:
    return {
        "utc": datetime.now(UTC).isoformat(timespec="seconds"),
        "reason": reason,
        "generation": generation,
        "elapsed_ms": max(0, min(elapsed_ms, 86400000)),
        "http_status": status if type(status) is int and 100 <= status <= 599 else None,
        "code": code if type(code) is str and code in SAFE_CODES else None,
        "retryable": retryable is True,
    }


@dataclass
class LiveRetry:
    attempts: int = 0
    invalid_count: int = 0
    deadline: float | None = None
    connected_at: float | None = None
    last_received: float | None = None
    stopped: bool = False
    last_received_utc: str | None = None

    def manual(self) -> None:
        self.attempts = self.invalid_count = 0
        self.deadline = self.connected_at = None
        self.stopped = False

    def received(self, now: float) -> None:
        self.last_received = now
        self.last_received_utc = datetime.now(UTC).isoformat(timespec="seconds")
        if self.connected_at is None:
            self.connected_at = now
        if now - self.connected_at >= 60:
            self.attempts = self.invalid_count = 0
        self.deadline = None

    def failed(self, details: dict[str, Any], now: float, *, jitter: float | None = None) -> None:
        self.connected_at = None
        self.deadline = None
        invalid = details.get("reason") in {"invalid_response", "delivery_gap"}
        if invalid:
            self.invalid_count += 1
        if (not invalid and details.get("retryable") is not True) or self.invalid_count > 1:
            self.stopped = True
            return
        self.stopped = False
        self.attempts = min(self.attempts + 1, 30)
        factor = random.uniform(0.8, 1.2) if jitter is None else max(0.8, min(jitter, 1.2))
        delay = min(30.0, (2 ** min(self.attempts - 1, 5)) * factor)
        self.deadline = now + delay

    def due(self, now: float) -> bool:
        return self.deadline is not None and now >= self.deadline and not self.stopped

    def starting(self) -> None:
        self.deadline = None
