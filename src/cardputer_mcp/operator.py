"""Local OS-user approval boundary. Deliberately not exposed as an MCP tool."""

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from filelock import FileLock, Timeout
from pydantic import TypeAdapter

from .broker import digest_plan
from .models import Digest, Identifier
from .store import Store


def approve_plan(
    state_dir: Path, digest: str, confirm: Callable[[str], str] = input
) -> dict[str, Any]:
    TypeAdapter(Digest).validate_python(digest)
    database = state_dir / "history.sqlite3"
    if not database.is_file():
        raise ValueError("broker_state_not_found")
    store = Store(database)
    try:
        disclosure = ""
        plan = store.plan(digest)
        if plan is None or digest_plan(plan) != digest:
            raise ValueError("plan_not_found_or_changed")
        job = store.job(str(plan.get("job_id", "")))
        if job is not None and job.get("state") == "awaiting_approval":
            raise ValueError("submitted_plan_requires_gui_approval")
        if plan.get("backend") not in ("mock", "serial"):
            raise ValueError("unsupported_backend")
        if plan.get("test") in ("firmware_backup", "firmware_flash", "firmware_restore"):
            from .plan_approval import PlanApprovals

            PlanApprovals._schema(plan)
            disclosure = (
                "This exact plan may reset/read/write the selected device. Verify the "
                "device/artifact/backup. Partial writes are not undone; no automatic restore.\n"
            )
        elif plan.get("test") in ("firmware_build", "firmware_fetch_source"):
            from .plan_approval import PlanApprovals

            PlanApprovals._schema(plan)
            disclosure = (
                "Fetches one registered public commit; no build, flash, or USB. "
                if plan["test"] == "firmware_fetch_source"
                else "Executes trusted local profile and project build scripts as your OS user. "
                "Output is shared with the requesting principal; redaction is best effort. "
                "External ESP-IDF must be exactly 6.0.1. No flash or USB operation.\n"
            )
        elif plan.get("backend") == "serial":
            if plan.get("test") not in (
                "passive_observe",
                "hello",
                "capture_home",
                "derby_smoke",
                "firmware_observe",
                "restart_and_observe",
            ):
                raise ValueError("unsupported_serial_action")
            if plan.get("test") in ("firmware_observe", "restart_and_observe"):
                from .plan_approval import PlanApprovals

                try:
                    PlanApprovals._schema(plan)
                except (ValueError, TypeError, KeyError):
                    raise ValueError("plan_not_found_or_changed") from None
                effects = (
                    "ONE hardware reset of an ESP32-S3 native USB Serial/JTAG device; "
                    "other ESP chips share the USB IDs, so verify the physical device. "
                    "Running app state and unsaved work can be lost. Discard buffered input "
                    "before reset. At most one same-identity reopen within 8 seconds; reopening "
                    "can lose early boot logs. No reset retry, serial commands or flash. "
                    "Issued control signals do not prove the application restarted. "
                    if plan.get("test") == "restart_and_observe"
                    else "No commands, reset command, flash or automatic reconnect. "
                )
                disclosure = (
                    "Receive device console output for up to 30 seconds / 64 KiB. "
                    "This approval permits delivery of the bounded report to "
                    f"{plan['log_delivery']['principal_id']} ({plan['log_delivery']['origin']}). "
                    "Logs may contain private data; known credential patterns are redacted "
                    "but redaction is not guaranteed complete. Only the newest report is "
                    "kept in RAM for up to five minutes. "
                    + effects
                    + "Opening/closing serial may glitch control lines "
                    "or reset the device. Firmware fields are unverified observed startup text; "
                    "an ELF prefix is not the firmware binary SHA256.\n"
                )
            if plan.get("test") in ("hello", "capture_home", "derby_smoke"):
                effects = (
                    "DERBY performs the exact fixed navigation, observation, capture and exit "
                    "listed in this plan; interruption may leave the game running. "
                    if plan.get("test") == "derby_smoke"
                    else "CAPTURE requires the device already be at HOME. "
                    "It sends one Esc to confirm "
                    "HOME, then one s key and receives "
                    "rendered pixels. In another screen Esc may exit an app/editor and lose "
                    "unsaved state. The requesting authenticated client may retrieve the image "
                    "for five minutes and transmit it to its AI service only after separate "
                    "image approval or an explicit operator image-delivery policy. "
                    "Pixels do not prove LCD transfer or frame coherence. "
                    if plan.get("test") == "capture_home"
                    else "HELLO exits the current app and sends q/a/u/e navigation keys. "
                )
                disclosure = (
                    effects
                    + "Device opening/closing may reset it due to driver control-line glitches. "
                    "Verify the configured firmware is running and other serial tools are closed. "
                )
        target = {key: plan[key] for key in ("device", "device_id", "port") if key in plan}
        recovery = {
            key: plan[key]
            for key in ("recovery_kind", "backup_job_id", "lkg_receipt")
            if plan.get(key) is not None
        }
        context = (
            f"Target: {json.dumps(target, sort_keys=True, ensure_ascii=True)}\n"
            f"Recovery: {json.dumps(recovery, sort_keys=True, ensure_ascii=True)}\n"
            "Approve records consent; it does not prove the physical device or running firmware.\n"
        )
        summary = json.dumps(plan, indent=2, sort_keys=True)
        submission_notice = "This approval alone does not submit execution.\n"
        answer = confirm(
            f"{context}{disclosure}{submission_notice}{summary}\nPlan SHA-256: {digest}\n"
            "Type APPROVE to approve this displayed plan: "
        )
        if answer.strip() not in ("APPROVE", f"APPROVE {digest}"):
            raise ValueError("approval_not_granted")
        # The prompt can remain open while a client submits or cancels work.
        # Bind its execution meaning as well as its immutable plan; otherwise
        # a prepared-only consent could start a newly submitted request.
        with store.db:
            store.db.execute("BEGIN IMMEDIATE")
            current = store.plan(digest)
            current_job = store.job(str(plan.get("job_id", "")))
            original_state = (job["state"], job["plan_hash"]) if job is not None else None
            current_state = (
                (current_job["state"], current_job["plan_hash"])
                if current_job is not None
                else None
            )
            if current is None or digest_plan(current) != digest or current_state != original_state:
                raise ValueError("stale_plan_review")
            store.approve(digest)
        return {"approved": True, "plan_hash": digest}
    finally:
        store.close()


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
            answer = confirm(
                f"{json.dumps(job, indent=2)}\nInspect the result first. "
                f"This will NOT retry anything. Type REVIEW {job_id}: "
            )
            if answer.strip() != f"REVIEW {job_id}":
                raise ValueError("review_not_acknowledged")
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
            answer = confirm(
                f"{json.dumps(request, indent=2)}\nInspect the device and prior job first. "
                "This invalidated session cannot resume and nothing will be replayed. "
                f"Type REVIEW REQUEST {request_id}: "
            )
            if answer.strip() != f"REVIEW REQUEST {request_id}":
                raise ValueError("review_not_acknowledged")
            store.review_request(request_id)
            return {"reviewed": True, "request_id": request_id, "replayed": False}
        finally:
            store.close()
    finally:
        lock.release()
