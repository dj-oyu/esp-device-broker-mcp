"""The one gate that turns an exception into a public error body.

Only fixed, reviewed codes leave the process: BrokerError text must be in the
broker or public vocabulary, SourceError uses its own public form, and any
other ValueError goes through the exact-string allowlist. Anything else is not
a public error and is re-raised by the caller.
"""

from __future__ import annotations

from typing import Any

from .broker import BrokerError
from .public_reasons import public_broker_reason, public_reason
from .source_snapshot import SourceError


def to_public(exc: BaseException) -> dict[str, Any] | None:
    """The public body for a request-path failure, or None if it is not one."""
    if isinstance(exc, BrokerError):
        return {"error": public_broker_reason(exc)}
    if isinstance(exc, SourceError):
        return exc.public()
    if isinstance(exc, ValueError):
        return {"error": public_reason(exc)}
    return None


def refusal(exc: BaseException) -> dict[str, Any]:
    """`{"ok": False, ...}` for a public failure; re-raise anything else."""
    body = to_public(exc)
    if body is None:
        raise exc
    return {"ok": False, **body}
