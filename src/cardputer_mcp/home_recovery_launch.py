"""Exact, single-use local CLI window for the dedicated recovery approval only."""

import math
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

from pydantic import TypeAdapter

from .broker import digest_plan
from .build_process import BuildProcess
from .home_recovery import ATTESTATION
from .models import Digest, Identifier
from .operator_launch import CaptureLaunchScope, OperatorLaunchSlot


class HomeRecoveryLaunchScope(CaptureLaunchScope):
    source_job_id: Identifier
    device_fingerprint: Digest
    expected_firmware_sha256: Digest


class HomeRecoveryLaunchRequest(HomeRecoveryLaunchScope):
    action: Literal["home-recovery-review", "home-recovery-approve"]
    review_revision: Digest | None = None
    attestation: str | None = None


class HomeRecoveryLaunchSlot(OperatorLaunchSlot):
    def __init__(
        self,
        executable: Path,
        executable_sha256: str,
        cwd: Path,
        url: str,
        local_token: str,
        owner_sid: str,
        *,
        scope: HomeRecoveryLaunchScope,
        lifetime_seconds: float = 30,
        clock: Callable[[], float] = time.monotonic,
        process: BuildProcess | None = None,
    ) -> None:
        self.recovery_scope = HomeRecoveryLaunchScope.model_validate(scope.model_dump())
        basic = CaptureLaunchScope.model_validate(
            {k: getattr(scope, k) for k in CaptureLaunchScope.model_fields}
        )
        # Scope is positional internally so the caller cannot supply/replace it twice.
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
        self._in_request = False

    def _command(self, action: str, *args: str) -> dict[str, Any]:
        return self._run_command(
            [str(self.executable), "--url", self.url, "--auth-source", "environment", action, *args]
        )

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
            approving = False
            try:
                r = HomeRecoveryLaunchRequest.model_validate(payload)
                if any(getattr(r, k) != v for k, v in self.recovery_scope.model_dump().items()):
                    raise ValueError("scope")
                if r.action == "home-recovery-review":
                    if (
                        self._revision is not None
                        or r.attestation is not None
                        or r.review_revision is not None
                    ):
                        raise ValueError("review")
                    started = self._clock()
                    d = self._command(r.action, "--plan", r.plan_hash)
                    p = d["plan"]
                    if (
                        d.get("ok") is not True
                        or p.get("operation") != "home_recovery"
                        or digest_plan(p) != r.plan_hash
                        or d["plan_hash"] != r.plan_hash
                        or any(
                            p.get(k) != v
                            for k, v in self.recovery_scope.model_dump().items()
                            if k != "plan_hash"
                        )
                        or d["attestation"] != ATTESTATION
                        or p.get("command") != "one_q"
                        or p.get("max_seconds") != 15
                        or p.get("max_receive_bytes") != 16384
                        or p.get("source_plan", {}).get("test") != "derby_smoke"
                        or p.get("source_plan", {}).get("backend") != "serial"
                    ):
                        raise ValueError("scope")
                    remaining = d["remaining_seconds"]
                    if (
                        type(remaining) not in (int, float)
                        or not math.isfinite(remaining)
                        or not 0 < remaining <= 30
                    ):
                        raise ValueError("expired")
                    self._deadline = min(self._deadline, started + remaining)
                    self._monitor()
                    if self._used:
                        raise ValueError("consumed")
                    self._revision = TypeAdapter(Digest).validate_python(d["review_revision"])
                    return {
                        "ok": True,
                        "status": "reviewed",
                        "review": {
                            k: d[k]
                            for k in (
                                "plan",
                                "plan_hash",
                                "review_revision",
                                "attestation",
                                "remaining_seconds",
                            )
                        },
                    }
                if (
                    self._revision is None
                    or r.review_revision != self._revision
                    or r.attestation != ATTESTATION
                ):
                    raise ValueError("review")
                self._used = approving = True
                d = self._command(
                    r.action,
                    "--plan",
                    r.plan_hash,
                    "--review-revision",
                    self._revision,
                    "--attestation",
                    ATTESTATION,
                )
                if (
                    d.get("ok") is not True
                    or d.get("approved") is not True
                    or d.get("job_id") != r.job_id
                    or d.get("state") != "running"
                ):
                    raise ValueError("approval")
                return {
                    "ok": True,
                    "status": "approval_requested",
                    **self.recovery_scope.model_dump(),
                }
            except Exception:
                self._used = True
                return {
                    "ok": False,
                    "error": "operator_launch_approval_outcome_unknown"
                    if approving
                    else "operator_launch_review_refused",
                }
            finally:
                self._in_request = False
                if self._used:
                    self._local_token = None
