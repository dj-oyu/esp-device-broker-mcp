"""Local operator control plane for the resident broker; never MCP tools."""

import json
from collections.abc import Callable
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator
from starlette.types import ASGIApp, Receive, Scope, Send

from . import cancellation, job_reads, operator_review, operator_stop, resource_recheck
from .broker import Broker, BrokerError
from .build_logs import BuildLogs
from .models import BuildLogRequest, Digest, Identifier, Origin, TestName
from .operator_live import OperatorLive
from .operator_timeline import OperatorTimeline
from .plan_approval import PlanApprovals

_MAX_BODY = 2048
RETIRED_ROUTES = frozenset(
    {
        "/operator/plans",
        "/operator/plan",
        "/operator/approve-plan",
        "/operator/home-recovery-review",
        "/operator/home-recovery-approve",
        "/operator/image-decision",
        "/operator/policy-options",
        "/operator/policies",
        "/operator/policy-create",
        "/operator/policy-update",
        "/operator/policy-revoke",
        "/operator/capture-arm",
        "/operator/local-home-line",
    }
)


class OperatorPrincipal(Protocol):
    @property
    def principal_id(self) -> str: ...

    @property
    def origin(self) -> Origin: ...

    @property
    def scopes(self) -> frozenset[str]: ...


class CancelWait(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    request_id: Identifier


class CancelJob(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    job_id: Identifier
    plan_hash: Digest
    broker_epoch: str = Field(pattern=r"^[a-f0-9]{32}$")


class ReorderWait(CancelWait):
    position: int = Field(ge=1, le=256)
    expected_revision: Digest


class ReleaseIdle(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    lease_id: Identifier


class ReviewTarget(BaseModel):
    """One needs_review job (id and plan hash) or one device request (id)."""

    model_config = ConfigDict(extra="forbid", strict=True)
    job_id: Identifier | None = None
    plan_hash: Digest | None = None
    request_id: Identifier | None = None
    broker_epoch: str = Field(pattern=r"^[a-f0-9]{32}$")

    @model_validator(mode="after")
    def _one_target(self) -> "ReviewTarget":
        job = self.job_id is not None and self.plan_hash is not None
        partial_job = (self.job_id is None) != (self.plan_hash is None)
        if partial_job or job == (self.request_id is not None):
            raise ValueError("invalid_request")
        return self


class Recheck(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    broker_epoch: str = Field(pattern=r"^[a-f0-9]{32}$")


class PreviewImage(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    job_id: Identifier


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
        self.live = OperatorLive(broker, self._timeline)

    def _operator_profile(self, plan: dict[str, Any] | None, principal: OperatorPrincipal) -> bool:
        return self.broker.profile_jobs.operator_cancellable(
            plan, principal.principal_id, principal.origin.value
        )

    def _timeline(self, principal: OperatorPrincipal) -> dict[str, Any]:
        result = self.timeline.snapshot()
        for card in result["cards"]:
            job = self.broker._store().job(card["job_id"])
            plan = self.broker._store().plan(job["plan_hash"]) if job else None
            card["cancel_available"] = bool(
                card["state"] in ("queued", "running") and self._operator_profile(plan, principal)
            )
            # A device-lane job of the current lease that the operator may stop
            # without interrupting a firmware write (see operator_stop.py).
            card["stop_available"] = bool(
                not card["cancel_available"]
                and card["state"] in ("queued", "running")
                and operator_stop.stop_available(self.broker, card["job_id"])
            )
        return result

    def _job_logs(self, request: BuildLogRequest, principal: OperatorPrincipal) -> dict[str, Any]:
        try:
            return self._retained_job_logs(request, principal)
        except BrokerError as error:
            if str(error) == "not_authorized":
                raise
            raise BrokerError("unavailable") from None
        except Exception:
            raise BrokerError("unavailable") from None

    def _retained_job_logs(
        self, request: BuildLogRequest, principal: OperatorPrincipal
    ) -> dict[str, Any]:
        """The local operator reads any job's redacted output.

        Like every read, the job owner is not required: the owner and stream come
        from `job_reads.log_stream`, the same lookup MCP log reads use. RAM output
        first, then the durable job log.
        """
        if "read" not in principal.scopes or principal.origin != Origin.LOCAL:
            raise BrokerError("not_authorized")
        store = self.broker._store()
        job = store.job(request.job_id)
        plan = store.plan(job["plan_hash"]) if job else None
        if plan is None:
            raise BrokerError("unavailable")
        # 16 * 4096 characters, even JSON control escapes, fits the 512 KiB
        # client transport bound. BuildLogs preserves cursor/has_more on this cap.
        bounded = request.model_copy(update={"limit": min(request.limit, 16)})
        test = plan.get("test")
        if test == TestName.APP_SESSION.value:
            app_logs = self._app_job_logs(request.job_id, job, plan, principal)
            try:
                app = app_logs.read(principal.principal_id, bounded)
            except ValueError:
                raise BrokerError("unavailable") from None
            return {"status": "expired" if app["expired"] else "available", **app}
        owner, service, stream = job_reads.log_stream(self.broker, plan, request.job_id)
        result: dict[str, Any] | None = None
        # RAM only: do not invoke receipt/manifest readers or hash retained artifacts.
        ram: Any = service.logs.get(request.job_id) if service is not None else None
        if ram is not None and isinstance(owner, str):
            try:
                result = ram.read(owner, bounded)
            except ValueError:
                result = None
        if (result is None or result["expired"]) and isinstance(owner, str):
            try:
                durable = self.broker.job_logs.build_read(request.job_id, owner, stream, bounded)
            except ValueError:
                raise BrokerError("unavailable") from None
            if durable is not None:
                result = durable
        if result is None:
            raise BrokerError("unavailable")
        return {"status": "expired" if result["expired"] else "available", **result}

    def _app_job_logs(
        self,
        job_id: str,
        job: dict[str, Any] | None,
        plan: dict[str, Any],
        principal: OperatorPrincipal,
    ) -> BuildLogs:
        """Adapt only this principal's current-epoch app RAM log cache."""
        if principal.origin != Origin.LOCAL or not {"read", "control"} <= principal.scopes:
            raise BrokerError("not_authorized")
        delivery = plan.get("log_delivery")
        if (
            type(delivery) is not dict
            or delivery.get("principal_id") != principal.principal_id
            or delivery.get("origin") != principal.origin.value
        ):
            raise BrokerError("not_authorized")
        run = self.broker.app_sessions.jobs.get(job_id)
        if run is None or job is None:
            raise BrokerError("unavailable")
        if (
            run.principal != principal.principal_id
            or run.events.principal != principal.principal_id
            or run.events.logs.principal != principal.principal_id
        ):
            raise BrokerError("not_authorized")
        # Validate bounded durable JSON/digest/schema only, never current device
        # preflight or artifact/receipt readers. A read grants no execution authority.
        if (
            self.broker.closing
            or plan.get("job_id") != job_id
            or plan.get("broker_epoch") != self.broker.epoch
            or self.plans._plan(job["plan_hash"]) != plan
            or run.options.model_dump(mode="json") != plan.get("app")
            or job.get("state")
            not in {
                "running",
                "succeeded",
                "failed",
                "cancelled",
                "needs_review",
            }
        ):
            raise BrokerError("unavailable")
        binding = (
            self.broker._store()
            .db.execute(
                "SELECT 1 FROM execution_requests "
                "WHERE job_id=? AND plan_hash=? AND principal_id=?",
                (job_id, job["plan_hash"], principal.principal_id),
            )
            .fetchone()
        )
        if binding is None:
            raise BrokerError("unavailable")
        return run.events.logs

    @staticmethod
    async def _respond(send: Send, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
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
            "/operator/events": "GET",
            "/operator/job-logs": "POST",
            "/operator/cancel": "POST",
            "/operator/job-cancel": "POST",
            "/operator/job-stop": "POST",
            "/operator/review": "POST",
            "/operator/recheck": "POST",
            "/operator/reorder": "POST",
            "/operator/release": "POST",
            "/operator/plans": "GET",
            "/operator/plan": "POST",
            "/operator/approve-plan": "POST",
            "/operator/home-recovery-review": "POST",
            "/operator/home-recovery-approve": "POST",
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
        if path in RETIRED_ROUTES:
            # Old review/approval requests must never turn into execution. The
            # existing authentication and local control scope remain required.
            await self._respond(send, 410, {"ok": False, "error": "product_approval_retired"})
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
            elif path == "/operator/timeline":
                result = self._timeline(principal)
            elif path == "/operator/events":
                # Server-Sent Events. Refusals before the stream starts use this JSON
                # boundary; the stream rechecks the current grant on every wake.
                await self.live.stream(principal, lambda: self.authenticate(header), receive, send)
                return
            elif path == "/operator/job-logs":
                result = self._job_logs(
                    BuildLogRequest.model_validate(payload, strict=True), principal
                )
            elif path == "/operator/job-cancel":
                cancel_job = CancelJob.model_validate(payload)
                cancelled = cancellation.cancel_job(
                    self.broker,
                    cancellation.OperatorProfileJob(
                        cancel_job.job_id,
                        cancel_job.plan_hash,
                        cancel_job.broker_epoch,
                        principal.principal_id,
                        principal.origin.value,
                    ),
                )
                result = {
                    "job_id": cancel_job.job_id,
                    "plan_hash": cancel_job.plan_hash,
                    "broker_epoch": self.broker.epoch,
                    "state": cancelled["state"],
                    "stop_requested": cancelled.get("stop_requested", False),
                }
            elif path == "/operator/job-stop":
                # The owner's device-lane stop, authorized for the local operator.
                # Never interrupts a flash/restore/backup; releases nothing.
                stop = CancelJob.model_validate(payload)
                result = cancellation.cancel_job(
                    self.broker,
                    cancellation.OperatorDeviceJob(
                        stop.job_id, stop.plan_hash, stop.broker_epoch, principal.principal_id
                    ),
                )
            elif path == "/operator/review":
                # A human acknowledgement only: sets `reviewed`, never state/result,
                # never retries, never touches the device. Not exposed through MCP.
                target = ReviewTarget.model_validate(payload)
                if target.request_id is not None:
                    result = operator_review.acknowledge_request(
                        self.broker, target.request_id, target.broker_epoch
                    )
                else:
                    assert target.job_id is not None and target.plan_hash is not None
                    result = operator_review.acknowledge_job(
                        self.broker, target.job_id, target.plan_hash, target.broker_epoch
                    )
            elif path == "/operator/recheck":
                # Explicit human action only; never run automatically.
                recheck = Recheck.model_validate(payload)
                if self.broker.closing:
                    raise BrokerError("broker_closing")
                if recheck.broker_epoch != self.broker.epoch:
                    raise BrokerError("stale_operator_snapshot")
                result = resource_recheck.recheck(self.broker)
            elif path == "/operator/images":
                result = self.broker.operator_images()
            elif path == "/operator/image":
                preview = PreviewImage.model_validate(payload)
                result = self.broker.operator_image(preview.job_id)
            elif path == "/operator/state":
                result = self.broker.operator_snapshot()
            elif path == "/operator/cancel":
                cancel = CancelWait.model_validate(payload)
                result = cancellation.cancel_job(
                    self.broker, cancellation.OperatorWait(cancel.request_id)
                )
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
        except BrokerError as error:
            # Live streams also use this JSON error boundary before they start. HTTP 200
            # alone does not mean success; GUI recovery must classify the fixed
            # error code and must not blindly retry every rejected request.
            await self._respond(send, 200, {"ok": False, "error": str(error)})
        except ValueError:
            await self._respond(send, 400, {"ok": False, "error": "invalid_request"})
        except Exception:
            await self._respond(send, 200, {"ok": False, "error": "operator_request_failed"})
        else:
            await self._respond(send, 200, {"ok": True, **result})
