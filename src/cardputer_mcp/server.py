"""Official MCP SDK v2, one lifespan-owned broker, Streamable HTTP only."""

import asyncio
import base64
import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from functools import partial
from math import isfinite
from pathlib import Path
from time import time
from typing import Any, Literal, Protocol, overload

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import (
    CallToolResult,
    ImageContent,
    InputRequiredResult,
    TextContent,
    ToolAnnotations,
)
from pydantic import TypeAdapter, ValidationError
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from . import cancellation, job_reads
from .app_models import AppCommandRequest, AppEventsRequest
from .auth import AuthState
from .broker import Broker, BrokerError
from .config import Settings
from .derby_reports import binding_unchanged, private_job, shared_job
from .firmware_flash import flash_readiness
from .home_recovery import CancelHomeRecovery, RequestHomeRecovery
from .http_diagnostics import (
    INGRESS_CONTEXT_KEY,
    DenialReason,
    HTTPDiagnostics,
    request_context,
    route_category,
)
from .input_diagnostics import invalid_arguments, validate_arguments
from .key_registry import KeyRegistry, KeyRegistryError
from .key_registry import Scope as KeyScope
from .models import (
    FLASH_TESTS,
    AcquireRequest,
    BuildLogRequest,
    CancelDeviceRequest,
    CancelRequest,
    CloseWorkSessionRequest,
    CreateWorkSessionRequest,
    ExecuteRequest,
    Identifier,
    JobRequest,
    LeaseRequest,
    LogRequest,
    Origin,
    PrepareRequest,
    RenewRequest,
    RequestDeviceRequest,
    ResumeWorkSessionRequest,
    SourceResourcesRequest,
    TestName,
)
from .operator_api import OperatorAPI
from .public_errors import refusal
from .public_reasons import public_broker_reason
from .registry_defaults import LiveKeyRegistry, default_key_registry_path
from .source_preflight import settings_identity, source_inventory, source_usage
from .source_resources import resource_report
from .source_snapshot import SourceError
from .surface import register_surface


class Authenticator(Protocol):
    def authenticate(self, header: str | None) -> object | None: ...


@dataclass(frozen=True)
class Principal:
    principal_id: str
    origin: Origin
    scopes: frozenset[KeyScope]


class RequestAuth:
    """Resolve each request independently; registry revocation is never cached."""

    def __init__(
        self,
        ephemeral: list[AuthState | None],
        registry: list[KeyRegistry | LiveKeyRegistry | None],
    ) -> None:
        self.ephemeral = ephemeral
        self.registry = registry

    def authenticate(self, header: str | None) -> Principal | None:
        state = self.ephemeral[0]
        origin = state.authenticate(header) if state is not None else None
        if origin is not None:
            return Principal(f"ephemeral:{origin.value}", origin, frozenset({"read", "control"}))
        registry = self.registry[0]
        key = registry.authenticate(header) if registry is not None else None
        if key is None:
            return None
        return Principal(key.principal_id, key.origin, key.scopes)

    def principal_valid(
        self, principal_id: str, origin: str, required_scopes: frozenset[str]
    ) -> bool:
        """Recheck grant eligibility from metadata only; never retain bearer tokens."""
        if origin not in (Origin.LOCAL.value, Origin.REMOTE.value):
            return False
        if principal_id == f"ephemeral:{origin}":
            return self.ephemeral[0] is not None and required_scopes <= {"read", "control"}
        registry = self.registry[0]
        if registry is None:
            return False
        try:
            now = time()
            if not isfinite(now) or now < 0:
                return False
            for key in registry.list_keys():
                if key.principal_id == principal_id:
                    return (
                        key.origin.value == origin
                        and key.revoked_at is None
                        and key.issued_at <= now < key.expires_at
                        and required_scopes <= key.scopes
                    )
        except (KeyRegistryError, ValueError, OSError):
            return False
        return False


def require_principal(
    authenticate: Callable[[str | None], Principal | None],
    header: str | None,
    scope: KeyScope,
    *,
    auth_required: bool,
    honor_auth_required: bool,
) -> Principal | None:
    """The one principal/scope check behind every MCP handler.

    `honor_auth_required=True` is the legacy-mock exception: when this server does
    not require authentication the check is skipped and None is returned (status,
    logs, job status, lease actions on a session capability, immediate acquire).
    `honor_auth_required=False` always authenticates, even on a mock server without
    configured credentials, where it raises `authentication_required` (logical work
    sessions, registered-project and deferred jobs, principal-bound reads).
    """
    if honor_auth_required and not auth_required:
        return None
    owner = authenticate(header)
    if owner is None:
        raise BrokerError("authentication_required")
    if scope not in owner.scopes:
        raise BrokerError("insufficient_scope")
    return owner


class SafeMCPServer(MCPServer[Any]):
    """Do not echo rejected arguments through SDK validation/unknown-tool errors."""

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        context: Context[Any, Any] | None = None,
    ) -> CallToolResult | InputRequiredResult:
        try:
            diagnostic = validate_arguments(name, arguments)
            if diagnostic is not None:
                return self._invalid(diagnostic)
            return await super().call_tool(name, arguments, context)
        except Exception:
            return self._invalid(invalid_arguments())

    @staticmethod
    def _invalid(diagnostic: dict[str, Any]) -> CallToolResult:
        return CallToolResult(
            content=[TextContent(type="text", text=json.dumps(diagnostic, sort_keys=True))],
            structured_content=diagnostic,
            is_error=True,
        )


