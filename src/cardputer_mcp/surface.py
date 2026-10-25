"""The six-tool MCP surface: status, submit, execute, flash, cancel, session.

This is the only MCP tool layer. Each tool takes one required discriminator
(view/kind/op/target/action); the per-value required and forbidden arguments are
checked here and refused with fixed reason codes. Each tool then calls the broker
and domain functions directly. The principal/scope check is the one `require(ctx,
scope)` passed in by `server.create_server`, and every failure leaves through the one
public-error gate (`_result` / `public_errors.refusal`). The broker still performs
every ownership, plan-hash and lease check; this layer only chooses which operation
an explicit request names.
"""

from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import Awaitable, Callable
from enum import StrEnum
from functools import partial
from typing import Any, Literal, TypeVar, cast

from mcp.server.mcpserver import Context, MCPServer
from mcp.types import CallToolResult, ImageContent, TextContent, ToolAnnotations
from pydantic import BaseModel, Field, TypeAdapter, ValidationError

from . import cancellation, job_reads
from .app_models import (
    U32,
    U64,
    Action,
    AppCommandRequest,
    AppEventCursor,
    AppEventsRequest,
    AppSessionOptions,
)
from .broker import BrokerError
from .derby_reports import binding_unchanged, private_job, shared_job
from .firmware_flash import flash_readiness
from .home_recovery import CancelHomeRecovery, RequestHomeRecovery
from .models import (
    DEFERRED_DEVICE_TESTS,
    FLASH_TESTS,
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
    LeaseRequest,
    LogCursor,
    LogRequest,
    Origin,
    PrepareRequest,
    RenewRequest,
    RequestDeviceRequest,
    ResumeWorkSessionRequest,
    StrictModel,
    TestName,
)
from .public_errors import refusal
from .public_reasons import public_broker_reason, public_reason
from .source_preflight import settings_identity, source_inventory, source_usage
from .source_resources import resource_report
from .source_snapshot import SourceError

TOOL_SET_VERSION = 2

# Plans of these tests overwrite device flash. Only the `flash` tool may queue them;
# the broker derives this from the stored plan, never from the request.
DESTRUCTIVE_TESTS = frozenset({TestName.FLASH.value, TestName.RESTORE.value})

_T = TypeVar("_T")
_M = TypeVar("_M", bound=BaseModel)


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
    JOBS = "jobs"
    RECOVERY = "recovery"


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
    TICKET = "ticket"


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
        description="logs: broker/build {epoch,sequence}; app {epoch,control,log}; jobs {before}",
    )
    limit: int | None = Field(default=None, ge=1, le=100, description="logs|jobs")
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
    after: str | None = Field(
        default=None,
        pattern=r"^[0-9A-HJKMNP-TV-Z]{8}$",
        description=(
            "Continue the job named by this ticket (the chain tip) without waiting for the "
            "device: build after fetch (omit source_receipt), or a device kind (omit session). "
            "Queue it with execute/flash; it waits for its parent and the device. One ticket, "
            "one child. Not for backup or app_session."
        ),
    )
    lease_seconds: int | None = Field(
        default=None, ge=1, le=300, description="with after, device kinds: lease length"
    )
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
    continues: bool | None = Field(
        default=None,
        description=(
            "op=run: like a commit that expects a child; returns a single-use "
            "continuation_ticket. The child is still queued by its own explicit request."
        ),
    )
    chain_total_hint: int | None = Field(
        default=None, ge=1, le=64, description="Display-only chain length hint; needs continues"
    )


class FlashToolRequest(StrictModel):
    plan_hash: Digest = Field(description="Plan prepared with kind=flash or kind=restore.")
    session: Capability | None = Field(default=None, description="Live lease of the plan.")
    continues: bool | None = Field(
        default=None,
        description=(
            "Like a commit that expects a child; returns a single-use continuation_ticket. "
            "The child is still queued by its own explicit request."
        ),
    )
    chain_total_hint: int | None = Field(
        default=None, ge=1, le=64, description="Display-only chain length hint; needs continues"
    )


class CancelToolRequest(StrictModel):
    target: Target = Field(description="job or request.")
    job_id: Identifier | None = Field(default=None, description="target=job")
    session: Capability | None = Field(default=None, description="target=job (device job)")
    plan_hash: Digest | None = Field(
        default=None, description="target=job; required for home recovery"
    )
    resume_token: Capability | None = Field(default=None, description="target=request")
    request_id: Identifier | None = Field(default=None, description="target=request")
    ticket: str | None = Field(
        default=None,
        pattern=r"^[0-9A-HJKMNP-TV-Z]{8}$",
        description="target=ticket: close the chain at its tip; no child can be appended",
    )


class SessionRequest(StrictModel):
    action: SessionAction = Field(description="open|request|renew|release|close.")
    owner: Origin | None = Field(default=None, description="open")
    ttl_seconds: int | None = Field(default=None, ge=1, le=86400, description="open")
    lease_seconds: int | None = Field(default=None, ge=1, le=300, description="open|request|renew")
    wait_seconds: int | None = Field(default=None, ge=0, le=3600, description="open|request")
    after: str | None = Field(
        default=None,
        pattern=r"^[0-9A-HJKMNP-TV-Z]{8}$",
        description=(
            "request: append to your chain. Spend the ticket of your previous device job "
            "(the chain tip) to take the device first. One ticket, one child: no branches, "
            "no merges."
        ),
    )
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


