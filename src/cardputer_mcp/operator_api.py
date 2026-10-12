"""Local operator control plane for the resident broker; never MCP tools."""

import json
from collections.abc import Callable
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field
from starlette.types import ASGIApp, Receive, Scope, Send

from .broker import Broker, BrokerError
from .capture_policy import CapturePolicyError
from .models import Digest, Identifier, Origin
from .operator_timeline import OperatorTimeline
from .plan_approval import PlanApprovals

_MAX_BODY = 2048


class OperatorPrincipal(Protocol):
    @property
    def origin(self) -> Origin: ...

    @property
    def scopes(self) -> frozenset[str]: ...


class CancelWait(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    request_id: Identifier


class ReorderWait(CancelWait):
    position: int = Field(ge=1, le=256)
    expected_revision: Digest


class ReleaseIdle(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    lease_id: Identifier


class ReviewPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    plan_hash: Digest


class ApprovePlan(ReviewPlan):
    review_revision: Digest
    attestation: Literal[
        "",
        "DEVICE FREE AND FIRMWARE VERIFIED",
        "DEVICE HOME AND FIRMWARE VERIFIED",
        "REGISTERED DEVICE VERIFIED FOR THIS BACKUP READ",
        "DEVICE AND BACKUP VERIFIED FOR THIS EXACT FLASH PLAN",
    ]
    retain_local_home_line: bool = False


class PreviewImage(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    job_id: Identifier


class PreviewHomeLine(PreviewImage):
    plan_hash: Digest
    diagnostic_id: str = Field(pattern=r"^[a-f0-9]{32}$")


class DecideImage(PreviewImage):
    sha256: Digest
    delivery_revision: Digest
    decision: Literal["approve", "reject"]


class CreateCapturePolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    principal_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_:.+-]+$")
    mode: Literal["session", "persistent"]
    capture_home: bool
    image_delivery: bool
    ttl_seconds: int = Field(ge=1, le=604800)
    expected_target: Digest
    expected_lease_id: Identifier | None = None
    acknowledge_effects: Literal[True]


class UpdateCapturePolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    policy_id: str = Field(pattern=r"^capture_policy_[a-f0-9]{32}$")
    expected_revision: int = Field(ge=1, le=2147483647)
    capture_home: bool
    image_delivery: bool
    ttl_seconds: int = Field(ge=1, le=604800)
    expected_target: Digest
    acknowledge_effects: Literal[True]


class RevokeCapturePolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    policy_id: str = Field(pattern=r"^capture_policy_[a-f0-9]{32}$")
    expected_revision: int = Field(ge=1, le=2147483647)


class ArmCapture(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    lease_id: Identifier
    expected_target: Digest
    ttl_seconds: int = Field(ge=1, le=604800)
    attestation: Literal["DEVICE HOME AND FIRMWARE VERIFIED"]
    acknowledge_effects: Literal[True]


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_json_key")
        result[key] = value
    return result


class OperatorAPI:
    """Shares the MCP app's lifespan and broker, inside its loopback guard."""

    def __init__(
        self,
        app: ASGIApp,
        broker: Broker,
        authenticate: Callable[[str | None], OperatorPrincipal | None],
        *,
        diagnostics: Callable[[], dict[str, Any]] | None = None,
    ) -> None:
        self.app = app
        self.broker = broker
        self.authenticate = authenticate
        self.diagnostics = diagnostics
        self.plans = PlanApprovals(broker)
        self.timeline = OperatorTimeline(broker, self.plans)

    @staticmethod
    async def _respond(send: Send, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"cache-control", b"no-store"),
                    (b"content-length", str(len(body)).encode("ascii")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "")
        if scope["type"] != "http" or not (path == "/operator" or path.startswith("/operator/")):
            await self.app(scope, receive, send)
            return
        headers = {key.lower(): value for key, value in scope["headers"]}
        try:
            raw_header = headers.get(b"authorization")
            header = raw_header.decode("ascii") if raw_header is not None else None
        except UnicodeError:
            header = None
        principal = self.authenticate(header)
        if principal is None:
            await self._respond(send, 401, {"ok": False, "error": "authentication_required"})
            return
        if principal.origin != Origin.LOCAL or "control" not in principal.scopes:
            await self._respond(send, 403, {"ok": False, "error": "local_operator_required"})
            return
        if scope.get("query_string"):
            await self._respond(send, 400, {"ok": False, "error": "invalid_request"})
            return
        routes = {
            "/operator/state": "GET",
            "/operator/timeline": "GET",
            "/operator/cancel": "POST",
            "/operator/reorder": "POST",
            "/operator/release": "POST",
            "/operator/plans": "GET",
            "/operator/plan": "POST",
            "/operator/approve-plan": "POST",
            "/operator/local-home-line": "POST",
            "/operator/images": "GET",
            "/operator/image": "POST",
            "/operator/image-decision": "POST",
            "/operator/policy-options": "GET",
            "/operator/policies": "GET",
            "/operator/policy-create": "POST",
            "/operator/policy-update": "POST",
            "/operator/policy-revoke": "POST",
            "/operator/capture-arm": "POST",
        }
        if self.diagnostics is not None:
            routes["/operator/http-diagnostics"] = "GET"
        if path not in routes:
            await self._respond(send, 404, {"ok": False, "error": "not_found"})
            return
        if scope["method"] != routes[path]:
            await self._respond(send, 405, {"ok": False, "error": "method_not_allowed"})
            return
        payload: Any = None
        if scope["method"] == "POST":
            if headers.get(b"content-type", b"").split(b";", 1)[0] != b"application/json":
                await self._respond(send, 400, {"ok": False, "error": "invalid_request"})
                return
            body = bytearray()
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return
                chunk = message.get("body", b"")
                if len(body) + len(chunk) > _MAX_BODY:
                    await self._respond(send, 413, {"ok": False, "error": "request_too_large"})
                    return
                body.extend(chunk)
                if not message.get("more_body", False):
                    break
            try:
                payload = json.loads(body, object_pairs_hook=_unique_object)
            except (ValueError, UnicodeError, RecursionError):
                await self._respond(send, 400, {"ok": False, "error": "invalid_request"})
                return
        if scope["method"] == "POST":
            # Receiving even a bounded body awaits the network. Recheck the
            # current operator grant after that gap, immediately before mutation.
            principal = self.authenticate(header)
            if principal is None:
                await self._respond(send, 401, {"ok": False, "error": "authentication_required"})
                return
            if principal.origin != Origin.LOCAL or "control" not in principal.scopes:
                await self._respond(send, 403, {"ok": False, "error": "local_operator_required"})
                return
        try:
            if path == "/operator/http-diagnostics" and self.diagnostics is not None:
                result = self.diagnostics()
            elif path in (
                "/operator/policy-options",
                "/operator/policies",
                "/operator/policy-create",
                "/operator/policy-update",
                "/operator/policy-revoke",
                "/operator/capture-arm",
            ):
                policies = self.broker.capture_policies
                if policies is None:
                    raise BrokerError("capture_policies_unavailable")
                if path == "/operator/policy-options":
                    result = policies.options()
                elif path == "/operator/policies":
                    result = policies.list()
                elif path == "/operator/policy-create":
                    create = CreateCapturePolicy.model_validate(payload)
                    result = {
                        "policy": policies.create(
                            **create.model_dump(exclude={"acknowledge_effects"})
                        )
                    }
                elif path == "/operator/policy-update":
                    update = UpdateCapturePolicy.model_validate(payload)
                    result = {
                        "policy": policies.update(
                            **update.model_dump(exclude={"acknowledge_effects"})
                        )
                    }
                elif path == "/operator/policy-revoke":
                    revoke = RevokeCapturePolicy.model_validate(payload)
                    result = {"policy": policies.revoke(**revoke.model_dump())}
                else:
                    arm = ArmCapture.model_validate(payload)
                    result = {"arming": policies.arm(**arm.model_dump())}
            elif path == "/operator/timeline":
                result = self.timeline.snapshot()
            elif path == "/operator/plans":
                result = self.plans.list_pending()
            elif path == "/operator/plan":
                review = ReviewPlan.model_validate(payload)
                result = self.plans.detail(review.plan_hash)
            elif path == "/operator/approve-plan":
                approval = ApprovePlan.model_validate(payload)
                result = self.plans.approve(
                    approval.plan_hash,
                    approval.review_revision,
                    approval.attestation,
                    approval.retain_local_home_line,
                )
            elif path == "/operator/local-home-line":
                home_line = PreviewHomeLine.model_validate(payload)
                result = self.broker.operator_home_line(
                    home_line.job_id, home_line.plan_hash, home_line.diagnostic_id
                )
            elif path == "/operator/images":
                result = self.broker.operator_images()
            elif path == "/operator/image":
                preview = PreviewImage.model_validate(payload)
                result = self.broker.operator_image(preview.job_id)
            elif path == "/operator/image-decision":
                decision = DecideImage.model_validate(payload)
                result = self.broker.operator_decide_image(
                    decision.job_id, decision.sha256, decision.delivery_revision, decision.decision
                )
            elif path == "/operator/state":
                result = self.broker.operator_snapshot()
            elif path == "/operator/cancel":
                cancel = CancelWait.model_validate(payload)
                result = self.broker.operator_cancel_wait(cancel.request_id)
            elif path == "/operator/reorder":
                reorder = ReorderWait.model_validate(payload)
                result = self.broker.operator_reorder_wait(
                    reorder.request_id,
                    reorder.position,
                    expected_revision=reorder.expected_revision,
                )
            else:
                release = ReleaseIdle.model_validate(payload)
                result = self.broker.operator_release_idle(release.lease_id)
        except CapturePolicyError:
            await self._respond(
                send, 200, {"ok": False, "error": "capture_policy_request_rejected"}
            )
        except BrokerError as error:
            await self._respond(send, 200, {"ok": False, "error": str(error)})
        except ValueError:
            await self._respond(send, 400, {"ok": False, "error": "invalid_request"})
        except Exception:
            await self._respond(send, 200, {"ok": False, "error": "operator_request_failed"})
        else:
            await self._respond(send, 200, {"ok": True, **result})
