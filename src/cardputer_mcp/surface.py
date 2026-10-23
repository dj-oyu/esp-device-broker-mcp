"""The six-tool MCP surface: status, submit, execute, flash, cancel, session.

A thin layer over the existing handlers. Each tool takes one required discriminator
(view/kind/op/target/action); the per-value required and forbidden arguments are
checked here and refused with fixed reason codes. The broker still performs every
authentication, ownership, plan-hash and lease check; this layer only chooses which
existing operation an explicit request names.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from enum import StrEnum
from typing import Any, Literal, TypeVar, cast

from mcp.server.mcpserver import Context, MCPServer
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from pydantic import Field, ValidationError

from .app_models import (
    U32,
    U64,
    Action,
    AppCommandRequest,
    AppEventCursor,
    AppEventsRequest,
    AppSessionOptions,
)
from .home_recovery import CancelHomeRecovery, RequestHomeRecovery
from .models import (
    AcquireRequest,
    BuildLogLevel,
    BuildLogRequest,
    CancelDeviceRequest,
    CancelRequest,
    Capability,
    CloseWorkSessionRequest,
    CreateWorkSessionRequest,
    DiagnosticMode,
    Digest,
    ExecuteRequest,
    Identifier,
    JobRequest,
    LeaseRequest,
    LogCursor,
    LogRequest,
    Origin,
    PrepareRequest,
    RenewRequest,
    RequestDeviceRequest,
    ResumeWorkSessionRequest,
    SourceResourcesRequest,
    StrictModel,
    TestName,
)
from .public_reasons import public_reason

TOOL_SET_VERSION = 2

# Plans of these tests overwrite device flash. Only the `flash` tool may queue them;
# the broker derives this from the stored plan, never from the request.
DESTRUCTIVE_TESTS = frozenset({TestName.FLASH.value, TestName.RESTORE.value})

Handler = Callable[..., Awaitable[Any]]
_T = TypeVar("_T")


class View(StrEnum):
    IDENTITY = "identity"
    TARGETS = "targets"
    SOURCE = "source"
    DEVICE = "device"
    SESSION = "session"
    JOB = "job"
    LOGS = "logs"
    CAPTURE = "capture"
    APPS = "apps"


class Stream(StrEnum):
    BROKER = "broker"
    BUILD = "build"
    DEVICE = "device"
    APP = "app"


class Kind(StrEnum):
    FETCH = "fetch"
    BUILD = "build"
    BACKUP = "backup"
    FLASH = "flash"
    RESTORE = "restore"
    APP_SESSION = "app_session"
    DERBY_FIXED = "derby_fixed"
    DERBY = "derby"
    CAPTURE = "capture"
    RESTART = "restart"
    OBSERVE = "observe"
    OBSERVE_FIRMWARE = "observe_firmware"
    HELLO = "hello"
    DIAGNOSTICS = "diagnostics"
    WORKFLOW = "workflow"


class Op(StrEnum):
    RUN = "run"
    APP_INPUT = "app_input"
    HOME_RECOVERY = "home_recovery"


class Target(StrEnum):
    JOB = "job"
    REQUEST = "request"


class SessionAction(StrEnum):
    OPEN = "open"
    REQUEST = "request"
    RENEW = "renew"
    RELEASE = "release"
    CLOSE = "close"


KIND_TEST = {
    Kind.FETCH: TestName.SOURCE,
    Kind.BUILD: TestName.BUILD,
    Kind.BACKUP: TestName.BACKUP,
    Kind.FLASH: TestName.FLASH,
    Kind.RESTORE: TestName.RESTORE,
    Kind.APP_SESSION: TestName.APP_SESSION,
    Kind.DERBY_FIXED: TestName.DERBY_FIXED,
    Kind.DERBY: TestName.DERBY,
    Kind.CAPTURE: TestName.CAPTURE,
    Kind.RESTART: TestName.RESTART,
    Kind.OBSERVE: TestName.OBSERVE,
    Kind.OBSERVE_FIRMWARE: TestName.FIRMWARE,
    Kind.HELLO: TestName.HELLO,
    Kind.DIAGNOSTICS: TestName.DIAGNOSTICS,
    Kind.WORKFLOW: TestName.WORKFLOW,
}


class StatusRequest(StrictModel):
    view: View = Field(description="What to read; each view lists its own arguments.")
    project_id: Identifier | None = Field(default=None, description="view=source")
    job_id: Identifier | None = Field(
        default=None, description="view=job|capture; logs build|device|app"
    )
    resume_token: Capability | None = Field(default=None, description="view=session")
    stream: Stream | None = Field(default=None, description="view=logs: broker|build|device|app")
    cursor: dict[str, int | str] | None = Field(
        default=None,
        description="logs: broker/build {epoch,sequence}; app {epoch,control,log}",
    )
    limit: int | None = Field(default=None, ge=1, le=100, description="logs")
    levels: list[BuildLogLevel] | None = Field(default=None, max_length=5, description="logs build")
    context: int | None = Field(default=None, ge=0, le=10, description="logs build")
    include_source_cache: bool | None = Field(default=None, description="logs build")


class SubmitRequest(StrictModel):
    kind: Kind = Field(description="Operation to prepare; creates an immutable plan, no effect.")
    job_id: Identifier
    session: Capability | None = Field(default=None, description="Live lease for device kinds.")
    project_id: Identifier | None = Field(default=None, description="fetch|build")
    source_commit: str | None = Field(default=None, pattern=r"^[a-f0-9]{40}$", description="fetch")
    source_receipt: Digest | None = Field(default=None, description="build")
    device_id: Identifier | None = Field(default=None, description="backup|flash|restore")
    build_job_id: Identifier | None = Field(default=None, description="flash")
    backup_job_id: Identifier | None = Field(default=None, description="flash|restore")
    lkg_receipt: Digest | None = Field(default=None, description="flash|restore")
    derby_detail_mode: DiagnosticMode | None = Field(default=None, description="build")
    derby_dma_mode: DiagnosticMode | None = Field(default=None, description="build")
    flash_job_id: Identifier | None = Field(default=None, description="derby_fixed")
    app: AppSessionOptions | None = Field(default=None, description="app_session")


class ExecuteToolRequest(StrictModel):
    op: Op = Field(description="run: queue a non-flash plan; app_input; home_recovery.")
    plan_hash: Digest | None = Field(default=None, description="op=run")
    session: Capability | None = Field(default=None, description="op=run|app_input")
    job_id: Identifier | None = Field(default=None, description="app_input|home_recovery")
    source_job_id: Identifier | None = Field(default=None, description="home_recovery")
    boot_id: U64 | None = Field(default=None, description="app_input")
    run_id: U32 | None = Field(default=None, description="app_input")
    control_session: U32 | None = Field(default=None, description="app_input")
    command_id: U32 | None = Field(default=None, description="app_input")
    seq: U32 | None = Field(default=None, description="app_input")
    operation: Literal["action.tap", "status", "suspend", "resume", "stop"] | None = Field(
        default=None, description="app_input"
    )
    action: Action | None = Field(default=None, description="app_input operation=action.tap")


class FlashToolRequest(StrictModel):
    plan_hash: Digest = Field(description="Plan prepared with kind=flash or kind=restore.")
    session: Capability | None = Field(default=None, description="Live lease of the plan.")


class CancelToolRequest(StrictModel):
    target: Target = Field(description="job or request.")
    job_id: Identifier | None = Field(default=None, description="target=job")
    session: Capability | None = Field(default=None, description="target=job (device job)")
    plan_hash: Digest | None = Field(
        default=None, description="target=job; required for home recovery"
    )
    resume_token: Capability | None = Field(default=None, description="target=request")
    request_id: Identifier | None = Field(default=None, description="target=request")


class SessionRequest(StrictModel):
    action: SessionAction = Field(description="open|request|renew|release|close.")
    owner: Origin | None = Field(default=None, description="open")
    ttl_seconds: int | None = Field(default=None, ge=1, le=86400, description="open")
    lease_seconds: int | None = Field(default=None, ge=1, le=300, description="open|request|renew")
    wait_seconds: int | None = Field(default=None, ge=0, le=3600, description="open|request")
    resume_token: Capability | None = Field(default=None, description="request|close")
    session: Capability | None = Field(default=None, description="renew|release")


def _req(value: _T | None) -> _T:
    """Rules checked before this guarantee the argument is present."""
    assert value is not None
    return value


def _refuse(error: str, field: str | None = None, **extra: str) -> dict[str, Any]:
    body: dict[str, Any] = {"ok": False, "error": error}
    if field is not None:
        body["field"] = field
    body.update(extra)
    return body


def _check(
    request: StrictModel,
    discriminator: str,
    required: set[str],
    optional: set[str],
) -> dict[str, Any] | None:
    present = request.model_fields_set - {discriminator}
    for name in sorted(required - present):
        return _refuse("argument_required", name)
    for name in sorted(present - required - optional):
        return _refuse("argument_not_allowed", name)
    return None


def _convert(build: Callable[[], Any]) -> tuple[Any, dict[str, Any] | None]:
    """Build an existing request model; map failures to fixed codes, never values."""
    try:
        return build(), None
    except ValidationError as error:
        issue = error.errors(include_input=False, include_url=False)[0]
        inner = issue.get("ctx", {}).get("error")
        if isinstance(inner, ValueError):
            reason = public_reason(inner)
            if reason != "invalid_request":
                return None, _refuse(reason)
        parts = [part for part in issue.get("loc", ()) if isinstance(part, str)]
        return None, _refuse("argument_invalid", parts[0] if parts else None)


STATUS_RULES: dict[View, tuple[set[str], set[str]]] = {
    View.IDENTITY: (set(), set()),
    View.TARGETS: (set(), set()),
    View.SOURCE: ({"project_id"}, set()),
    View.DEVICE: (set(), set()),
    View.SESSION: ({"resume_token"}, set()),
    View.JOB: ({"job_id"}, set()),
    View.LOGS: (
        {"stream"},
        {"cursor", "limit", "levels", "context", "include_source_cache", "job_id"},
    ),
    View.CAPTURE: ({"job_id"}, set()),
    View.APPS: (set(), set()),
}
STREAM_RULES: dict[Stream, tuple[set[str], set[str]]] = {
    Stream.BROKER: (set(), {"cursor", "limit"}),
    Stream.BUILD: (
        {"job_id"},
        {"cursor", "limit", "levels", "context", "include_source_cache"},
    ),
    Stream.DEVICE: ({"job_id"}, set()),
    Stream.APP: ({"job_id"}, {"cursor", "limit"}),
}

_DEVICE_KINDS = {
    Kind.BACKUP,
    Kind.FLASH,
    Kind.RESTORE,
    Kind.APP_SESSION,
    Kind.DERBY_FIXED,
    Kind.DERBY,
    Kind.CAPTURE,
    Kind.RESTART,
    Kind.OBSERVE,
    Kind.OBSERVE_FIRMWARE,
    Kind.HELLO,
    Kind.DIAGNOSTICS,
    Kind.WORKFLOW,
}
_RECOVERY = {"backup_job_id", "lkg_receipt"}
SUBMIT_RULES: dict[Kind, tuple[set[str], set[str]]] = {
    Kind.FETCH: ({"job_id", "project_id", "source_commit"}, set()),
    Kind.BUILD: (
        {"job_id", "project_id"},
        {"source_receipt", "derby_detail_mode", "derby_dma_mode"},
    ),
    Kind.BACKUP: ({"job_id", "session", "device_id"}, set()),
    Kind.FLASH: ({"job_id", "session", "device_id", "build_job_id"}, set(_RECOVERY)),
    Kind.RESTORE: ({"job_id", "session", "device_id"}, set(_RECOVERY)),
    Kind.APP_SESSION: ({"job_id", "session", "app"}, set()),
    Kind.DERBY_FIXED: ({"job_id", "session"}, {"flash_job_id"}),
}
for _kind in _DEVICE_KINDS - set(SUBMIT_RULES):
    SUBMIT_RULES[_kind] = ({"job_id", "session"}, set())

EXECUTE_RULES: dict[Op, tuple[set[str], set[str]]] = {
    Op.RUN: ({"plan_hash"}, {"session"}),
    Op.APP_INPUT: (
        {"session", "job_id", "boot_id", "run_id", "control_session", "operation"},
        {"command_id", "seq", "action"},
    ),
    Op.HOME_RECOVERY: ({"job_id", "source_job_id"}, set()),
}
CANCEL_RULES: dict[Target, tuple[set[str], set[str]]] = {
    Target.JOB: ({"job_id"}, {"session", "plan_hash"}),
    Target.REQUEST: ({"resume_token", "request_id"}, set()),
}
SESSION_RULES: dict[SessionAction, tuple[set[str], set[str]]] = {
    SessionAction.OPEN: ({"owner"}, {"ttl_seconds", "lease_seconds", "wait_seconds"}),
    SessionAction.REQUEST: ({"resume_token"}, {"lease_seconds", "wait_seconds"}),
    SessionAction.RENEW: ({"session"}, {"lease_seconds"}),
    SessionAction.RELEASE: ({"session"}, set()),
    SessionAction.CLOSE: ({"resume_token"}, set()),
}

PLAN_PRINCIPAL_KEYS = ("principal_id", "flash_principal", "build_principal", "source_principal")


def _wrap(body: dict[str, Any]) -> CallToolResult:
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(body, sort_keys=True))],
        structured_content=body,
    )


def register_surface(
    server: MCPServer[Any],
    *,
    ops: dict[str, Handler],
    resident: Any,
    principal: Callable[[Context[Any, Any], Any], Any],
    auth_required: bool,
    read_only: ToolAnnotations,
    mutating: ToolAnnotations,
    destructive: ToolAnnotations,
) -> None:
    def plan_for(plan_hash: str) -> dict[str, Any] | None:
        plan = resident._store().plan(plan_hash)
        return plan if isinstance(plan, dict) else None

    def plan_is_callers(plan: dict[str, Any], ctx: Context[Any, Any], session: str | None) -> bool:
        """The plan's kind is disclosed only to its own principal (or lease holder)."""
        owner = principal(ctx, "control")
        bound = [plan[key] for key in PLAN_PRINCIPAL_KEYS if isinstance(plan.get(key), str)]
        if bound:
            return all(value == owner.principal_id for value in bound)
        return False

    def wrong_tool(
        plan_hash: str, session: str | None, ctx: Context[Any, Any], tool: str
    ) -> dict[str, Any] | None:
        """Refuse a plan of the other safety class before any state is changed."""
        plan = plan_for(plan_hash)
        if plan is None or not plan_is_callers(plan, ctx, session):
            return None  # the ordinary execute path reports not-found/ownership
        flashes = plan.get("test") in DESTRUCTIVE_TESTS
        if tool == "execute" and flashes:
            return _refuse("wrong_execute_tool", use="flash")
        if tool == "flash" and not flashes:
            return _refuse("wrong_flash_tool", use="execute")
        return None

    @server.tool(annotations=read_only)
    async def status(request: StatusRequest, ctx: Context[Any, Any]) -> CallToolResult:
        """Read broker, job, log or session state. view selects what; no side effects."""
        required, optional = STATUS_RULES[request.view]
        refused = _check(request, "view", required, optional)
        if refused is not None:
            return _wrap(refused)
        view = request.view
        if view is View.IDENTITY:
            body = await ops["client_identity"](ctx)
            if body.get("ok"):
                body["tool_set_version"] = TOOL_SET_VERSION
        elif view is View.TARGETS:
            body = await ops["list_registered_targets"](ctx)
        elif view is View.SOURCE:
            assert request.project_id is not None
            body = await ops["read_source_resources"](
                SourceResourcesRequest(project_id=request.project_id), ctx
            )
        elif view is View.DEVICE:
            body = await ops["device_status"](ctx)
        elif view is View.SESSION:
            assert request.resume_token is not None
            body = await ops["resume_work_session"](
                ResumeWorkSessionRequest(resume_token=_req(request.resume_token)), ctx
            )
        elif view is View.JOB:
            assert request.job_id is not None
            body = await ops["job_status"](JobRequest(job_id=_req(request.job_id)), ctx)
        elif view is View.APPS:
            body = await ops["list_apps"](ctx)
        elif view is View.CAPTURE:
            assert request.job_id is not None
            result = await ops["read_capture"](JobRequest(job_id=_req(request.job_id)), ctx)
            return cast(CallToolResult, result)
        else:
            assert request.stream is not None
            required, optional = STREAM_RULES[request.stream]
            refused = _check(request, "view", required | {"stream"}, optional | {"stream"})
            if refused is not None:
                return _wrap(refused)
            body = await _read_logs(request, ctx)
        return _wrap(body)

    async def _read_logs(request: StatusRequest, ctx: Context[Any, Any]) -> dict[str, Any]:
        stream = request.stream
        cursor = request.cursor
        extra: dict[str, Any] = {}
        if request.limit is not None:
            extra["limit"] = request.limit
        if stream is Stream.BROKER:
            model, error = _convert(
                lambda: LogRequest(
                    cursor=LogCursor.model_validate(cursor) if cursor is not None else None,
                    **extra,
                )
            )
            return error or await ops["read_logs"](model, ctx)
        assert request.job_id is not None
        if stream is Stream.BUILD:
            for name in ("levels", "context", "include_source_cache"):
                if getattr(request, name) is not None:
                    extra[name] = getattr(request, name)
            model, error = _convert(
                lambda: BuildLogRequest(
                    job_id=_req(request.job_id),
                    cursor=LogCursor.model_validate(cursor) if cursor is not None else None,
                    **extra,
                )
            )
            return error or await ops["read_build_logs"](model, ctx)
        if stream is Stream.DEVICE:
            device: dict[str, Any] = await ops["read_device_logs"](
                JobRequest(job_id=_req(request.job_id)), ctx
            )
            return device
        model, error = _convert(
            lambda: AppEventsRequest(
                job_id=_req(request.job_id),
                cursor=AppEventCursor.model_validate(cursor) if cursor is not None else None,
                **extra,
            )
        )
        return error or await ops["read_app_events"](model, ctx)

    @server.tool(annotations=mutating)
    async def submit(request: SubmitRequest, ctx: Context[Any, Any]) -> dict[str, Any]:
        """Prepare an immutable plan for one operation (kind). Nothing is queued.

        Device kinds need a live session lease. Queue the plan with the tool named
        in execute_tool: flash for flash and restore, otherwise execute.
        """
        required, optional = SUBMIT_RULES[request.kind]
        refused = _check(request, "kind", required, optional)
        if refused is not None:
            return refused
        if (
            _RECOVERY <= request.model_fields_set
            and request.backup_job_id is not None
            and (request.lkg_receipt is not None)
        ):
            return _refuse("argument_conflict", "lkg_receipt")
        test = KIND_TEST[request.kind]
        fields = {
            name: getattr(request, name) for name in request.model_fields_set if name != "kind"
        }
        model, error = _convert(lambda: PrepareRequest(test=test, **fields))
        if error is not None:
            return error
        body: dict[str, Any] = await ops["prepare_test"](model, ctx)
        if body.get("ok"):
            body["execute_tool"] = "flash" if test.value in DESTRUCTIVE_TESTS else "execute"
        return body

    @server.tool(annotations=mutating)
    async def execute(request: ExecuteToolRequest, ctx: Context[Any, Any]) -> dict[str, Any]:
        """Queue a prepared plan (op=run), send typed app input, or request home recovery.

        Plans of kind flash and restore are refused here; use the flash tool.
        """
        required, optional = EXECUTE_RULES[request.op]
        refused = _check(request, "op", required, optional)
        if refused is not None:
            return refused
        if request.op is Op.RUN:
            assert request.plan_hash is not None
            if auth_required:
                wrong = wrong_tool(request.plan_hash, request.session, ctx, "execute")
                if wrong is not None:
                    return wrong
            model, error = _convert(
                lambda: ExecuteRequest(plan_hash=_req(request.plan_hash), session=request.session)
            )
            return error or await ops["execute_test"](model, ctx)
        if request.op is Op.APP_INPUT:
            fields = {
                name: getattr(request, name) for name in request.model_fields_set if name != "op"
            }
            app_model, error = _convert(lambda: AppCommandRequest(**fields))
            return error or await ops["app_command"](app_model, ctx)
        assert request.job_id is not None and request.source_job_id is not None
        recovery, error = _convert(
            lambda: RequestHomeRecovery(
                job_id=_req(request.job_id), source_job_id=_req(request.source_job_id)
            )
        )
        return error or await ops["request_home_recovery"](recovery, ctx)

    @server.tool(annotations=destructive)
    async def flash(request: FlashToolRequest, ctx: Context[Any, Any]) -> dict[str, Any]:
        """Queue a prepared flash or restore plan; these overwrite device flash.

        Plans of any other kind are refused here; use the execute tool.
        """
        if auth_required:
            wrong = wrong_tool(request.plan_hash, request.session, ctx, "flash")
            if wrong is not None:
                return wrong
        model, error = _convert(
            lambda: ExecuteRequest(plan_hash=_req(request.plan_hash), session=request.session)
        )
        return error or await ops["execute_test"](model, ctx)

    @server.tool(annotations=mutating)
    async def cancel(request: CancelToolRequest, ctx: Context[Any, Any]) -> dict[str, Any]:
        """Cancel a job (target=job) or a waiting device request (target=request)."""
        required, optional = CANCEL_RULES[request.target]
        refused = _check(request, "target", required, optional)
        if refused is not None:
            return refused
        if request.target is Target.REQUEST:
            assert request.resume_token is not None and request.request_id is not None
            model, error = _convert(
                lambda: CancelDeviceRequest(
                    resume_token=_req(request.resume_token), request_id=_req(request.request_id)
                )
            )
            return error or await ops["cancel_device_request"](model, ctx)
        assert request.job_id is not None
        job = resident._store().job(request.job_id)
        plan = resident._store().plan(job["plan_hash"]) if job is not None else None
        if request.plan_hash is not None and (
            job is None or job.get("plan_hash") != request.plan_hash
        ):
            return _refuse("job_plan_mismatch", "plan_hash")
        if isinstance(plan, dict) and plan.get("operation") == "home_recovery":
            if request.plan_hash is None:
                return _refuse("argument_required", "plan_hash")
            recovery, error = _convert(
                lambda: CancelHomeRecovery(
                    job_id=_req(request.job_id), plan_hash=_req(request.plan_hash)
                )
            )
            return error or await ops["cancel_home_recovery"](recovery, ctx)
        model, error = _convert(
            lambda: CancelRequest(job_id=_req(request.job_id), session=request.session)
        )
        return error or await ops["cancel_job"](model, ctx)

    @server.tool(annotations=mutating)
    async def session(request: SessionRequest, ctx: Context[Any, Any]) -> dict[str, Any]:
        """Open, request, renew, release or close a work session and its device lease.

        open without lease_seconds creates a work session. open with lease_seconds
        and wait_seconds=0 (or omitted) reserves the device at once or fails with
        device_busy; wait_seconds>=1 creates a work session and queues for the device.
        """
        required, optional = SESSION_RULES[request.action]
        refused = _check(request, "action", required, optional)
        if refused is not None:
            return refused
        action = request.action
        if action is SessionAction.OPEN:
            assert request.owner is not None
            lease = request.lease_seconds
            if lease is None:
                if request.wait_seconds is not None:
                    return _refuse("argument_required", "lease_seconds")
                ttl = {"ttl_seconds": request.ttl_seconds} if request.ttl_seconds else {}
                work, error = _convert(
                    lambda: CreateWorkSessionRequest(owner=_req(request.owner), **ttl)
                )
                return error or await ops["create_work_session"](work, ctx)
            if not request.wait_seconds:
                if request.ttl_seconds is not None:
                    return _refuse("argument_not_allowed", "ttl_seconds")
                acquire, error = _convert(
                    lambda: AcquireRequest(owner=_req(request.owner), lease_seconds=lease)
                )
                return error or await ops["acquire_session"](acquire, ctx)
            ttl = {"ttl_seconds": request.ttl_seconds} if request.ttl_seconds else {}
            work, error = _convert(
                lambda: CreateWorkSessionRequest(owner=_req(request.owner), **ttl)
            )
            if error is not None:
                return error
            created: dict[str, Any] = await ops["create_work_session"](work, ctx)
            if not created.get("ok"):
                return created
            queued, error = _convert(
                lambda: RequestDeviceRequest(
                    resume_token=created["resume_token"],
                    lease_seconds=lease,
                    wait_seconds=_req(request.wait_seconds),
                )
            )
            if error is not None:
                return {**created, "device_request": error}
            return {**created, "device_request": await ops["request_device"](queued, ctx)}
        if action is SessionAction.REQUEST:
            assert request.resume_token is not None
            extra: dict[str, Any] = {}
            if request.lease_seconds is not None:
                extra["lease_seconds"] = request.lease_seconds
            if request.wait_seconds is not None:
                if request.wait_seconds < 1:
                    return _refuse("argument_invalid", "wait_seconds")
                extra["wait_seconds"] = request.wait_seconds
            queued, error = _convert(
                lambda: RequestDeviceRequest(resume_token=_req(request.resume_token), **extra)
            )
            return error or await ops["request_device"](queued, ctx)
        if action is SessionAction.RENEW:
            assert request.session is not None
            renew_extra = (
                {"lease_seconds": request.lease_seconds}
                if request.lease_seconds is not None
                else {}
            )
            renew, error = _convert(
                lambda: RenewRequest(session=_req(request.session), **renew_extra)
            )
            return error or await ops["renew_session"](renew, ctx)
        if action is SessionAction.RELEASE:
            assert request.session is not None
            lease_model, error = _convert(lambda: LeaseRequest(session=_req(request.session)))
            return error or await ops["release_session"](lease_model, ctx)
        assert request.resume_token is not None
        close, error = _convert(
            lambda: CloseWorkSessionRequest(resume_token=_req(request.resume_token))
        )
        return error or await ops["close_work_session"](close, ctx)