def _declared_field(name: str) -> bool:
    """True when a broker model declares this field name (fixed vocabulary, never input)."""
    stack: list[type[BaseModel]] = [BaseModel]
    while stack:
        model = stack.pop()
        stack.extend(model.__subclasses__())
        if model.__module__.startswith("cardputer_mcp.") and name in model.model_fields:
            return True
    return False


def _convert(
    build: Callable[[], Any], field: str | None = None
) -> tuple[Any, dict[str, Any] | None]:
    """Build an existing request model; map failures to fixed codes, never values.

    `field` names the surface argument being converted. Otherwise only a declared
    field name is reported, so a caller-chosen key (e.g. inside `cursor`) is never echoed.
    """
    try:
        return build(), None
    except ValidationError as error:
        issue = error.errors(include_input=False, include_url=False)[0]
        inner = issue.get("ctx", {}).get("error")
        if isinstance(inner, ValueError):
            reason = public_reason(inner)
            if reason != "invalid_request":
                return None, _refuse(reason)
        if field is not None:
            return None, _refuse("argument_invalid", field)
        parts = [part for part in issue.get("loc", ()) if isinstance(part, str)]
        named = parts[0] if parts and _declared_field(parts[0]) else None
        return None, _refuse("argument_invalid", named)


def _cursor(
    model: type[_M], cursor: dict[str, int | str] | None
) -> tuple[_M | None, dict[str, Any] | None]:
    """Validate a caller cursor; failures name only the `cursor` argument."""
    if cursor is None:
        return None, None
    return _convert(lambda: model.model_validate(cursor), "cursor")


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
    View.JOBS: (set(), {"cursor", "limit"}),
    View.RECOVERY: (set(), set()),
}
JOBS_PAGE_DEFAULT = 20
TEST_KIND = {test.value: kind.value for kind, test in KIND_TEST.items()}


class JobListCursor(StrictModel):
    """Opaque position in the newest-first job list, from a previous next_cursor."""

    before: int = Field(ge=1, le=2**63 - 1)


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
        {"source_receipt", "derby_detail_mode", "derby_dma_mode", "after"},
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
    Op.RUN: ({"plan_hash"}, {"session", "continues", "chain_total_hint"}),
    Op.APP_INPUT: (
        {"session", "job_id", "boot_id", "run_id", "control_session", "operation"},
        {"command_id", "seq", "action"},
    ),
    Op.HOME_RECOVERY: ({"job_id", "source_job_id"}, set()),
}
CANCEL_RULES: dict[Target, tuple[set[str], set[str]]] = {
    Target.JOB: ({"job_id"}, {"session", "plan_hash"}),
    Target.REQUEST: ({"resume_token", "request_id"}, set()),
    Target.TICKET: ({"ticket"}, set()),
}
SESSION_RULES: dict[SessionAction, tuple[set[str], set[str]]] = {
    SessionAction.OPEN: ({"owner"}, {"ttl_seconds", "lease_seconds", "wait_seconds"}),
    SessionAction.REQUEST: ({"resume_token"}, {"lease_seconds", "wait_seconds", "after"}),
    SessionAction.RENEW: ({"session"}, {"lease_seconds"}),
    SessionAction.RELEASE: ({"session"}, set()),
    SessionAction.CLOSE: ({"resume_token"}, set()),
}

PLAN_PRINCIPAL_KEYS = job_reads.PLAN_PRINCIPAL_KEYS


def _wrap(body: dict[str, Any]) -> CallToolResult:
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(body, sort_keys=True))],
        structured_content=body,
    )


