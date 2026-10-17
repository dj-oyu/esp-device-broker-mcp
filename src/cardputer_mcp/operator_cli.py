"""Explicit live local operator actions; no database writes or GUI dependencies."""

import secrets
from typing import Any

from pydantic import TypeAdapter

from .models import Digest
from .operator_client import OperatorClient, OperatorClientError


def review_plan(client: OperatorClient, plan_hash: str) -> dict[str, Any]:
    TypeAdapter(Digest).validate_python(plan_hash)
    detail = client.plan(plan_hash)
    return {
        **detail,
        "approval_effect": "Queues this exact submitted job once."
        if detail["execution_submitted"]
        else "Records approval only; does not submit or start work.",
        "capture_notice": (
            "Capture requires HOME and the verified configured firmware. It sends Esc then s; "
            "Esc on another screen can exit an app/editor and lose unsaved state. Serial "
            "open/close may reset the device. Pixels do not prove LCD transfer or coherence. "
            "Image delivery stays private unless separately approved or explicitly opted in. "
            "--allow-image-delivery permits only this plan/job's one resulting image to its "
            "named requesting principal and AI service, for the existing five-minute retention; "
            "it creates no policy and does not extend any deadline."
            if detail["plan"]["test"] == "capture_home"
            else (
                "DERBY requires HOME and the exact configured normal firmware. Fixed "
                "Esc/a/u x16/d x12/e navigation starts local.derby; automatic demo observation "
                "is bounded to 120 seconds, whole job to 180 seconds. At most one s captures "
                "only a currently observed photo; no timed fallback. Reserve 18 seconds for "
                "capture, 12 for exit and 5 margin. Without a current photo, exit unverified "
                "without an image. One q attempts HOME. Cancellation or lease loss stops "
                "new writes including q and may leave the game running. Serial open/close "
                "can reset the device; exit may save state. No arbitrary keys or replay. "
                "Pixels do not prove LCD transfer or frame coherence. Image delivery requires "
                "a separate image-review / image-decision for the exact SHA and revision."
                if detail["plan"]["test"] == "derby_smoke"
                else None
            )
        ),
    }


def approve_live(
    client: OperatorClient,
    plan_hash: str,
    revision: str,
    attestation: str,
    *,
    start_submitted: bool,
    allow_image_delivery: bool = False,
) -> dict[str, Any]:
    TypeAdapter(Digest).validate_python(plan_hash)
    TypeAdapter(Digest).validate_python(revision)
    detail = client.plan(plan_hash)
    if not secrets.compare_digest(revision, detail["review_revision"]):
        raise OperatorClientError("Stale review. Review the plan again; nothing was approved.")
    if detail["execution_submitted"] is not start_submitted:
        raise OperatorClientError("Approval action does not match the reviewed submission state.")
    if allow_image_delivery and (
        detail["plan"]["test"] != "capture_home" or not detail.get("image_delivery_recipient")
    ):
        raise OperatorClientError("Image delivery consent requires an owned capture plan.")
    return client.approve(
        plan_hash, revision, attestation, allow_image_delivery=allow_image_delivery
    )
