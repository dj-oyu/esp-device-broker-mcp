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
from typing import Any, Protocol

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

from .auth import AuthState
from .broker import Broker, BrokerError
from .config import Settings
from .derby_reports import binding_unchanged, private_job, shared_job
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
    TestName,
)
from .operator_api import OperatorAPI
from .source_preflight import settings_identity, source_usage
from .source_snapshot import SourceError


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
        self, ephemeral: list[AuthState | None], registry: list[KeyRegistry | None]
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

    def list_principals(self) -> list[dict[str, Any]]:
        """Local policy options expose current key metadata, never bearer secrets."""
        result: list[dict[str, Any]] = []
        registry = self.registry[0]
        if registry is not None:
            for key in registry.list_keys():
                result.append(
                    {
                        "principal_id": key.principal_id,
                        "key_id": key.key_id,
                        "origin": key.origin.value,
                        "scopes": sorted(key.scopes),
                        "label": key.label,
                        "issued_at": key.issued_at,
                        "expires_at": key.expires_at,
                        "revoked_at": key.revoked_at,
                        "kind": "issued",
                    }
                )
        if self.ephemeral[0] is not None:
            for origin in Origin:
                result.append(
                    {
                        "principal_id": f"ephemeral:{origin.value}",
                        "origin": origin.value,
                        "scopes": ["read", "control"],
                        "label": "Shared ephemeral identity",
                        "issued_at": 0.0,
                        "expires_at": None,
                        "revoked_at": None,
                        "kind": "ephemeral",
                    }
                )
        return result

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
    except BrokerError as exc:
        return {"ok": False, "error": str(exc)}
    except SourceError as exc:
        return {"ok": False, "error": exc.code}
    except ValueError:
        return {"ok": False, "error": "invalid_request"}


