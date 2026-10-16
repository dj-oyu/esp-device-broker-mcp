"""One explicitly enabled capture window; never a general operator relay."""

import hashlib
import json
import math
import os
import re
import stat
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from .broker import digest_plan
from .build_process import BuildProcess
from .firmware_build import OS_ENV
from .models import Digest, Identifier
from .operator_client import operator_base_url
from .plan_approval import PlanApprovals

MAX_REQUEST = 2048
MAX_RESPONSE = 65536
HOME_ATTESTATION = "DEVICE HOME AND FIRMWARE VERIFIED"


class OperatorLaunchError(ValueError):
    """Fixed diagnostics only; native output and credentials are never reflected."""


class CaptureLaunchScope(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    broker_epoch: str = Field(pattern=r"^[a-f0-9]{32}$")
    job_id: Identifier
    plan_hash: Digest
    principal_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:@-]+$")


class LaunchRequest(CaptureLaunchScope):
    action: Literal["plan-review", "approve-and-start"]
    review_revision: Digest | None = None
    attestation: Literal["", "DEVICE HOME AND FIRMWARE VERIFIED"] | None = None
    allow_image_delivery: bool = False


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_key")
        result[key] = value
    return result


def decode_message(data: bytes, limit: int) -> dict[str, Any]:
    if not data or len(data) > limit:
        raise OperatorLaunchError("operator_launch_invalid_message")
    try:
        result = json.loads(data.decode("utf-8"), object_pairs_hook=_unique_object)
        if type(result) is not dict:
            raise ValueError
        return result
    except Exception:
        raise OperatorLaunchError("operator_launch_invalid_message") from None


def _plain_path(path: Path) -> Path:
    if not path.is_absolute() or ".." in path.parts:
        raise OperatorLaunchError("operator_launch_invalid_path")
    for part in (path, *path.parents):
        info = part.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise OperatorLaunchError("operator_launch_invalid_path")
    return path