def _result(call: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    """The one exception gate for a tool body: `to_public` refusals; anything else re-raises."""
    try:
        return {"ok": True, **call()}
    except ValueError as exc:
        return refusal(exc)


async def _result_async(call: Callable[[], Awaitable[dict[str, Any]]]) -> dict[str, Any]:
    """`_result` for a body that awaits (file IO off the loop, source scans)."""
    try:
        return {"ok": True, **await call()}
    except ValueError as exc:
        return refusal(exc)


def register_surface(
    server: MCPServer[Any],
    *,
    resident: Any,
    require: Callable[..., Any],
    auth_required: bool,
    tunnel_ingress_enabled: bool,
    read_only: ToolAnnotations,
    mutating: ToolAnnotations,
    destructive: ToolAnnotations,
) -> None:
    """Register the six tools. `require(ctx, scope, honor_auth_required=...)` is the only
    principal/scope check; every body below leaves through `_result`/`refusal`."""
    identifier = TypeAdapter(Identifier)

    def registered_ids(registrations: dict[str, Any]) -> list[str]:
        ids = []
        for key in registrations:
            try:
                ids.append(identifier.validate_python(key, strict=True))
            except ValidationError:
                # Legacy local config can contain names that no MCP request accepts.
                continue
        return sorted(ids)

    def bind_lease(owner: Any, capability: str | None) -> None:
        """The lease must belong to this principal; nothing is bound when auth was skipped."""
        if owner is not None:
            resident.require_owner(capability, owner.origin.value, principal_id=owner.principal_id)

    def bind_origin(owner: Any, requested: Origin) -> None:
        if requested != owner.origin:
            raise BrokerError("authenticated_owner_mismatch")

    def _reader(ctx: Context[Any, Any]) -> str | None:
        """The caller's principal for owned_by_caller; None on an unauthenticated mock."""
        principal = require(ctx, "read", honor_auth_required=True)
        return principal.principal_id if principal is not None else None

    def plan_for(plan_hash: str) -> dict[str, Any] | None:
        plan = resident._store().plan(plan_hash)
        return plan if isinstance(plan, dict) else None

    def plan_is_callers(plan: dict[str, Any], ctx: Context[Any, Any], session: str | None) -> bool:
        """The plan's kind is disclosed only to its own principal (or lease holder)."""
        try:
            owner = require(ctx, "control", honor_auth_required=False)
        except BrokerError:
            return False  # the ordinary handler reports the authentication/scope error
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

    # --- status bodies -----------------------------------------------------------------

    def identity_body(ctx: Context[Any, Any]) -> dict[str, Any]:
        """This request's principal/scopes; never credentials or capabilities."""

        def identify() -> dict[str, Any]:
            owner = require(ctx, "read", honor_auth_required=False)
            return {
                "authenticated": True,
                "origin": owner.origin.value,
                "principal_id": owner.principal_id,
                "scopes": sorted(owner.scopes),
                "tunnel_ingress_enabled": tunnel_ingress_enabled,
            }

        return _result(identify)

    def targets_body(ctx: Context[Any, Any]) -> dict[str, Any]:
        """Registered IDs a request can name; registration is not eligibility or connection."""

        def targets() -> dict[str, Any]:
            require(ctx, "read", honor_auth_required=False)
            current = resident.settings
            store = resident._store()
            return {
                "build_projects": [
                    {"project_id": project_id}
                    for project_id in registered_ids(current.build_projects)
                ],
                "flash_devices": [
                    {
                        "device_id": device_id,
                        **flash_readiness(
                            current.flash_devices[device_id],
                            store.firmware_current(device_id) is not None,
                        ),
                    }
                    for device_id in registered_ids(current.flash_devices)
                ],
            }

        return _result(targets)

    async def source_body(project_id: str, ctx: Context[Any, Any]) -> dict[str, Any]:
        """One fresh bounded cache inventory and the caller's retained-source references.

        Never deletes or grants cleanup. The read grant and broker identity are
        rechecked after the scan, which runs off the loop.
        """

        async def report() -> dict[str, Any]:
            owner = require(ctx, "read", honor_auth_required=False)
            store, settings, epoch = resident._store(), resident.settings, resident.epoch
            captured = settings.model_copy(deep=True)
            identity = settings_identity(captured)
            failure: SourceError | None = None
            inventory: dict[str, Any] = {}
            try:
                inventory = await resident.profile_jobs.source_reads.run(
                    partial(source_inventory, captured, project_id)
                )
            except SourceError as exc:
                failure = exc
            current = require(ctx, "read", honor_auth_required=False)
            if (
                resident.closing
                or resident.store is not store
                or resident.epoch != epoch
                or resident.settings is not settings
                or settings_identity(resident.settings) != identity
                or current.principal_id != owner.principal_id
                or current.origin != owner.origin
            ):
                raise BrokerError("source_log_not_available")
            if failure is not None:
                raise failure
            return resource_report(
                store,
                project_id,
                owner.principal_id,
                inventory,
                epoch=epoch,
                now=resident.clock(),
            )

        return await _result_async(report)

    def job_body(job_id: str, ctx: Context[Any, Any]) -> dict[str, Any]:
        """Persisted job state; any read-scoped principal, derby private extras owner-only."""

        def get_job() -> dict[str, Any]:
            job = resident.job(job_id)
            store = resident._store()
            plan = store.plan(job["plan_hash"])
            if plan is not None and plan.get("test") in (
                TestName.DERBY.value,
                TestName.DERBY_FIXED.value,
            ):
                try:
                    owner = require(ctx, "read", honor_auth_required=False)
                except BrokerError:
                    if auth_required:
                        raise
                    return shared_job(job)
                epoch = resident.epoch
                result = private_job(store, job, plan, owner.principal_id)
                if "derby_diagnostics" in result or "fixed_trial" in result:
                    try:
                        current = require(ctx, "read", honor_auth_required=False)
                    except BrokerError:
                        if auth_required:
                            raise
                        return shared_job(job)
                    if (
                        resident.store is not store
                        or resident.epoch != epoch
                        or resident.closing
                        or current.principal_id != owner.principal_id
                        or current.origin != owner.origin
                        or not binding_unchanged(store, job, plan, current.principal_id)
                    ):
                        return shared_job(job)
                return result
            # Status is readable by any read-scoped principal, whoever started the job.
            if plan is not None and plan.get("operation") == "home_recovery":
                require(ctx, "read", honor_auth_required=False)
                job["recovery_audit"] = [
                    dict(row)
                    for row in resident._store().db.execute(
                        "SELECT sequence,event,created FROM home_recovery_audit "
                        "WHERE source_job=? AND recovery_job IN ('',?) ORDER BY sequence LIMIT 64",
                        (plan["source_job_id"], job_id),
                    )
                ]
            if plan is not None and plan.get("execution_context") == "registered_project":
                require(ctx, "read", honor_auth_required=False)
            return shared_job(job)

        def checked() -> dict[str, Any]:
            require(ctx, "read", honor_auth_required=True)
            return get_job()

        return _result(checked)

    async def build_logs_body(request: BuildLogRequest, ctx: Context[Any, Any]) -> dict[str, Any]:
        """Bounded shared source/flash/build output by job, read through the owner's reader.

        The current read grant and the exact broker/job/registration are rechecked
        after any IO off the loop, before private receipts are returned.
        """

        async def get_logs() -> dict[str, Any]:
            owner = require(ctx, "read", honor_auth_required=False)
            store = resident._store()
            job = store.job(request.job_id)
            plan = store.plan(job["plan_hash"]) if job is not None else None
            # Any read-scoped caller reads the output through the job owner's reader,
            # so the body (and its redaction) is exactly what the owner would get.
            reader = job_reads.log_stream(resident, plan, request.job_id)[0] if plan else None
            if not isinstance(reader, str) or not reader:
                reader = None
            if plan is not None and job is not None and plan.get("test") == TestName.SOURCE.value:
                if reader is None:
                    raise BrokerError("source_log_not_available")
                source = resident.firmware_source
                epoch, settings = resident.epoch, resident.settings
                captured = settings.model_copy(deep=True)
                identity = settings_identity(captured)
                result: dict[str, Any] = source.read_logs(request, reader, include_cache=False)
                usage: dict[str, Any] = {"available": False, "reason": "not_requested"}
                if request.include_source_cache:
                    try:
                        usage = await resident.profile_jobs.source_reads.run(
                            partial(source_usage, captured, plan["project_id"])
                        )
                    except SourceError as exc:
                        # Ancillary inventory failure must not hide durable job
                        # results. No partial/previous totals become current usage.
                        usage = {"available": False, "code": exc.code}
                        if "scan" in exc.public():
                            usage["scan"] = exc.public()["scan"]
                current_owner = require(ctx, "read", honor_auth_required=False)
                if (
                    resident.closing
                    or resident.store is not store
                    or resident.epoch != epoch
                    or resident.firmware_source is not source
                    or resident.settings is not settings
                    or settings_identity(resident.settings) != identity
                    or current_owner.principal_id != owner.principal_id
                    or current_owner.origin != owner.origin
                    or store.job(request.job_id) != job
                    or store.plan(job["plan_hash"]) != plan
                ):
                    raise BrokerError("source_log_not_available")
                # The cache read can outlive RAM retention or log eviction. Read
                # the same cursor again only after the current grant is checked.
                result = source.read_logs(request, reader, include_cache=False)
                result["source_cache"] = usage
                return result
            if plan is not None and plan.get("test") in {t.value for t in FLASH_TESTS}:
                if reader is None:
                    raise BrokerError("flash_log_not_available")
                return cast(dict[str, Any], resident.firmware_flash.read_logs(request, reader))
            if (
                plan is None
                or plan.get("test") != TestName.BUILD.value
                or reader is None
                or job is None
            ):
                raise BrokerError("build_log_not_available")
            builder = resident.firmware_build
            epoch = resident.epoch
            settings = resident.settings
            build_settings = builder.settings
            project_id = plan.get("project_id") if job["state"] == "succeeded" else None
            project = build_settings.build_projects.get(project_id) if project_id else None
            registration = project.model_dump_json() if project is not None else None
            try:
                built: dict[str, Any] = await asyncio.to_thread(
                    builder.read_logs,
                    request,
                    reader,
                    project_id=project_id,
                    broker_epoch=epoch,
                )
            except ValueError as exc:
                raise BrokerError(str(exc)) from None
            # Hash IO awaited another thread. Recheck the current read grant and
            # the exact broker/job/registration before returning private receipts.
            current_owner = require(ctx, "read", honor_auth_required=False)
            if (
                resident.closing
                or resident.store is not store
                or resident.epoch != epoch
                or resident.firmware_build is not builder
                or resident.settings is not settings
                or builder.settings is not build_settings
            ):
                raise BrokerError("build_log_not_available")
            current_job = store.job(request.job_id)
            current_project = (
                builder.settings.build_projects.get(project_id) if project_id else None
            )
            current_registration = (
                current_project.model_dump_json() if current_project is not None else None
            )
            if (
                current_owner.principal_id != owner.principal_id
                or current_owner.origin != owner.origin
                or current_registration != registration
                or current_job is None
                or current_job["plan_hash"] != job["plan_hash"]
                or (project_id is not None and current_job["state"] != "succeeded")
                or store.plan(job["plan_hash"]) != plan
            ):
                raise BrokerError("build_log_not_available")
            return built

        return await _result_async(get_logs)

    def capture_result(job_id: str, ctx: Context[Any, Any]) -> CallToolResult:
        """A succeeded job's capture from RAM, then the saved copy; never opens USB.

        The only status path whose domain refusal is isError=true (unsorted JSON text).
        """
        try:
            caller = require(ctx, "read", honor_auth_required=False)
            reader = job_reads.capture_owner(resident, job_id)
            captured = resident.read_capture(job_id, reader or caller.principal_id)
        except BrokerError as exc:
            body = {"ok": False, "error": public_broker_reason(exc)}
            return CallToolResult(
                content=[TextContent(type="text", text=json.dumps(body))],
                structured_content=body,
                is_error=True,
            )
        metadata = {"ok": True, **captured.metadata()}
        return CallToolResult(
            content=[
                TextContent(type="text", text=json.dumps(metadata, sort_keys=True)),
                ImageContent(
                    type="image",
                    mime_type="image/png",
                    data=base64.b64encode(captured.png).decode("ascii"),
                ),
            ],
            structured_content=metadata,
        )

    # --- submit / execute bodies -------------------------------------------------------

    async def prepare_body(request: PrepareRequest, ctx: Context[Any, Any]) -> dict[str, Any]:
        """Prepare an immutable plan; nothing is queued and serial preflight never opens USB."""
        if request.test == TestName.SOURCE:

            def current_owner() -> tuple[str, str]:
                owner = require(ctx, "control", honor_auth_required=False)
                if "read" not in owner.scopes:
                    raise BrokerError("insufficient_scope")
                return owner.principal_id, owner.origin.value

            async def fetch() -> dict[str, Any]:
                owner_id, origin = current_owner()
                return cast(
                    dict[str, Any],
                    await resident.profile_jobs.prepare_source(
                        request, owner_id, origin, reauthorize=current_owner
                    ),
                )

            return await _result_async(fetch)

        def prepare() -> dict[str, Any]:
            if request.after is not None and request.test not in (
                TestName.BUILD,
                TestName.SOURCE,
            ):
                waiter = require(ctx, "control", honor_auth_required=False)
                return cast(
                    dict[str, Any],
                    resident.prepare_deferred(request, waiter.principal_id, waiter.origin.value),
                )
            if request.test in (TestName.BUILD, TestName.SOURCE):
                owner = require(ctx, "control", honor_auth_required=False)
                return cast(
                    dict[str, Any],
                    resident.profile_jobs.prepare(request, owner.principal_id, owner.origin.value),
                )
            if request.test in (
                TestName.CAPTURE,
                TestName.DERBY,
                TestName.DERBY_FIXED,
                TestName.FIRMWARE,
                TestName.RESTART,
                *FLASH_TESTS,
            ):
                # Always authenticated (even on a mock server), then the gated check.
                bind_lease(require(ctx, "control", honor_auth_required=False), request.session)
            bind_lease(require(ctx, "control", honor_auth_required=True), request.session)
            return cast(dict[str, Any], resident.prepare(request))

        return _result(prepare)

    async def execute_body(request: ExecuteRequest, ctx: Context[Any, Any]) -> dict[str, Any]:
        """Queue one exact plan on its explicit request; the same job ID never replays."""

        def authorize() -> None:
            bind_lease(require(ctx, "control", honor_auth_required=True), request.session)

        async def execute() -> dict[str, Any]:
            plan = resident._store().plan(request.plan_hash)
            if plan is not None and plan.get("test") in (
                TestName.BUILD.value,
                TestName.SOURCE.value,
            ):
                owner = require(ctx, "control", honor_auth_required=False)
                job = resident.profile_jobs.execute(request, owner.principal_id, owner.origin.value)
            elif plan is not None and plan.get("execution_context") == "device_deferred":
                waiter = require(ctx, "control", honor_auth_required=False)
                job = resident.execute_deferred(request, waiter.principal_id, waiter.origin.value)
            else:
                authorize()
                # Firmware file verification runs off the loop; authority is checked
                # again before the final synchronous checks and the enqueue.
                job = await resident.execute_async(request, reauthorize=authorize)
            return shared_job(job)

        return await _result_async(execute)

    async def enqueue(
        ctx: Context[Any, Any],
        plan_hash: str,
        session: str | None,
        continues: bool | None,
        hint: int | None,
    ) -> dict[str, Any]:
        """Queue one plan on its own explicit request; optionally issue its ticket."""
        if hint is not None and not continues:
            return _refuse("argument_required", "continues")
        if continues:
            plan = plan_for(plan_hash)
            if (
                plan is not None
                and "session_id" not in plan
                and not str(plan.get("execution_context", "")).startswith("registered_project")
                and plan.get("execution_context") != "device_deferred"
            ):
                return _refuse("argument_not_allowed", "continues")
        model, error = _convert(lambda: ExecuteRequest(plan_hash=plan_hash, session=session))
        body: dict[str, Any] = error or await execute_body(model, ctx)
        if continues and body.get("ok") and isinstance(body.get("id"), str):
            try:
                body["continuation_ticket"] = resident.issue_ticket(body["id"], hint)
            except BrokerError as exc:
                body["continuation_error"] = public_broker_reason(exc)
        return body

    # --- the six tools -----------------------------------------------------------------

    @server.tool(annotations=read_only)
    async def status(request: StatusRequest, ctx: Context[Any, Any]) -> CallToolResult:
        """Read broker, job, log or session state. view selects what; no side effects.

        Any read-scoped caller can read any job (job, logs, capture), whoever
        started it; owned_by_caller says whether it is yours. Changes stay owner-only.
        view=job adds `chain` for your own chained jobs, like git log from that job:
        chain_position, chain_jobs_known, chain_open (the tip still accepts a child).
        view=jobs lists recent jobs, newest first (cursor from next_cursor).
        view=recovery lists per device the LKG receipts and full backups.
        view=capture returns the PNG of a succeeded capture: RAM for five minutes,
        then a saved copy kept across restarts (oldest pruned past 200 / 64 MiB).
        """
        required, optional = STATUS_RULES[request.view]
        refused = _check(request, "view", required, optional)
        if refused is not None:
            return _wrap(refused)
        view = request.view
        if view is View.IDENTITY:
            body = identity_body(ctx)
            if body.get("ok"):
                body["tool_set_version"] = TOOL_SET_VERSION
        elif view is View.TARGETS:
            body = targets_body(ctx)
        elif view is View.SOURCE:
            assert request.project_id is not None
            body = await source_body(request.project_id, ctx)
        elif view is View.DEVICE:

            def device() -> dict[str, Any]:
                require(ctx, "read", honor_auth_required=True)
                return cast(dict[str, Any], resident.status())

            body = _result(device)
        elif view is View.SESSION:
            assert request.resume_token is not None
            resume = ResumeWorkSessionRequest(resume_token=_req(request.resume_token))

            def resumed() -> dict[str, Any]:
                # Logical sessions are always authenticated, including on a legacy mock server.
                owner = require(ctx, "read", honor_auth_required=False)
                return cast(
                    dict[str, Any],
                    resident.resume_work_session(
                        resume, principal_id=owner.principal_id, scopes=owner.scopes
                    ),
                )

            body = _result(resumed)
        elif view is View.JOB:
            assert request.job_id is not None
            job_id = _req(request.job_id)
            waiting_view = None
            caller: str | None = None
            try:
                caller = require(ctx, "read", honor_auth_required=False).principal_id
                owner = job_reads.deferred_owner(resident, job_id)
                if owner is not None:
                    waiting_view = resident.deferred_job(job_id, owner)
            except BrokerError:
                waiting_view = None
            if waiting_view is not None:
                mine = job_reads.same_principal(job_reads.deferred_owner(resident, job_id), caller)
                chain = resident.chain_progress(job_id) if mine else None
                return _wrap(
                    {
                        "ok": True,
                        **waiting_view,
                        "owned_by_caller": mine,
                        **({"chain": chain} if chain is not None else {}),
                    }
                )
            body = job_body(job_id, ctx)
            if body.get("ok") and isinstance(body.get("plan_hash"), str):
                plan = plan_for(body["plan_hash"])
                mine = plan is not None and job_reads.same_principal(
                    job_reads.job_owner(resident, job_id, plan), caller
                )
                body["owned_by_caller"] = mine
                if (
                    plan is not None
                    and caller is not None
                    and resident.job_principal(job_id) == caller
                ):
                    chain = resident.chain_progress(job_id)
                    if chain is not None:
                        body["chain"] = chain
        elif view is View.JOBS:
            jobs_cursor, error = _cursor(JobListCursor, request.cursor)
            body = error or _result(
                lambda: job_reads.list_jobs(
                    resident,
                    _reader(ctx),
                    TEST_KIND,
                    before=jobs_cursor.before if jobs_cursor is not None else None,
                    limit=request.limit or JOBS_PAGE_DEFAULT,
                )
            )
        elif view is View.RECOVERY:
            body = _result(lambda: job_reads.recovery(resident, _reader(ctx)))
        elif view is View.APPS:

            def catalog() -> dict[str, Any]:
                # The cached app-control catalog and freshness; never opens USB.
                require(ctx, "read", honor_auth_required=False)
                return cast(dict[str, Any], resident.app_sessions.list_apps())

            body = _result(catalog)
        elif view is View.CAPTURE:
            assert request.job_id is not None
            return capture_result(_req(request.job_id), ctx)
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
            log_cursor, error = _cursor(LogCursor, cursor)
            if error is not None:
                return error
            model, error = _convert(lambda: LogRequest(cursor=log_cursor, **extra))
            if error is not None:
                return error

            def broker_logs() -> dict[str, Any]:
                # Bounded cached events; never acquires or reads a serial port.
                require(ctx, "read", honor_auth_required=True)
                return cast(dict[str, Any], resident.read_logs(model))

            return _result(broker_logs)
        assert request.job_id is not None
        job_id = _req(request.job_id)
        if stream is Stream.BUILD:
            for name in ("levels", "context", "include_source_cache"):
                if getattr(request, name) is not None:
                    extra[name] = getattr(request, name)
            log_cursor, error = _cursor(LogCursor, cursor)
            if error is not None:
                return error
            model, error = _convert(
                lambda: BuildLogRequest(job_id=job_id, cursor=log_cursor, **extra)
            )
            return error or await build_logs_body(model, ctx)
        if stream is Stream.DEVICE:

            def report() -> dict[str, Any]:
                # The owner's bounded redacted firmware_observe/restart_and_observe report.
                caller = require(ctx, "read", honor_auth_required=False)
                reader = job_reads.device_log_owner(resident, job_id)
                return cast(
                    dict[str, Any],
                    resident.read_device_logs(job_id, reader or caller.principal_id),
                )

            return _result(report)
        app_cursor, error = _cursor(AppEventCursor, cursor)
        if error is not None:
            return error
        events_request, error = _convert(
            lambda: AppEventsRequest(job_id=job_id, cursor=app_cursor, **extra)
        )
        if error is not None:
            return error

        def events() -> dict[str, Any]:
            # Private bounded control ACKs and redacted logs, read through the owner.
            caller = require(ctx, "read", honor_auth_required=False)
            reader = job_reads.app_event_owner(resident, job_id)
            return cast(
                dict[str, Any],
                resident.read_app_events(events_request, reader or caller.principal_id),
            )

        return _result(events)

    @server.tool(annotations=mutating)
    async def submit(request: SubmitRequest, ctx: Context[Any, Any]) -> dict[str, Any]:
        """Prepare an immutable plan for one operation (kind). Nothing is queued.

        Device kinds need a live session lease. Queue the plan with the tool named
        in execute_tool: flash for flash and restore, otherwise execute.
        """
        required, optional = SUBMIT_RULES[request.kind]
        if "after" in request.model_fields_set:
            if request.kind is Kind.BUILD:
                pass  # build keeps its own rule: after is optional there
            elif KIND_TEST[request.kind] in DEFERRED_DEVICE_TESTS:
                # Requested ahead of time: no live lease, the ticket names the parent.
                required = (required - {"session"}) | {"after"}
                optional = optional | {"lease_seconds"}
            else:
                return _refuse("argument_not_allowed", "after")
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
        body = await prepare_body(model, ctx)
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
            return await enqueue(
                ctx,
                request.plan_hash,
                request.session,
                request.continues,
                request.chain_total_hint,
            )
        if request.op is Op.APP_INPUT:
            fields = {
                name: getattr(request, name) for name in request.model_fields_set if name != "op"
            }
            app_model, error = _convert(lambda: AppCommandRequest(**fields))
            if error is not None:
                return error

            def command() -> dict[str, Any]:
                # Typed input inside one exact app_session plan; status never replays.
                bind_lease(require(ctx, "control", honor_auth_required=False), app_model.session)
                return cast(dict[str, Any], resident.app_command(app_model))

            return _result(command)
        assert request.job_id is not None and request.source_job_id is not None
        recovery, error = _convert(
            lambda: RequestHomeRecovery(
                job_id=_req(request.job_id), source_job_id=_req(request.source_job_id)
            )
        )
        if error is not None:
            return error

        def home_recovery() -> dict[str, Any]:
            # One bounded q after a proved-closed DERBY capture failure; no retry.
            owner = require(ctx, "control", honor_auth_required=False)
            return cast(
                dict[str, Any],
                resident.home_recovery.request(recovery, owner.principal_id, owner.origin.value),
            )

        return _result(home_recovery)

    @server.tool(annotations=destructive)
    async def flash(request: FlashToolRequest, ctx: Context[Any, Any]) -> dict[str, Any]:
        """Queue a prepared flash or restore plan; these overwrite device flash.

        Plans of any other kind are refused here; use the execute tool.
        """
        if auth_required:
            wrong = wrong_tool(request.plan_hash, request.session, ctx, "flash")
            if wrong is not None:
                return wrong
        return await enqueue(
            ctx, request.plan_hash, request.session, request.continues, request.chain_total_hint
        )

    @server.tool(annotations=mutating)
    async def cancel(request: CancelToolRequest, ctx: Context[Any, Any]) -> dict[str, Any]:
        """Cancel a job (target=job) or a waiting device request (target=request)."""
        required, optional = CANCEL_RULES[request.target]
        refused = _check(request, "target", required, optional)
        if refused is not None:
            return refused
        if request.target is Target.TICKET:
            try:
                owner = require(ctx, "control", honor_auth_required=False)
                return {
                    "ok": True,
                    **cancellation.cancel_job(
                        resident, cancellation.Ticket(_req(request.ticket), owner.principal_id)
                    ),
                }
            except BrokerError as exc:
                return _refuse(public_broker_reason(exc))
        if request.target is Target.REQUEST:
            assert request.resume_token is not None and request.request_id is not None
            wait, error = _convert(
                lambda: CancelDeviceRequest(
                    resume_token=_req(request.resume_token), request_id=_req(request.request_id)
                )
            )
            if error is not None:
                return error

            def cancel_wait() -> dict[str, Any]:
                # Logical sessions are always authenticated, including on a legacy mock server.
                owner = require(ctx, "control", honor_auth_required=False)
                return cancellation.cancel_job(
                    resident, cancellation.DeviceRequestWait(wait, owner.principal_id, owner.scopes)
                )

            return _result(cancel_wait)
        assert request.job_id is not None
        try:
            cancelled = cancellation.cancel_job(
                resident,
                cancellation.DeferredJob(
                    request.job_id, require(ctx, "control", honor_auth_required=False).principal_id
                ),
            )
        except BrokerError:
            cancelled = None
        if cancelled is not None:
            return {"ok": True, **cancelled}
        if request.session is None:
            try:
                running = cancellation.cancel_job(
                    resident,
                    cancellation.JobLeaseJob(
                        request.job_id,
                        require(ctx, "control", honor_auth_required=False).principal_id,
                    ),
                )
            except BrokerError as exc:
                return _refuse(public_broker_reason(exc))
            if running is not None:
                return {"ok": True, **shared_job(running)}
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
            if error is not None:
                return error
            # Stop this principal's exact recovery; ownership is kept until confirmed close.
            return _result(
                lambda: cancellation.cancel_job(
                    resident,
                    cancellation.HomeRecoveryJob(
                        recovery, require(ctx, "control", honor_auth_required=False).principal_id
                    ),
                )
            )
        stop, error = _convert(
            lambda: CancelRequest(job_id=_req(request.job_id), session=request.session)
        )
        if error is not None:
            return error

        def cancel_owned() -> dict[str, Any]:
            # Source/build stops use the current control principal and no USB lease;
            # device jobs keep their lease ownership and interruption rules.
            stored = resident._store().job(stop.job_id)
            stored_plan = (
                resident._store().plan(stored["plan_hash"]) if stored is not None else None
            )
            if stored_plan is not None and stored_plan.get("test") in (
                TestName.BUILD.value,
                TestName.SOURCE.value,
            ):
                owner = require(ctx, "control", honor_auth_required=False)
                return cancellation.cancel_job(
                    resident,
                    cancellation.ProfileJob(stop, owner.principal_id, owner.origin.value),
                )
            bind_lease(require(ctx, "control", honor_auth_required=True), stop.session)
            return cancellation.cancel_job(resident, cancellation.LeasedDeviceJob(stop))

        return _result(lambda: shared_job(cancel_owned()))

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

        # Logical work sessions are always authenticated, including on a legacy mock
        # server; lease actions on a session capability and the immediate acquire are not.
        def create_work(work: CreateWorkSessionRequest) -> dict[str, Any]:
            def create() -> dict[str, Any]:
                owner = require(ctx, "control", honor_auth_required=False)
                bind_origin(owner, work.owner)
                return cast(
                    dict[str, Any],
                    resident.create_work_session(
                        work, principal_id=owner.principal_id, scopes=owner.scopes
                    ),
                )

            return _result(create)

        def request_device(queued: RequestDeviceRequest) -> dict[str, Any]:
            def queue() -> dict[str, Any]:
                owner = require(ctx, "control", honor_auth_required=False)
                return cast(
                    dict[str, Any],
                    resident.request_device(
                        queued, principal_id=owner.principal_id, scopes=owner.scopes
                    ),
                )

            return _result(queue)

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
                return error or create_work(work)
            if not request.wait_seconds:
                if request.ttl_seconds is not None:
                    return _refuse("argument_not_allowed", "ttl_seconds")
                acquire, error = _convert(
                    lambda: AcquireRequest(owner=_req(request.owner), lease_seconds=lease)
                )
                if error is not None:
                    return error

                def reserve() -> dict[str, Any]:
                    owner = require(ctx, "control", honor_auth_required=True)
                    if owner is None:
                        return cast(dict[str, Any], resident.acquire(acquire))
                    bind_origin(owner, acquire.owner)
                    return cast(
                        dict[str, Any], resident.acquire(acquire, principal_id=owner.principal_id)
                    )

                return _result(reserve)
            ttl = {"ttl_seconds": request.ttl_seconds} if request.ttl_seconds else {}
            work, error = _convert(
                lambda: CreateWorkSessionRequest(owner=_req(request.owner), **ttl)
            )
            if error is not None:
                return error
            created = create_work(work)
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
            return {**created, "device_request": request_device(queued)}
        if action is SessionAction.REQUEST:
            assert request.resume_token is not None
            extra: dict[str, Any] = {}
            if request.lease_seconds is not None:
                extra["lease_seconds"] = request.lease_seconds
            if request.wait_seconds is not None:
                if request.wait_seconds < 1:
                    return _refuse("argument_invalid", "wait_seconds")
                extra["wait_seconds"] = request.wait_seconds
            if request.after is not None:
                extra["after"] = request.after
            queued, error = _convert(
                lambda: RequestDeviceRequest(resume_token=_req(request.resume_token), **extra)
            )
            return error or request_device(queued)
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
            if error is not None:
                return error

            def extend() -> dict[str, Any]:
                # Extend a live lease using its in-memory bearer capability.
                bind_lease(require(ctx, "control", honor_auth_required=True), renew.session)
                return cast(dict[str, Any], resident.renew(renew))

            return _result(extend)
        if action is SessionAction.RELEASE:
            assert request.session is not None
            lease_model, error = _convert(lambda: LeaseRequest(session=_req(request.session)))
            if error is not None:
                return error

            def release() -> dict[str, Any]:
                # Release an idle session; active work cannot be cancelled or released here.
                bind_lease(require(ctx, "control", honor_auth_required=True), lease_model.session)
                return cast(dict[str, Any], resident.release(lease_model))

            return _result(release)
        assert request.resume_token is not None
        close, error = _convert(
            lambda: CloseWorkSessionRequest(resume_token=_req(request.resume_token))
        )
        if error is not None:
            return error

        def close_work() -> dict[str, Any]:
            # Close the logical workflow safely; it cannot stop an active physical job.
            owner = require(ctx, "control", honor_auth_required=False)
            return cast(
                dict[str, Any],
                resident.close_work_session(
                    close, principal_id=owner.principal_id, scopes=owner.scopes
                ),
            )

        return _result(close_work)