class LoopbackOnly:
    """Defense in depth: no public bind, proxy identity, wildcard host, or browser origin."""

    def __init__(
        self,
        app: ASGIApp,
        port: int,
        auth: Callable[[], Authenticator | None] | None = None,
        diagnostics: HTTPDiagnostics | None = None,
    ) -> None:
        self.app = app
        self.auth = auth
        self.diagnostics = diagnostics
        self.hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        self.origins = {f"http://{host}" for host in self.hosts}

    def check_headers(
        self, scope: Scope, allowed_forwarding: frozenset[str] = frozenset()
    ) -> tuple[dict[str, str], DenialReason | None]:
        """Validate raw transport metadata before any optional ingress normalization."""
        rejection: DenialReason | None = None
        try:
            pairs = [
                (key.decode("ascii").lower(), value.decode("ascii"))
                for key, value in scope["headers"]
            ]
            headers = dict(pairs)
            if len(headers) != len(pairs):
                rejection = DenialReason.DUPLICATE_HEADERS
        except UnicodeError:
            headers = {}
            rejection = DenialReason.NON_ASCII_HEADERS
        client = scope.get("client")
        # Preserve the original guard order and rejection rules for the direct path.
        if rejection is not None:
            pass
        elif not client or client[0] not in ("127.0.0.1", "::1"):
            rejection = DenialReason.NON_LOOPBACK_PEER
        elif headers.get("host") not in self.hosts:
            rejection = DenialReason.HOST_NOT_ALLOWED
        elif "origin" in headers and headers["origin"] not in self.origins:
            rejection = DenialReason.ORIGIN_NOT_ALLOWED
        elif any(
            (name.startswith("x-forwarded-") or name == "forwarded")
            and name not in allowed_forwarding
            for name in headers
        ):
            rejection = DenialReason.FORWARDING_HEADER_PRESENT
        elif (
            scope["method"] == "POST"
            and headers.get("content-type", "").split(";", 1)[0] != "application/json"
        ):
            rejection = DenialReason.CONTENT_TYPE_NOT_JSON
        return headers, rejection

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        route = route_category(scope.get("path", ""))
        if scope["type"] == "http":
            headers, rejection = self.check_headers(scope)
            if rejection is not None:
                if self.diagnostics is not None:
                    self.diagnostics.record(
                        rejection,
                        route,
                        403,
                        context=request_context(
                            scope,
                            "post_ingress_guard"
                            if scope.get(INGRESS_CONTEXT_KEY)
                            else "direct_guard",
                        ),
                    )
                await send(
                    {
                        "type": "http.response.start",
                        "status": 403,
                        "headers": [(b"content-type", b"application/json")],
                    }
                )
                await send({"type": "http.response.body", "body": b'{"error":"local_only"}'})
                return
            if self.auth is not None:
                authenticator = self.auth()
                if (
                    authenticator is None
                    or authenticator.authenticate(headers.get("authorization")) is None
                ):
                    if self.diagnostics is not None:
                        self.diagnostics.record(
                            DenialReason.AUTHENTICATION_REQUIRED,
                            route,
                            401,
                            context=request_context(
                                scope,
                                "post_ingress_auth"
                                if scope.get(INGRESS_CONTEXT_KEY)
                                else "direct_guard",
                            ),
                        )
                    await send(
                        {
                            "type": "http.response.start",
                            "status": 401,
                            "headers": [(b"content-type", b"application/json")],
                        }
                    )
                    await send(
                        {
                            "type": "http.response.body",
                            "body": b'{"error":"authentication_required"}',
                        }
                    )
                    return
        error_response = False
        error_sent = False

        async def safe_send(message: Message) -> None:
            nonlocal error_response, error_sent
            if message["type"] == "http.response.start" and message["status"] >= 400:
                if self.diagnostics is not None:
                    reason = {
                        "mcp": DenialReason.MCP_HTTP_REJECTION,
                        "operator": DenialReason.OPERATOR_HTTP_REJECTION,
                        "other": DenialReason.OTHER_HTTP_REJECTION,
                    }[route]
                    self.diagnostics.record(
                        reason,
                        route,
                        message["status"],
                        context=request_context(scope, route + "_handler"),
                    )
                error_response = True
                message = {**message, "headers": [(b"content-type", b"application/json")]}
            elif message["type"] == "http.response.body" and error_response:
                if error_sent:
                    return
                error_sent = True
                message = {
                    "type": "http.response.body",
                    "body": b'{"error":"request_rejected"}',
                    "more_body": False,
                }
            await send(message)

        # SDK legacy-envelope validation may include caller input in 4xx bodies.
        # Keep the status, but never reflect those bodies to clients or logs.
        await self.app(scope, receive, safe_send)