class OperatorLaunchSlot:
    """Trusted launcher enables one capture window tied to its current epoch.

    The owner SID is verified again by the native pipe. Same-account processes
    are not mutually isolated: their access is bounded by the short window,
    one review and one approval attempt, not by the secrecy of a pipe name.
    A supplied exact scope is fixed in advance; otherwise the first same-user
    review selects it once. IPC cannot extend the window, re-arm or retarget it.
    """

    def __init__(
        self,
        executable: Path,
        executable_sha256: str,
        cwd: Path,
        url: str,
        local_token: str,
        owner_sid: str,
        scope: CaptureLaunchScope | None,
        *,
        broker_epoch: str | None = None,
        allow_image_delivery: bool = False,
        lifetime_seconds: float = 30,
        clock: Callable[[], float] = time.monotonic,
        process: BuildProcess | None = None,
    ) -> None:
        self._lock = threading.RLock()
        self._clock = clock
        now = clock()
        if (
            not math.isfinite(now)
            or not math.isfinite(lifetime_seconds)
            or not 0 < lifetime_seconds <= 60
            or type(allow_image_delivery) is not bool
            or re.fullmatch(r"[a-f0-9]{64}", local_token) is None
            or re.fullmatch(r"S-\d+(?:-\d+)+", owner_sid) is None
        ):
            raise OperatorLaunchError("operator_launch_invalid_scope")
        self.scope = CaptureLaunchScope.model_validate(scope.model_dump()) if scope else None
        epoch = self.scope.broker_epoch if self.scope else broker_epoch
        if type(epoch) is not str or re.fullmatch(r"[a-f0-9]{32}", epoch) is None:
            raise OperatorLaunchError("operator_launch_invalid_scope")
        self._epoch = epoch
        self.owner_sid = owner_sid
        self.executable = _plain_path(executable)
        self.cwd = _plain_path(cwd)
        if self.executable.suffix.lower() != ".exe" or not self.cwd.is_dir():
            raise OperatorLaunchError("operator_launch_invalid_path")
        self._exe_hash = TypeAdapter(Digest).validate_python(executable_sha256)
        self.url = operator_base_url(url) + "/mcp"
        self._cwd_identity = (self.cwd.stat().st_dev, self.cwd.stat().st_ino)
        self._local_token: str | None = local_token
        self._deadline = now + lifetime_seconds
        self._allow_image_delivery = allow_image_delivery
        self._revision: str | None = None
        self._attestation: str | None = None
        self._used = False
        self.process = process or BuildProcess()
        self._verify_paths()

    def __repr__(self) -> str:
        return "<OperatorLaunchSlot scoped; credentials redacted>"

    @property
    def closed(self) -> bool:
        now = self._clock()
        return self._used or not math.isfinite(now) or now >= self._deadline

    def _verify_paths(self) -> None:
        _plain_path(self.executable)
        _plain_path(self.cwd)
        info = self.executable.stat()
        if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= 100 * 1024 * 1024:
            raise OperatorLaunchError("operator_launch_invalid_path")
        if (self.cwd.stat().st_dev, self.cwd.stat().st_ino) != self._cwd_identity:
            raise OperatorLaunchError("operator_launch_invalid_path")
        with self.executable.open("rb") as handle:
            if hashlib.file_digest(handle, "sha256").hexdigest() != self._exe_hash:
                raise OperatorLaunchError("operator_launch_executable_changed")

    def _monitor(self) -> None:
        now = self._clock()
        if not math.isfinite(now) or now >= self._deadline:
            raise OperatorLaunchError("operator_launch_expired")

    def _run(self, action: str, request: LaunchRequest) -> dict[str, Any]:
        assert self.scope is not None
        self._monitor()
        self._verify_paths()
        args = [
            str(self.executable),
            "--url",
            self.url,
            "--auth-source",
            "environment",
            action,
            "--plan",
            self.scope.plan_hash,
        ]
        if action == "approve-and-start":
            assert self._revision is not None and request.attestation is not None
            args += ["--review-revision", self._revision, "--attestation", request.attestation]
            if request.allow_image_delivery:
                args.append("--allow-image-delivery")
        environment = {
            key.upper(): value for key, value in os.environ.items() if key.upper() in OS_ENV
        }
        if self._local_token is None:
            raise OperatorLaunchError("operator_launch_consumed")
        environment["CARDPUTER_LOCAL_TOKEN"] = self._local_token
        lines: list[str] = []
        count = 0

        def collect(line: str, stream: str) -> None:
            nonlocal count
            if stream != "stdout":
                return
            count += len(line.encode("utf-8")) + 1
            if count > MAX_RESPONSE:
                self.process.stop_requested.set()
                return
            lines.append(line)

        try:
            code = self.process.run(
                args,
                environment,
                self.cwd,
                collect,
                timeout=min(10.0, self._deadline - self._clock()),
                monitor=self._monitor,
            )
            if code != 0 or count > MAX_RESPONSE:
                raise OperatorLaunchError("operator_launch_child_failed")
            return decode_message("\n".join(lines).encode("utf-8"), MAX_RESPONSE)
        finally:
            environment.clear()
            lines.clear()

    def _review(self, request: LaunchRequest) -> dict[str, Any]:
        assert self.scope is not None
        if (
            self._revision is not None
            or request.review_revision is not None
            or (request.attestation is not None or request.allow_image_delivery)
        ):
            raise OperatorLaunchError("operator_launch_invalid_message")
        started = self._clock()
        detail = self._run("plan-review", request)
        plan = detail["plan"]
        PlanApprovals._schema(plan)
        required = HOME_ATTESTATION if plan["backend"] == "serial" else ""
        if (
            detail.get("ok") is not True
            or detail.get("execution_submitted") is not True
            or plan["test"] != "capture_home"
            or digest_plan(plan) != self.scope.plan_hash
            or detail["plan_hash"] != self.scope.plan_hash
            or plan["job_id"] != self.scope.job_id
            or plan["broker_epoch"] != self.scope.broker_epoch
            or detail.get("image_delivery_recipient") != self.scope.principal_id
            or detail.get("attestation_text") != required
            or detail.get("attestation")
            != ("device_home_firmware" if plan["backend"] == "serial" else "none")
        ):
            raise OperatorLaunchError("operator_launch_scope_mismatch")
        remaining = [detail["lease_remaining_seconds"], detail["intent_remaining_seconds"]]
        if any(
            type(value) not in (int, float) or not math.isfinite(value) or not 0 < value <= 300
            for value in remaining
        ):
            raise OperatorLaunchError("operator_launch_expired")
        self._deadline = min(self._deadline, started + min(remaining))
        self._monitor()
        self._revision = TypeAdapter(Digest).validate_python(detail["review_revision"])
        self._attestation = required
        # Do not relay arbitrary child/server fields or output to the IPC caller.
        keys = {
            "plan",
            "plan_hash",
            "review_revision",
            "attestation",
            "attestation_text",
            "execution_submitted",
            "image_delivery_recipient",
            "lease_remaining_seconds",
            "intent_remaining_seconds",
        }
        return {"ok": True, "status": "reviewed", "review": {key: detail[key] for key in keys}}

    def handle(self, payload: dict[str, Any], *, peer_sid: str) -> dict[str, Any]:
        with self._lock:
            if peer_sid != self.owner_sid:
                return {"ok": False, "error": "operator_launch_same_user_required"}
            if self.closed:
                self._local_token = None
                return {"ok": False, "error": "operator_launch_expired_or_consumed"}
            approving = False
            try:
                request = LaunchRequest.model_validate(payload)
                if request.broker_epoch != self._epoch:
                    raise OperatorLaunchError("operator_launch_scope_mismatch")
                if self.scope is None:
                    if request.action != "plan-review":
                        raise OperatorLaunchError("operator_launch_scope_mismatch")
                    # First same-user review selects one tuple, once. Its live
                    # capture/recipient still must be proved by the fixed CLI.
                    self.scope = CaptureLaunchScope.model_validate(
                        {key: getattr(request, key) for key in CaptureLaunchScope.model_fields}
                    )
                if {key: getattr(request, key) for key in type(self.scope).model_fields} != (
                    self.scope.model_dump()
                ):
                    raise OperatorLaunchError("operator_launch_scope_mismatch")
                if request.action == "plan-review":
                    return self._review(request)
                if (
                    self._revision is None
                    or request.review_revision != self._revision
                    or request.attestation is None
                    or request.attestation != self._attestation
                    or (request.allow_image_delivery and not self._allow_image_delivery)
                ):
                    raise OperatorLaunchError("operator_launch_scope_mismatch")
                # Consume before starting a child. A lost reply or timeout cannot replay.
                self._used = True
                approving = True
                result = self._run("approve-and-start", request)
                if (
                    result.get("ok") is not True
                    or result.get("approved") is not True
                    or result.get("execution_submitted") is not True
                    or result.get("plan_hash") != self.scope.plan_hash
                    or result.get("job_state") != "queued"
                    or result.get("image_delivery_opt_in", False)
                    is not request.allow_image_delivery
                ):
                    raise OperatorLaunchError("operator_launch_child_failed")
                return {"ok": True, "status": "approval_requested", **self.scope.model_dump()}
            except Exception:
                self._used = True
                return {
                    "ok": False,
                    "error": "operator_launch_approval_outcome_unknown"
                    if approving
                    else "operator_launch_review_refused",
                }
            finally:
                if self._used:
                    self._local_token = None

    def close(self) -> bool:
        """Reap only our CLI tree; retain an uncertain owner for explicit cleanup."""
        with self._lock:
            self._used = True
            self._local_token = None
            self.process.stop_requested.set()
            owner = self.process.owner
            if owner is not None and owner.stop(5):
                owner.close()
                self.process.owner = None
                self.process.active = self.process.uncertain = False
            return not self.process.active and not self.process.uncertain