def create_server(
    settings: Settings,
    broker: Broker | None = None,
    *,
    serial_enabled: bool = False,
    require_auth: bool = False,
    auth_source: str = "environment",
    auth_holder: list[AuthState | None] | None = None,
    key_registry_path: Path | None = None,
    key_registry_holder: list[KeyRegistry | None] | None = None,
) -> MCPServer[Any]:
    if settings.backend == "serial" and not serial_enabled:
        raise BrokerError("serial_runtime_opt_in_required")
    if auth_source not in ("environment", "file"):
        raise BrokerError("unsupported_auth_source")
    auth_required = settings.backend == "serial" or require_auth or key_registry_path is not None
    holder: list[AuthState | None] = auth_holder if auth_holder is not None else [None]
    registry_holder: list[KeyRegistry | None] = (
        key_registry_holder if key_registry_holder is not None else [None]
    )
    authenticator = RequestAuth(holder, registry_holder)
    resident = broker or Broker(settings, serial_enabled=serial_enabled)
    resident.principal_valid = authenticator.principal_valid
    runtime_path = settings.state_dir / "runtime-auth.json"

    def principal(ctx: Context[Any, Any], scope: KeyScope) -> Principal:
        headers = ctx.headers
        owner = authenticator.authenticate(headers.get("authorization") if headers else None)
        if owner is None:
            raise BrokerError("authentication_required")
        if scope not in owner.scopes:
            raise BrokerError("insufficient_scope")
        return owner

    def read(ctx: Context[Any, Any], action: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        if auth_required:
            principal(ctx, "read")
        return action()

    def owned(
        request: LeaseRequest | PrepareRequest | ExecuteRequest | CancelRequest,
        ctx: Context[Any, Any],
        action: Callable[[], dict[str, Any]],
    ) -> dict[str, Any]:
        if auth_required:
            owner = principal(ctx, "control")
            resident.require_owner(
                request.session, owner.origin.value, principal_id=owner.principal_id
            )
        return action()

    def acquire_owned(request: AcquireRequest, ctx: Context[Any, Any]) -> dict[str, Any]:
        if auth_required:
            owner = principal(ctx, "control")
            if request.owner != owner.origin:
                raise BrokerError("authenticated_owner_mismatch")
            return resident.acquire(request, principal_id=owner.principal_id)
        return resident.acquire(request)

    def work_action(
        ctx: Context[Any, Any],
        scope: KeyScope,
        action: Callable[[Principal], dict[str, Any]],
    ) -> dict[str, Any]:
        # Logical sessions are always authenticated, including on a legacy mock server.
        return action(principal(ctx, scope))

    def create_work_owned(
        request: CreateWorkSessionRequest, ctx: Context[Any, Any]
    ) -> dict[str, Any]:
        owner = principal(ctx, "control")
        if request.owner != owner.origin:
            raise BrokerError("authenticated_owner_mismatch")
        return resident.create_work_session(
            request, principal_id=owner.principal_id, scopes=owner.scopes
        )

    @asynccontextmanager
    async def lifespan(_: MCPServer[Any]) -> AsyncIterator[None]:
        await resident.start()
        try:
            if key_registry_path is not None:
                registry_holder[0] = KeyRegistry(key_registry_path, create=False)
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
        version="0.12.0",
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

    def registered_ids(registrations: dict[str, Any]) -> list[str]:
        ids = []
        for key in registrations:
            try:
                ids.append(identifier.validate_python(key, strict=True))
            except ValidationError:
                # Legacy local config can contain names that no MCP request accepts.
                continue
        return sorted(ids)

    @server.tool(annotations=read_only)
    async def client_identity(ctx: Context[Any, Any]) -> dict[str, Any]:
        """Inspect this request's principal/scopes; never return credentials or capabilities."""

        def identify() -> dict[str, Any]:
            owner = principal(ctx, "read")
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

    @server.tool(annotations=read_only)
    async def list_registered_targets(ctx: Context[Any, Any]) -> dict[str, Any]:
        """List current addressable registered IDs with read scope; no hardware access.

        Pass no arguments. Use project_id for source/build and device_id for
        backup/flash/restore. Registration does not establish operation eligibility
        or a physical connection. Paths, selectors and credentials stay private.
        Names outside the request ID format (1–64 letters/digits/underscore/hyphen)
        are omitted; they cannot be used in prepare_test.
        """

        def targets() -> dict[str, Any]:
            principal(ctx, "read")
            current = resident.settings
            return {
                "build_projects": [
                    {"project_id": project_id}
                    for project_id in registered_ids(current.build_projects)
                ],
                "flash_devices": [
                    {"device_id": device_id} for device_id in registered_ids(current.flash_devices)
                ],
            }

        return _result(targets)

    @server.tool(annotations=read_only)
    async def device_status(ctx: Context[Any, Any]) -> dict[str, Any]:
        """Read broker state without opening hardware. Never returns session capabilities."""
        return _result(lambda: read(ctx, resident.status))

    @server.tool(annotations=read_only)
    async def read_logs(request: LogRequest, ctx: Context[Any, Any]) -> dict[str, Any]:
        """Read bounded cached events using a cursor; does not acquire or read a serial port."""
        return _result(lambda: read(ctx, lambda: resident.read_logs(request)))

    @server.tool(annotations=mutating)
    async def acquire_session(request: AcquireRequest, ctx: Context[Any, Any]) -> dict[str, Any]:
        """Reserve a workflow; authenticated mode binds ownership to the client identity."""
        return _result(lambda: acquire_owned(request, ctx))

    @server.tool(annotations=mutating)
    async def renew_session(request: RenewRequest, ctx: Context[Any, Any]) -> dict[str, Any]:
        """Extend a live lease using its in-memory bearer capability."""
        return _result(lambda: owned(request, ctx, lambda: resident.renew(request)))

    @server.tool(annotations=mutating)
    async def release_session(request: LeaseRequest, ctx: Context[Any, Any]) -> dict[str, Any]:
        """Release an idle session. Active work cannot be cancelled or released this way."""
        return _result(lambda: owned(request, ctx, lambda: resident.release(request)))

    @server.tool(annotations=mutating)
    async def prepare_test(request: PrepareRequest, ctx: Context[Any, Any]) -> dict[str, Any]:
        """Prepare an immutable plan; serial preflight enumerates without opening USB.

        Source fetch is an explicit registered-project/full-commit operation.
        It returns a principal-bound receipt; build can select it without worktree sync.
        Registered source/build plans require current control authorization and
        no physical lease. Omit session for these jobs.
        Registered build scripts run as the local OS user; this is not a sandbox.
        Source fetch never builds or flashes. No caller-supplied URL/path/shell is accepted.
        firmware_flash requires request.job_id, test=firmware_flash, a live owned
        request.session, registered request.device_id and successful owned
        request.build_job_id. Do not pass project_id: the registered device selects
        its project. backup_job_id and lkg_receipt are mutually exclusive; verified
        retained LKG may be used when both are omitted. Missing recovery is rejected.
        firmware_backup requires session/device_id, with no build/recovery inputs.
        firmware_restore requires session/device_id and verified recovery, with no
        build_job_id or project_id. Explicit null optional references are allowed.
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
            if request.test in (TestName.BUILD, TestName.SOURCE):
                owner = principal(ctx, "control")
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
                owner = principal(ctx, "control")
                resident.require_owner(
                    request.session, owner.origin.value, principal_id=owner.principal_id
                )
            return owned(request, ctx, lambda: resident.prepare(request))

        if request.test == TestName.SOURCE:
            try:

                def current_owner() -> tuple[str, str]:
                    owner = principal(ctx, "control")
                    if "read" not in owner.scopes:
                        raise BrokerError("insufficient_scope")
                    return owner.principal_id, owner.origin.value

                owner_id, origin = current_owner()
                result = await resident.profile_jobs.prepare_source(
                    request, owner_id, origin, reauthorize=current_owner
                )
                return {"ok": True, **result}
            except BrokerError as exc:
                return {"ok": False, "error": str(exc)}
            except SourceError as exc:
                return {"ok": False, "error": exc.code}
            except ValueError:
                return {"ok": False, "error": "invalid_request"}
        return _result(prepare)

    @server.tool(annotations=execution)
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

        def execute() -> dict[str, Any]:
            plan = resident._store().plan(request.plan_hash)
            if plan is not None and plan.get("test") in (
                TestName.BUILD.value,
                TestName.SOURCE.value,
            ):
                owner = principal(ctx, "control")
                return resident.profile_jobs.execute(
                    request, owner.principal_id, owner.origin.value
                )
            return owned(request, ctx, lambda: resident.execute(request))

        return _result(lambda: shared_job(execute()))

    @server.tool(annotations=mutating)
    async def request_home_recovery(
        request: RequestHomeRecovery, ctx: Context[Any, Any]
    ) -> dict[str, Any]:
        """Explicitly request one q after a proved-closed DERBY capture failure.

        Requires current authenticated control authority. Fifteen-second single-use
        authority; fresh APP_STOPPED/HOME_READY and confirmed close are required.
        No reset, flash, screenshot, reconnect, retry or general review bypass.
        """

        def submit() -> dict[str, Any]:
            owner = principal(ctx, "control")
            return resident.home_recovery.request(request, owner.principal_id, owner.origin.value)

        return _result(submit)

    @server.tool(annotations=mutating)
    async def cancel_home_recovery(
        request: CancelHomeRecovery, ctx: Context[Any, Any]
    ) -> dict[str, Any]:
        """Stop this principal's exact recovery; retain ownership until confirmed close."""
        return _result(
            lambda: resident.home_recovery.cancel(request, principal(ctx, "control").principal_id)
        )

    @server.tool(annotations=read_only)
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
                    owner = principal(ctx, "read")
                except BrokerError:
                    if auth_required:
                        raise
                    return shared_job(job)
                epoch = resident.epoch
                result = private_job(store, job, plan, owner.principal_id)
                if "derby_diagnostics" in result or "fixed_trial" in result:
                    try:
                        current = principal(ctx, "read")
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
            if plan is not None and plan.get("operation") == "home_recovery":
                owner = principal(ctx, "read")
                if plan["principal_id"] != owner.principal_id:
                    raise BrokerError("owned_home_recovery_required")
                job["recovery_audit"] = [
                    dict(row)
                    for row in resident._store().db.execute(
                        "SELECT sequence,event,created FROM home_recovery_audit "
                        "WHERE source_job=? AND recovery_job IN ('',?) ORDER BY sequence LIMIT 64",
                        (plan["source_job_id"], request.job_id),
                    )
                ]
            if plan is not None and plan.get("execution_context") == "registered_project":
                owner = principal(ctx, "read")
                if plan.get("principal_id") != owner.principal_id:
                    raise BrokerError("owned_registered_project_job_required")
            return shared_job(job)

        return _result(lambda: read(ctx, get_job))

    @server.tool(annotations=read_only)
    async def read_build_logs(request: BuildLogRequest, ctx: Context[Any, Any]) -> dict[str, Any]:
        """Read bounded shared build output by job and current authenticated principal.

        Default warning/error plus progress, with diagnostic context; no path input.
        Cursor reads do not consume output. Expiry, restart and eviction report gaps.
        A successful build includes a revalidated durable manifest even after RAM log
        expiry/restart. expired/gap still report lost logs. artifact_status is
        verified, unavailable or busy. No manifest grants flash permission.
        Flash summaries survive log expiry/restart and contain bounded matched IDF/ELF
        evidence. Full BIN readback hashes plus boot prefix support artifact matching;
        boot text alone is not authenticated or cryptographic identity proof.
        """

        async def get_logs() -> dict[str, Any]:
            owner = principal(ctx, "read")
            store = resident._store()
            job = store.job(request.job_id)
            plan = store.plan(job["plan_hash"]) if job is not None else None
            if plan is not None and job is not None and plan.get("test") == TestName.SOURCE.value:
                if plan.get("source_principal") != owner.principal_id:
                    raise BrokerError("source_log_not_available")
                source = resident.firmware_source
                epoch, settings = resident.epoch, resident.settings
                captured = settings.model_copy(deep=True)
                identity = settings_identity(captured)
                result = source.read_logs(request, owner.principal_id, include_cache=False)
                try:
                    usage: dict[str, Any] = await resident.profile_jobs.source_reads.run(
                        partial(source_usage, captured, plan["project_id"])
                    )
                except SourceError as exc:
                    if "diagnostic" not in result:
                        raise
                    usage = {"available": False, "code": exc.code}
                current_owner = principal(ctx, "read")
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
                result = source.read_logs(request, owner.principal_id, include_cache=False)
                result["source_cache"] = usage
                return result
            if plan is not None and plan.get("test") in {t.value for t in FLASH_TESTS}:
                if plan.get("flash_principal") != owner.principal_id:
                    raise BrokerError("flash_log_not_available")
                return resident.firmware_flash.read_logs(request, owner.principal_id)
            if (
                plan is None
                or plan.get("test") != TestName.BUILD.value
                or plan.get("build_principal") != owner.principal_id
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
                    owner.principal_id,
                    project_id=project_id,
                    broker_epoch=epoch,
                )
            except ValueError as exc:
                raise BrokerError(str(exc)) from None
            # Hash IO awaited another thread. Recheck the current read grant and
            # the exact broker/job/registration before returning private receipts.
            current_owner = principal(ctx, "read")
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
        except BrokerError as exc:
            return {"ok": False, "error": str(exc)}
        except SourceError as exc:
            return {"ok": False, "error": exc.code}
        except ValueError:
            return {"ok": False, "error": "invalid_request"}

    @server.tool(annotations=read_only)
    async def read_device_logs(request: JobRequest, ctx: Context[Any, Any]) -> dict[str, Any]:
        """Read your completed firmware_observe or restart_and_observe report from RAM.

        Requires the same authenticated principal and current read permission. The
        explicit read returns bounded redacted device logs to that principal.
        Known credential patterns are removed, not all secrets.
        Firmware identity is unverified observed boot text; ELF prefixes are not
        firmware binary hashes. Never opens USB, resets, retries or recaptures.
        """

        def get_report() -> dict[str, Any]:
            owner = principal(ctx, "read")
            return resident.read_device_logs(request.job_id, owner.principal_id)

        return _result(get_report)

    @server.tool(annotations=read_only)
    async def read_capture(request: JobRequest, ctx: Context[Any, Any]) -> CallToolResult:
        """Read your completed capture from five-minute memory cache; never opens USB.

        Requires the same principal that requested capture_home or derby_smoke, current
        read permission. No separate image-delivery approval is required.
        Mock images say TEST / NOT DEVICE. Render-buffer images do not
        prove snapshot coherence or LCD transfer acknowledgement. No automatic recapture.
        """
        try:
            owner = principal(ctx, "read")
            captured = resident.read_capture(request.job_id, owner.principal_id)
        except BrokerError as exc:
            body = {"ok": False, "error": str(exc)}
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

    @server.tool(annotations=mutating)
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
                owner = principal(ctx, "control")
                return resident.profile_jobs.cancel(request, owner.principal_id, owner.origin.value)
            return owned(request, ctx, lambda: resident.cancel(request))

        return _result(lambda: shared_job(cancel()))

    @server.tool(annotations=mutating)
    async def create_work_session(
        request: CreateWorkSessionRequest, ctx: Context[Any, Any]
    ) -> dict[str, Any]:
        """Create an authenticated logical workflow; does not reserve the device."""
        return _result(lambda: create_work_owned(request, ctx))

    @server.tool(annotations=read_only)
    async def resume_work_session(
        request: ResumeWorkSessionRequest, ctx: Context[Any, Any]
    ) -> dict[str, Any]:
        """Resume your logical workflow; device access needs its separate physical lease."""
        return _result(
            lambda: work_action(
                ctx,
                "read",
                lambda owner: resident.resume_work_session(
                    request, principal_id=owner.principal_id, scopes=owner.scopes
                ),
            )
        )

    @server.tool(annotations=mutating)
    async def request_device(
        request: RequestDeviceRequest, ctx: Context[Any, Any]
    ) -> dict[str, Any]:
        """Request a bounded FIFO device lease for your authenticated logical workflow."""
        return _result(
            lambda: work_action(
                ctx,
                "control",
                lambda owner: resident.request_device(
                    request, principal_id=owner.principal_id, scopes=owner.scopes
                ),
            )
        )

    @server.tool(annotations=mutating)
    async def cancel_device_request(
        request: CancelDeviceRequest, ctx: Context[Any, Any]
    ) -> dict[str, Any]:
        """Cancel your pending device request without interrupting an active job."""
        return _result(
            lambda: work_action(
                ctx,
                "control",
                lambda owner: resident.cancel_device_request(
                    request, principal_id=owner.principal_id, scopes=owner.scopes
                ),
            )
        )

    @server.tool(annotations=mutating)
    async def close_work_session(
        request: CloseWorkSessionRequest, ctx: Context[Any, Any]
    ) -> dict[str, Any]:
        """Close your logical workflow safely; cannot stop an active physical job."""
        return _result(
            lambda: work_action(
                ctx,
                "control",
                lambda owner: resident.close_work_session(
                    request, principal_id=owner.principal_id, scopes=owner.scopes
                ),
            )
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
    registry_holder: list[KeyRegistry | None] = [None]
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