class TunnelIngress:
    """Opt-in fixed MCP alias; authenticated proxy metadata is discarded, never trusted.

    This wraps the same resident app and never creates a client, broker, listener,
    session or credential. Non-MCP/operator paths are never normalized. The raw
    peer, Host, Origin, duplicate-header and JSON rules are checked before auth
    and normalization, then checked again by the original guard afterwards.
    """

    PATH = "/tunnel/mcp"
    FORWARDING_METADATA = frozenset(
        {
            "forwarded",
            "x-forwarded-for",
            "x-forwarded-host",
            "x-forwarded-proto",
            "x-forwarded-port",
            "x-forwarded-client-cert",
        }
    )

    def __init__(
        self, guarded: LoopbackOnly, authenticate: Callable[[str | None], Principal | None]
    ) -> None:
        self.guarded = guarded
        self.authenticate = authenticate

    async def _reject(
        self, scope: Scope, send: Send, status: int, reason: DenialReason, body: bytes
    ) -> None:
        if self.guarded.diagnostics is not None:
            stage = (
                "ingress_auth"
                if reason
                in (DenialReason.AUTHENTICATION_REQUIRED, DenialReason.TUNNEL_PRINCIPAL_REJECTED)
                else "ingress_guard"
            )
            self.guarded.diagnostics.record(
                reason, "mcp", status, context=request_context(scope, stage)
            )
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send({"type": "http.response.body", "body": body})

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("path") != self.PATH:
            await self.guarded(scope, receive, send)
            return
        headers, rejection = self.guarded.check_headers(scope, self.FORWARDING_METADATA)
        if rejection is not None:
            await self._reject(scope, send, 403, rejection, b'{"error":"local_only"}')
            return
        if (
            scope.get("raw_path", self.PATH.encode("ascii")) != self.PATH.encode("ascii")
            or scope.get("root_path", "")
            or scope.get("app_root_path", "")
            or scope.get("query_string", b"")
        ):
            await self._reject(
                scope,
                send,
                400,
                DenialReason.TUNNEL_ROUTE_REJECTED,
                b'{"error":"invalid_tunnel_route"}',
            )
            return
        principal = self.authenticate(headers.get("authorization"))
        if principal is None:
            await self._reject(
                scope,
                send,
                401,
                DenialReason.AUTHENTICATION_REQUIRED,
                b'{"error":"authentication_required"}',
            )
            return
        if principal.origin != Origin.REMOTE or "read" not in principal.scopes:
            await self._reject(
                scope,
                send,
                403,
                DenialReason.TUNNEL_PRINCIPAL_REJECTED,
                b'{"error":"remote_read_principal_required"}',
            )
            return
        # Preserve the original ASGI scope and header list for any outer caller.
        # Never use proxy metadata as identity, URL, origin or effective peer.
        forwarded_scope = dict(scope)
        forwarded_scope[INGRESS_CONTEXT_KEY] = True
        forwarded_scope["path"] = "/mcp"
        forwarded_scope["raw_path"] = b"/mcp"
        forwarded_scope["headers"] = [
            (key, value)
            for key, value in scope["headers"]
            if key.decode("ascii").lower() not in self.FORWARDING_METADATA
        ]
        await self.guarded(forwarded_scope, receive, send)


