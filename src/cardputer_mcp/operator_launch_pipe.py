"""Retired product approval IPC; never starts a process or opens a pipe."""
from typing import Any


def request_operator_launch(
    name: str, payload: dict[str, Any], *, timeout: float = 20
) -> dict[str, Any]:
    """Old launch callers get an explicit terminal response without side effects."""
    return {"ok": False, "error": "product_approval_retired"}
