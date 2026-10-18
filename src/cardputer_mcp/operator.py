"""Offline ownership recovery; explicit acknowledgement never retries device work."""

from collections.abc import Callable
from pathlib import Path
from typing import Any

from filelock import FileLock, Timeout
from pydantic import TypeAdapter

from .models import Identifier
from .store import Store


def approve_plan(
    state_dir: Path, digest: str, confirm: Callable[[str], str] = input
) -> dict[str, Any]:
    raise ValueError("approval_interface_retired")


def acknowledge_recovery(
    state_dir: Path, job_id: str, confirm: Callable[[str], str] = input
) -> dict[str, Any]:
    TypeAdapter(Identifier).validate_python(job_id)
    if not (state_dir / "history.sqlite3").is_file():
        raise ValueError("broker_state_not_found")
    lock = FileLock(str(state_dir / "broker.lockfile"), timeout=0)
    try:
        lock.acquire()
    except Timeout as exc:
        raise ValueError("stop_broker_before_recovery_review") from exc
    try:
        store = Store(state_dir / "history.sqlite3")
        try:
            job = store.job(job_id)
            if not job or job["state"] != "needs_review":
                raise ValueError("job_does_not_need_review")
            store.db.execute("UPDATE jobs SET reviewed=1 WHERE id=?", (job_id,))
            store.db.commit()
            return {"reviewed": True, "job_id": job_id, "replayed": False}
        finally:
            store.close()
    finally:
        lock.release()


def acknowledge_device_request_recovery(
    state_dir: Path, request_id: str, confirm: Callable[[str], str] = input
) -> dict[str, Any]:
    """Offline explicit review of restart-invalidated physical ownership/waiting."""
    TypeAdapter(Identifier).validate_python(request_id)
    if not (state_dir / "history.sqlite3").is_file():
        raise ValueError("broker_state_not_found")
    lock = FileLock(str(state_dir / "broker.lockfile"), timeout=0)
    try:
        lock.acquire()
    except Timeout as exc:
        raise ValueError("stop_broker_before_recovery_review") from exc
    try:
        store = Store(state_dir / "history.sqlite3")
        try:
            request = store.request(request_id)
            if not request or request["state"] != "needs_review" or request["reviewed"]:
                raise ValueError("device_request_does_not_need_review")
            store.review_request(request_id)
            return {"reviewed": True, "request_id": request_id, "replayed": False}
        finally:
            store.close()
    finally:
        lock.release()