def _result(call: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    try:
        return {"ok": True, **call()}
    except ValueError as exc:
        return refusal(exc)


def create_server(
    settings: Settings,
    broker: Broker | None = None,
    *,
    serial_enabled: bool = False,
    require_auth: bool = False,
    auth_source: str = "environment",
    auth_holder: list[AuthState | None] | None = None,
    key_registry_path: Path | None = None,
    key_registry_holder: list[KeyRegistry | LiveKeyRegistry | None] | None = None,
) -> MCPServer[Any]:
    if settings.backend == "serial" and not serial_enabled:
        raise BrokerError("serial_runtime_opt_in_required")
    if auth_source not in ("environment", "file"):
        raise BrokerError("unsupported_auth_source")
    auth_required = settings.backend == "serial" or require_auth or key_registry_path is not None
    holder: list[AuthState | None] = auth_holder if auth_holder is not None else [None]
    registry_holder: list[KeyRegistry | LiveKeyRegistry | None] = (
        key_registry_holder if key_registry_holder is not None else [None]
    )
    authenticator = RequestAuth(holder, registry_holder)
    resident = broker or Broker(settings, serial_enabled=serial_enabled)
    resident.principal_valid = authenticator.principal_valid
    runtime_path = settings.state_dir / "runtime-auth.json"

    @overload
    def require(
        ctx: Context[Any, Any], scope: KeyScope, *, honor_auth_required: Literal[False]
    ) -> Principal: ...
    @overload
    def require(
        ctx: Context[Any, Any], scope: KeyScope, *, honor_auth_required: bool
    ) -> Principal | None: ...
    def require(
        ctx: Context[Any, Any], scope: KeyScope, *, honor_auth_required: bool
    ) -> Principal | None:
        """Every MCP handler's principal/scope check; see `require_principal`."""
        if honor_auth_required and not auth_required:
            return None  # skipped before the request headers are read
        headers = ctx.headers
        return require_principal(
            authenticator.authenticate,
            headers.get("authorization") if headers else None,
            scope,
            auth_required=auth_required,
            honor_auth_required=honor_auth_required,
        )

    def bind_lease(owner: Principal | None, capability: str | None) -> None:
        """The lease must belong to this principal; nothing is bound when auth was skipped."""
        if owner is not None:
            resident.require_owner(capability, owner.origin.value, principal_id=owner.principal_id)

    def bind_origin(owner: Principal, requested: Origin) -> None:
        if requested != owner.origin:
            raise BrokerError("authenticated_owner_mismatch")

    @asynccontextmanager
    async def lifespan(_: MCPServer[Any]) -> AsyncIterator[None]:
        await resident.start()
        try:
            if key_registry_path is not None:
                registry_holder[0] = KeyRegistry(key_registry_path, create=False)
            elif auth_required:
                registry_holder[0] = LiveKeyRegistry(default_key_registry_path())
            if auth_required:
                if holder[0] is None:
                    holder[0] = (
                        AuthState.from_environment(enable_real_auth=True)
                        if auth_source == "environment"
                        else AuthState(enable_real_auth=True)
                    )
                state = holder[0]
                assert state is not None
                if auth_source == "file":
                    state.write_runtime(runtime_path)
            yield None
        finally:
            try:
                await resident.close()
            finally:
                try:
                    if auth_required and holder[0] is not None and auth_source == "file":
                        holder[0].close_runtime(runtime_path)
                finally:
                    holder[0] = None
                    if registry_holder[0] is not None:
                        registry_holder[0].close()
                    registry_holder[0] = None

    server: MCPServer[Any] = SafeMCPServer(
        "Cardputer Device Broker",
        version="0.13.0",
        lifespan=lifespan,
        instructions=(
            "Single-owner Cardputer broker. Mock mode is the default. Explicit serial mode "
            "supports bounded observation, HELLO and HOME render-buffer capture, with "
            "authenticated, scoped clients. Acquire, prepare, then explicitly execute_test. "
            "Preparation alone never requests execution. Capture uses a fresh HOME handshake; "
            "Esc can exit another app. Read captures within five minutes using the same "
            "authenticated identity and current read scope. "
            "Pixels do not prove LCD transfer. "
            "request_home_recovery explicitly requests one bounded DERBY q exit "
            "only after a recorded capture failure and confirmed close; no prior HOME "
            "attestation is asserted. It cannot resolve flash reviews or retry itself. "
            "No arbitrary shell/raw serial commands are available. Flash requires "
            "registered device/build/backup and verified identities. "
            "Restart executes only on an explicit exact-plan request."
        ),
        log_level="WARNING",
    )
    read_only = ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=False)
    mutating = ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=False)
    execution = ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=False)
    identifier = TypeAdapter(Identifier)
    ops: dict[str, Any] = {}

    def op(handler: Any) -> Any:
        """Keep a handler as an internal operation; only the six surface tools are MCP tools."""
        ops[handler.__name__] = handler
        return handler

    def registered_ids(registrations: dict[str, Any]) -> list[str]:
        ids = []
        for key in registrations:
            try:
                ids.append(identifier.validate_python(key, strict=True))
            except ValidationError:
                # Legacy local config can contain names that no MCP request accepts.
                continue
        return sorted(ids)

    @op
    async def client_identity(ctx: Context[Any, Any]) -> dict[str, Any]:
        """Inspect this request's principal/scopes; never return credentials or capabilities."""

        def identify() -> dict[str, Any]:
            owner = require(ctx, "read", honor_auth_required=False)
            return {
                "authenticated": True,
                "origin": owner.origin.value,
                "principal_id": owner.principal_id,
                "scopes": sorted(owner.scopes),
                "tunnel_ingress_enabled": (
                    settings.tunnel is not None and settings.tunnel.ingress_enabled
                ),
            }

        return _result(identify)

    @op
    async def list_registered_targets(ctx: Context[Any, Any]) -> dict[str, Any]:
        """List current addressable registered IDs with read scope; no hardware access.

        Pass no arguments. Use project_id for source/build and device_id for
        backup/flash/restore. Registration does not establish operation eligibility
        or a physical connection. Paths, selectors and credentials stay private.
        Names outside the request ID format (1–64 letters/digits/underscore/hyphen)
        are omitted; they cannot be used in prepare_test.
        Each device reports its enabled operations, flash_baseline (not_required when
        flash is enabled: a flash needs no LKG or backup) and lkg_recorded.
        """

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

    @op
    async def read_source_resources(
        request: SourceResourcesRequest, ctx: Context[Any, Any]
    ) -> dict[str, Any]:
        """Read one fresh bounded cache inventory and your retained-source references.

        This read never deletes or grants cleanup. Explicit source fetches can
        reclaim unreferenced payloads while retaining receipts. Unknown and
        referenced data remain protected. Scan-limit responses
        distinguish deadline/entries and contain no incomplete or cached totals.
        For ordinary source log polling use include_source_cache=false.
        """
        try:
            project_id = request.project_id
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
            return {
                "ok": True,
                **resource_report(
                    store,
                    project_id,
                    owner.principal_id,
                    inventory,
                    epoch=epoch,
                    now=resident.clock(),
                ),
            }
        except ValueError as exc:
            return refusal(exc)

    @op
    async def device_status(ctx: Context[Any, Any]) -> dict[str, Any]:
        """Read broker state without opening hardware. Never returns session capabilities."""

        def status() -> dict[str, Any]:
            require(ctx, "read", honor_auth_required=True)
            return resident.status()

        return _result(status)

    @op
    async def read_logs(request: LogRequest, ctx: Context[Any, Any]) -> dict[str, Any]:
        """Read bounded cached events using a cursor; does not acquire or read a serial port."""

        def logs() -> dict[str, Any]:
            require(ctx, "read", honor_auth_required=True)
            return resident.read_logs(request)

        return _result(logs)

    @op
    async def acquire_session(request: AcquireRequest, ctx: Context[Any, Any]) -> dict[str, Any]:
        """Reserve a workflow; authenticated mode binds ownership to the client identity."""

        def acquire() -> dict[str, Any]:
            owner = require(ctx, "control", honor_auth_required=True)
            if owner is None:
                return resident.acquire(request)
            bind_origin(owner, request.owner)
            return resident.acquire(request, principal_id=owner.principal_id)

        return _result(acquire)

    @op
    async def renew_session(request: RenewRequest, ctx: Context[Any, Any]) -> dict[str, Any]:
        """Extend a live lease using its in-memory bearer capability."""

        def renew() -> dict[str, Any]:
            bind_lease(require(ctx, "control", honor_auth_required=True), request.session)
            return resident.renew(request)

        return _result(renew)

    @op
    async def release_session(request: LeaseRequest, ctx: Context[Any, Any]) -> dict[str, Any]:
        """Release an idle session. Active work cannot be cancelled or released this way."""

        def release() -> dict[str, Any]:
            bind_lease(require(ctx, "control", honor_auth_required=True), request.session)
            return resident.release(request)

        return _result(release)

    @op
    async def prepare_test(request: PrepareRequest, ctx: Context[Any, Any]) -> dict[str, Any]:
        """Prepare an immutable plan; serial preflight enumerates without opening USB.

        app_session fixes registered app/start, typed allowed actions, command/rate/
        duration/log limits and stop/teardown effects in request.app. Fresh is default.
        list_apps only reads cached catalog. execute handshakes actual boot/catalog/
        reported firmware identity; observed identity is not attestation. app_command
        never accepts source text, paths, held keys or raw serial. Device TTL and
        bounded STOP/CLOSE cleanup are agreed by the immutable app_session plan.

        Source fetch is an explicit registered-project/full-commit operation.
        It returns a principal-bound receipt; build can select it without worktree sync.
        Registered source/build plans require current control authorization and
        no physical lease. Omit session for these jobs.
        Registered build scripts run as the local OS user; this is not a sandbox.
        Source fetch never builds or flashes. No caller-supplied URL/path/shell is accepted.
        firmware_flash requires request.job_id, test=firmware_flash, a live owned
        request.session, registered request.device_id and successful owned
        request.build_job_id. Do not pass project_id: the registered device selects
        its project. backup_job_id and lkg_receipt are mutually exclusive and add a
        pre-write recovery fence; with both omitted no recovery point is required.
        firmware_backup requires session/device_id, with no build/recovery inputs.
        firmware_restore requires session/device_id and verified recovery, with no
        build_job_id or project_id. Explicit null optional references are allowed.

        Registered source builds may select derby_detail_mode=ON or OFF for the
        allowlisted J1 source. derby_dma_mode is a separate ON/OFF selector that
        requires both explicit axes and a separately approved DMA-contract entry;
        the runtime registry currently admits J1 only and rejects DMA requests.
        Fixed-workload collectors may select flash_job_id
        to bind expected ELF identity to a successful owned current flash receipt.
        All use request={...}; no caller-supplied port, path or raw flash arguments.
        Backup, flash and restore are independent explicit exact-plan operations.
        Flash may use a retained verified LKG instead of another full backup.
        Restore binds the exact recovery receipt at prepare time. Artifact recovery
        is firmware-only; an explicit full backup restore also rewrites device data.
        DERBY smoke is an explicitly requested fixed row-12 launch, bounded automatic
        demo observation, one render-buffer capture and explicit q exit. It exposes
        no arbitrary keys; missing phase markers never prove a complete demo cycle.
        DERBY fixed workload is a separate once-only row-12 launch with bounded
        raw collection and a private host-owned transport receipt.
        Their plans disclose ROM download-mode resets, writes and observation.
        Flash leaves verified new firmware installed; restore requires a separate
        explicit request. No automatic rollback, repeat write or HELLO is added.
        """

        def prepare() -> dict[str, Any]:
            if request.after is not None and request.test not in (
                TestName.BUILD,
                TestName.SOURCE,
            ):
                waiter = require(ctx, "control", honor_auth_required=False)
                return resident.prepare_deferred(request, waiter.principal_id, waiter.origin.value)
            if request.test in (TestName.BUILD, TestName.SOURCE):
                owner = require(ctx, "control", honor_auth_required=False)
                return resident.profile_jobs.prepare(
                    request, owner.principal_id, owner.origin.value
                )
            if request.test in (
                TestName.CAPTURE,
                TestName.DERBY,
                TestName.DERBY_FIXED,
                TestName.FIRMWARE,
                TestName.RESTART,
                TestName.BUILD,
                TestName.SOURCE,
                *FLASH_TESTS,
            ):
                # Always authenticated (even on a mock server), then the gated check.
                bind_lease(require(ctx, "control", honor_auth_required=False), request.session)
            bind_lease(require(ctx, "control", honor_auth_required=True), request.session)
            return resident.prepare(request)

        if request.test == TestName.SOURCE:
            try:

                def current_owner() -> tuple[str, str]:
                    owner = require(ctx, "control", honor_auth_required=False)
                    if "read" not in owner.scopes:
                        raise BrokerError("insufficient_scope")
                    return owner.principal_id, owner.origin.value

                owner_id, origin = current_owner()
                result = await resident.profile_jobs.prepare_source(
                    request, owner_id, origin, reauthorize=current_owner
                )
                return {"ok": True, **result}
            except ValueError as exc:
                return refusal(exc)
        return _result(prepare)

    @op
    async def execute_test(request: ExecuteRequest, ctx: Context[Any, Any]) -> dict[str, Any]:
        """Request execution of an exact plan; repeated requests never replay a job.

        Successful flash/restore automatically selects its verified recovery target
        only with the final successful-job commit, after write/readback/expected
        same-device boot/close/authorization. It never automatically restores.
        Failed/cancelled work retains the prior LKG. Firmware itself may change
        NVS/filesystem after boot; firmware recovery cannot guarantee data compatibility.

        Firmware backup probes/reads private full flash via ROM connections that
        can reset into download mode; it does not reset back into normal boot.
        ROM read limits are fixed by size: at least 300 s, then 60 s + 600 s/MiB,
        up to 9660 s for 16 MiB. No advancing progress for 60 s stops the job.
        Longer reads require explicit lease renewal; the broker never renews it.
        Flash/restore write only their exact verified images/backup, verify readback,
        then perform one 100 ms normal-boot RTS pulse and passive observation
        (up to 30 s). A disconnect allows at most one same-USB reopen after close,
        without another reset pulse. Failure stops for review without retry or
        automatic restore. HELLO remains a separate explicit request.

        Source/build execute within locally registered project settings without
        a USB lease. Execution deadlines are independent of
        connection lifetime. Cancellation, principal revocation/expiry or changed
        registration stops work; confirmed normal failure permits a new job.
        Same job ID never replays. Flash/restore authorization remains separate.

        An authenticated explicit request queues the exact prepared operation
        after current scope, ownership, device and firmware checks. No operator
        approval, keyword or arming step is required. Preparation alone never
        submits execution. Old pending approvals never resume as new execution.
        """

        def authorize() -> None:
            bind_lease(require(ctx, "control", honor_auth_required=True), request.session)

        async def execute() -> dict[str, Any]:
            plan = resident._store().plan(request.plan_hash)
            if plan is not None and plan.get("test") in (
                TestName.BUILD.value,
                TestName.SOURCE.value,
            ):
                owner = require(ctx, "control", honor_auth_required=False)
                return resident.profile_jobs.execute(
                    request, owner.principal_id, owner.origin.value
                )
            if plan is not None and plan.get("execution_context") == "device_deferred":
                waiter = require(ctx, "control", honor_auth_required=False)
                return resident.execute_deferred(request, waiter.principal_id, waiter.origin.value)
            authorize()
            # Firmware file verification runs off the loop; authority is checked
            # again before the final synchronous checks and the enqueue.
            return await resident.execute_async(request, reauthorize=authorize)

        try:
            return {"ok": True, **shared_job(await execute())}
        except ValueError as exc:
            return refusal(exc)

    @op
    async def request_home_recovery(
        request: RequestHomeRecovery, ctx: Context[Any, Any]
    ) -> dict[str, Any]:
        """Explicitly request one q after a proved-closed DERBY capture failure.

        Requires current authenticated control authority. Fifteen-second single-use
        authority; fresh APP_STOPPED/HOME_READY and confirmed close are required.
        No reset, flash, screenshot, reconnect, retry or general review bypass.
        """

        def submit() -> dict[str, Any]:
            owner = require(ctx, "control", honor_auth_required=False)
            return resident.home_recovery.request(request, owner.principal_id, owner.origin.value)

        return _result(submit)

    @op
    async def cancel_home_recovery(
        request: CancelHomeRecovery, ctx: Context[Any, Any]
    ) -> dict[str, Any]:
        """Stop this principal's exact recovery; retain ownership until confirmed close."""
        return _result(
            lambda: cancellation.cancel_job(
                resident,
                cancellation.HomeRecoveryJob(
                    request, require(ctx, "control", honor_auth_required=False).principal_id
                ),
            )
        )

    @op
    async def job_status(request: JobRequest, ctx: Context[Any, Any]) -> dict[str, Any]:
        """Read persisted job state after reconnecting. Interrupted work is never replayed."""

        def get_job() -> dict[str, Any]:
            job = resident.job(request.job_id)
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
                        (plan["source_job_id"], request.job_id),
                    )
                ]
            if plan is not None and plan.get("execution_context") == "registered_project":
                require(ctx, "read", honor_auth_required=False)
            return shared_job(job)

        def checked() -> dict[str, Any]:
            require(ctx, "read", honor_auth_required=True)
            return get_job()

        return _result(checked)

    @op
    async def read_build_logs(request: BuildLogRequest, ctx: Context[Any, Any]) -> dict[str, Any]:
        """Read bounded shared build output by job; any read-scoped principal may read it.

        Default warning/error plus progress, with diagnostic context; no path input.
        Source log polling can set include_source_cache=false to avoid a separate
        full filesystem inventory. Inventory failure never hides source job output.
        Cursor reads do not consume output. Expiry, restart and eviction report gaps.
        A successful build includes a revalidated durable manifest even after RAM log
        expiry/restart. expired/gap still report lost logs. artifact_status is
        verified, unavailable or busy. No manifest grants flash permission.
        manifest_export/manifest_export_sha256 give the versioned private-cache projection;
        new flash plans bind its hash alongside the unchanged original manifest hash.
        Flash summaries survive log expiry/restart and contain bounded matched IDF/ELF
        evidence. Full BIN readback hashes plus boot prefix support artifact matching;
        boot text alone is not authenticated or cryptographic identity proof.
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
                result = source.read_logs(request, reader, include_cache=False)
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
                return resident.firmware_flash.read_logs(request, reader)
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
                result = await asyncio.to_thread(
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
            return result

        try:
            return {"ok": True, **await get_logs()}
        except ValueError as exc:
            return refusal(exc)

    @op
    async def list_apps(ctx: Context[Any, Any]) -> dict[str, Any]:
        """Read the cached app-control catalog and freshness; never opens USB.

        An unobserved catalog is explicit. Device-reported identity is unattested.
        """

        def read_catalog() -> dict[str, Any]:
            require(ctx, "read", honor_auth_required=False)
            return resident.app_sessions.list_apps()

        return _result(read_catalog)

    @op
    async def app_command(request: AppCommandRequest, ctx: Context[Any, Any]) -> dict[str, Any]:
        """Queue typed input inside one exact app_session plan; status never replays.

        queued/sent/accepted/consumed differ. consumed is delivery to the owner
        action path, not JavaScript, rendering, saving or benchmark completion.
        """

        def command() -> dict[str, Any]:
            bind_lease(require(ctx, "control", honor_auth_required=False), request.session)
            return resident.app_command(request)

        return _result(command)

    @op
    async def read_app_events(request: AppEventsRequest, ctx: Context[Any, Any]) -> dict[str, Any]:
        """Read private bounded control ACKs and redacted observed_during_run logs.

        Independent cursors expose gaps/drops/expiry. Never opens another reader.
        """

        def events() -> dict[str, Any]:
            caller = require(ctx, "read", honor_auth_required=False)
            reader = job_reads.app_event_owner(resident, request.job_id)
            return resident.read_app_events(request, reader or caller.principal_id)

        return _result(events)

    @op
    async def read_device_logs(request: JobRequest, ctx: Context[Any, Any]) -> dict[str, Any]:
        """Read a completed firmware_observe or restart_and_observe report.

        Any principal with current read permission may read any job's report; it is
        the same bounded redacted body the job's owner gets.
        Known credential patterns are removed, not all secrets.
        Firmware identity is unverified observed boot text; ELF prefixes are not
        firmware binary hashes. Never opens USB, resets, retries or recaptures.
        """

        def get_report() -> dict[str, Any]:
            caller = require(ctx, "read", honor_auth_required=False)
            reader = job_reads.device_log_owner(resident, request.job_id)
            return resident.read_device_logs(request.job_id, reader or caller.principal_id)

        return _result(get_report)

    @op
    async def read_capture(request: JobRequest, ctx: Context[Any, Any]) -> CallToolResult:
        """Read a completed capture from five-minute memory cache; never opens USB.

        Any principal with current read permission may read it, whoever requested
        capture_home or derby_smoke. No separate image-delivery approval is required.
        Mock images say TEST / NOT DEVICE. Render-buffer images do not
        prove snapshot coherence or LCD transfer acknowledgement. No automatic recapture.
        """
        try:
            caller = require(ctx, "read", honor_auth_required=False)
            reader = job_reads.capture_owner(resident, request.job_id)
            captured = resident.read_capture(request.job_id, reader or caller.principal_id)
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

    @op
    async def cancel_job(request: CancelRequest, ctx: Context[Any, Any]) -> dict[str, Any]:
        """Cancel owned work. Source/build stops wait for confirmed worker/process exit.

        Source/build cancellation uses current control principal, no USB lease.
        Device ownership and interruption rules remain in force.
        """

        def cancel() -> dict[str, Any]:
            job = resident._store().job(request.job_id)
            plan = resident._store().plan(job["plan_hash"]) if job is not None else None
            if plan is not None and plan.get("test") in (
                TestName.BUILD.value,
                TestName.SOURCE.value,
            ):
                owner = require(ctx, "control", honor_auth_required=False)
                return cancellation.cancel_job(
                    resident,
                    cancellation.ProfileJob(request, owner.principal_id, owner.origin.value),
                )
            bind_lease(require(ctx, "control", honor_auth_required=True), request.session)
            return cancellation.cancel_job(resident, cancellation.LeasedDeviceJob(request))

        return _result(lambda: shared_job(cancel()))

    @op
    async def create_work_session(
        request: CreateWorkSessionRequest, ctx: Context[Any, Any]
    ) -> dict[str, Any]:
        """Create an authenticated logical workflow; does not reserve the device."""

        def create() -> dict[str, Any]:
            owner = require(ctx, "control", honor_auth_required=False)
            bind_origin(owner, request.owner)
            return resident.create_work_session(
                request, principal_id=owner.principal_id, scopes=owner.scopes
            )

        return _result(create)

    @op
    async def resume_work_session(
        request: ResumeWorkSessionRequest, ctx: Context[Any, Any]
    ) -> dict[str, Any]:
        """Resume your logical workflow; device access needs its separate physical lease."""

        def action() -> dict[str, Any]:
            # Logical sessions are always authenticated, including on a legacy mock server.
            owner = require(ctx, "read", honor_auth_required=False)
            return resident.resume_work_session(
                request, principal_id=owner.principal_id, scopes=owner.scopes
            )

        return _result(action)

    @op
    async def request_device(
        request: RequestDeviceRequest, ctx: Context[Any, Any]
    ) -> dict[str, Any]:
        """Request a bounded FIFO device lease for your authenticated logical workflow."""

        def action() -> dict[str, Any]:
            # Logical sessions are always authenticated, including on a legacy mock server.
            owner = require(ctx, "control", honor_auth_required=False)
            return resident.request_device(
                request, principal_id=owner.principal_id, scopes=owner.scopes
            )

        return _result(action)

    @op
    async def cancel_device_request(
        request: CancelDeviceRequest, ctx: Context[Any, Any]
    ) -> dict[str, Any]:
        """Cancel your pending device request without interrupting an active job."""

        def action() -> dict[str, Any]:
            # Logical sessions are always authenticated, including on a legacy mock server.
            owner = require(ctx, "control", honor_auth_required=False)
            return cancellation.cancel_job(
                resident,
                cancellation.DeviceRequestWait(request, owner.principal_id, owner.scopes),
            )

        return _result(action)

    @op
    async def close_work_session(
        request: CloseWorkSessionRequest, ctx: Context[Any, Any]
    ) -> dict[str, Any]:
        """Close your logical workflow safely; cannot stop an active physical job."""

        def action() -> dict[str, Any]:
            # Logical sessions are always authenticated, including on a legacy mock server.
            owner = require(ctx, "control", honor_auth_required=False)
            return resident.close_work_session(
                request, principal_id=owner.principal_id, scopes=owner.scopes
            )

        return _result(action)

    register_surface(
        server,
        ops=ops,
        resident=resident,
        require=require,
        auth_required=auth_required,
        read_only=read_only,
        mutating=mutating,
        destructive=execution,
    )
    return server


def create_app(
    settings: Settings,
    broker: Broker | None = None,
    *,
    serial_enabled: bool = False,
    require_auth: bool = False,
    auth_source: str = "environment",
    auth: AuthState | None = None,
    key_registry_path: Path | None = None,
) -> ASGIApp:
    ingress_enabled = settings.tunnel is not None and settings.tunnel.ingress_enabled
    auth_required = settings.backend == "serial" or require_auth or key_registry_path is not None
    if ingress_enabled and not auth_required:
        raise ValueError("tunnel_ingress_requires_authenticated_broker")
    holder: list[AuthState | None] = [auth]
    registry_holder: list[KeyRegistry | LiveKeyRegistry | None] = [None]
    resident = broker or Broker(settings, serial_enabled=serial_enabled)
    server = create_server(
        settings,
        resident,
        serial_enabled=serial_enabled,
        require_auth=require_auth,
        auth_source=auth_source,
        auth_holder=holder,
        key_registry_path=key_registry_path,
        key_registry_holder=registry_holder,
    )
    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[f"127.0.0.1:{settings.port}", f"localhost:{settings.port}"],
        allowed_origins=[f"http://127.0.0.1:{settings.port}", f"http://localhost:{settings.port}"],
    )
    app = server.streamable_http_app(
        stateless_http=True,
        json_response=True,
        max_request_body_size=16384,
        host=settings.host,
        transport_security=security,
    )
    authenticator = RequestAuth(holder, registry_holder)
    diagnostics = HTTPDiagnostics()
    operator_app = OperatorAPI(
        app, resident, authenticator.authenticate, diagnostics=diagnostics.snapshot
    )
    guarded = LoopbackOnly(
        operator_app,
        settings.port,
        (lambda: authenticator) if auth_required else None,
        diagnostics=diagnostics,
    )
    if ingress_enabled:
        return TunnelIngress(guarded, authenticator.authenticate)
    return guarded
