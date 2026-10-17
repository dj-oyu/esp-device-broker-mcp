"""Explicitly armed DERBY-only execution or image decision, never generic input."""

import base64
import hashlib
import math
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

from pydantic import TypeAdapter

from .broker import digest_plan
from .build_process import BuildProcess
from .models import Digest
from .operator_launch import HOME_ATTESTATION, CaptureLaunchScope, OperatorLaunchSlot
from .plan_approval import PlanApprovals


class DerbyLaunchScope(CaptureLaunchScope):
    device_fingerprint: Digest
    expected_firmware_sha256: Digest


class DerbyLaunchRequest(DerbyLaunchScope):
    action: Literal[
        "derby-plan-review", "derby-approve-and-start", "derby-image-review", "derby-image-decision"
    ]
    review_revision: Digest | None = None
    attestation: Literal["DEVICE HOME AND FIRMWARE VERIFIED"] | None = None
    sha256: Digest | None = None
    delivery_revision: Digest | None = None
    decision: Literal["approve", "reject"] | None = None


class DerbyOperatorLaunchSlot(OperatorLaunchSlot):
    """Two separate trusted armings: execution now, image after successful completion.

    No first-request target selection. Each window retains the shared same-user,
    executable, credential, finite clock and single-use child ownership boundary.
    """

    def __init__(
        self,
        executable: Path,
        executable_sha256: str,
        cwd: Path,
        url: str,
        local_token: str,
        owner_sid: str,
        *,
        scope: DerbyLaunchScope,
        phase: Literal["execution", "image"],
        lifetime_seconds: float = 30,
        clock: Callable[[], float] = time.monotonic,
        process: BuildProcess | None = None,
    ) -> None:
        self.derby_scope = DerbyLaunchScope.model_validate(scope.model_dump())
        if phase not in ("execution", "image"):
            raise ValueError("derby_operator_invalid_scope")
        self.phase = phase
        self._image: tuple[str, str] | None = None
        self._in_request = False
        basic = CaptureLaunchScope.model_validate(
            {key: getattr(scope, key) for key in CaptureLaunchScope.model_fields}
        )
        super().__init__(
            executable,
            executable_sha256,
            cwd,
            url,
            local_token,
            owner_sid,
            basic,
            lifetime_seconds=lifetime_seconds,
            clock=clock,
            process=process,
        )

    def _command(self, action: str, *args: str) -> dict[str, Any]:
        return self._run_command(
            [
                str(self.executable),
                "--url",
                self.url,
                "--auth-source",
                "environment",
                action,
                *args,
            ],
            response_limit=256 * 1024 if action == "image-review" else 65536,
        )

    def _shorten(self, started: float, *remaining: Any) -> None:
        if self._used:
            raise ValueError("consumed")
        if any(
            type(value) not in (float, int) or not math.isfinite(value) or not 0 < value <= 300
            for value in remaining
        ):
            raise ValueError("expired")
        self._deadline = min(self._deadline, started + min(remaining))
        self._monitor()

    def handle(self, payload: dict[str, Any], *, peer_sid: str) -> dict[str, Any]:
        with self._lock:
            if peer_sid != self.owner_sid:
                return {"ok": False, "error": "operator_launch_same_user_required"}
            if self.closed:
                self._local_token = None
                return {"ok": False, "error": "operator_launch_expired_or_consumed"}
            if self._in_request:
                self._used = True
                self._local_token = None
                return {"ok": False, "error": "operator_launch_review_refused"}
            self._in_request = True
            deciding = False
            try:
                r = DerbyLaunchRequest.model_validate(payload)
                if any(getattr(r, k) != v for k, v in self.derby_scope.model_dump().items()):
                    raise ValueError("scope")
                started = self._clock()
                if r.action == "derby-plan-review" and self.phase == "execution":
                    if self._revision is not None or any(
                        getattr(r, k) is not None
                        for k in (
                            "review_revision",
                            "attestation",
                            "sha256",
                            "delivery_revision",
                            "decision",
                        )
                    ):
                        raise ValueError("review")
                    detail = self._command("plan-review", "--plan", r.plan_hash)
                    plan = detail["plan"]
                    PlanApprovals._schema(plan)
                    if (
                        detail.get("ok") is not True
                        or detail.get("execution_submitted") is not True
                        or plan["test"] != "derby_smoke"
                        or plan["backend"] != "serial"
                        or digest_plan(plan) != r.plan_hash
                        or detail["plan_hash"] != r.plan_hash
                        or any(
                            plan[k] != getattr(r, k)
                            for k in (
                                "job_id",
                                "broker_epoch",
                                "device_fingerprint",
                                "expected_firmware_sha256",
                            )
                        )
                        or detail.get("image_delivery_recipient") != r.principal_id
                        or detail.get("attestation") != "device_home_firmware"
                        or detail.get("attestation_text") != HOME_ATTESTATION
                    ):
                        raise ValueError("scope")
                    self._shorten(
                        started,
                        detail["lease_remaining_seconds"],
                        detail["intent_remaining_seconds"],
                    )
                    self._revision = TypeAdapter(Digest).validate_python(detail["review_revision"])
                    keys = (
                        "plan",
                        "plan_hash",
                        "review_revision",
                        "attestation",
                        "attestation_text",
                        "execution_submitted",
                        "image_delivery_recipient",
                        "lease_remaining_seconds",
                        "intent_remaining_seconds",
                        "capture_notice",
                    )
                    return {
                        "ok": True,
                        "status": "reviewed",
                        "review": {k: detail[k] for k in keys if k in detail},
                    }
                if r.action == "derby-approve-and-start" and self.phase == "execution":
                    if (
                        self._revision is None
                        or r.review_revision != self._revision
                        or r.attestation != HOME_ATTESTATION
                        or any(
                            getattr(r, k) is not None
                            for k in ("sha256", "delivery_revision", "decision")
                        )
                    ):
                        raise ValueError("review")
                    self._used = deciding = True
                    result = self._command(
                        "approve-and-start",
                        "--plan",
                        r.plan_hash,
                        "--review-revision",
                        self._revision,
                        "--attestation",
                        HOME_ATTESTATION,
                    )
                    if (
                        result.get("ok") is not True
                        or result.get("approved") is not True
                        or result.get("execution_submitted") is not True
                        or result.get("job_state") != "queued"
                        or result.get("plan_hash") != r.plan_hash
                        or result.get("image_delivery_opt_in", False) is not False
                    ):
                        raise ValueError("approval")
                    return {
                        "ok": True,
                        "status": "approval_requested",
                        **self.derby_scope.model_dump(),
                    }
                if r.action == "derby-image-review" and self.phase == "image":
                    if self._image is not None or any(
                        getattr(r, k) is not None
                        for k in (
                            "review_revision",
                            "attestation",
                            "sha256",
                            "delivery_revision",
                            "decision",
                        )
                    ):
                        raise ValueError("review")
                    detail = self._command("image-review", "--job", r.job_id)
                    if (
                        detail.get("ok") is not True
                        or detail.get("derby_scope") != self.derby_scope.model_dump()
                        or detail.get("recipient_principal") != r.principal_id
                        or detail.get("job_id") != r.job_id
                        or detail.get("delivery_state") != "pending"
                    ):
                        raise ValueError("scope")
                    sha = TypeAdapter(Digest).validate_python(detail["sha256"])
                    revision = TypeAdapter(Digest).validate_python(detail["delivery_revision"])
                    png = base64.b64decode(detail["image_base64"], validate=True)
                    if hashlib.sha256(png).hexdigest() != sha:
                        raise ValueError("image")
                    self._shorten(started, detail["remaining_seconds"])
                    self._image = (sha, revision)
                    # Pixels stay local until the exact decision authorizes cloud delivery.
                    return {
                        "ok": True,
                        "status": "image_reviewed",
                        "sha256": sha,
                        "delivery_revision": revision,
                        **self.derby_scope.model_dump(),
                    }
                if r.action == "derby-image-decision" and self.phase == "image":
                    if (
                        self._image is None
                        or (r.sha256, r.delivery_revision) != self._image
                        or r.decision is None
                        or r.review_revision is not None
                        or r.attestation is not None
                    ):
                        raise ValueError("image")
                    self._used = deciding = True
                    sha, revision = self._image
                    result = self._command(
                        "image-decision",
                        "--job",
                        r.job_id,
                        "--sha256",
                        sha,
                        "--delivery-revision",
                        revision,
                        "--decision",
                        r.decision,
                    )
                    state = "approved" if r.decision == "approve" else "rejected"
                    if result != {"ok": True, "job_id": r.job_id, "delivery_state": state}:
                        raise ValueError("decision")
                    return result
                raise ValueError("action")
            except Exception:
                self._used = True
                return {
                    "ok": False,
                    "error": "operator_launch_approval_outcome_unknown"
                    if deciding
                    else "operator_launch_review_refused",
                }
            finally:
                self._in_request = False
                if self._used:
                    self._local_token = None
