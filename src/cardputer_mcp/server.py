"""Official MCP SDK v2, one lifespan-owned broker, Streamable HTTP only."""

import base64
import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
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
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .auth import AuthState
from .broker import Broker, BrokerError
from .config import Settings
from .http_diagnostics import (
    INGRESS_CONTEXT_KEY,
    DenialReason,
    HTTPDiagnostics,
    request_context,
    route_category,
)
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
            return await super().call_tool(name, arguments, context)
        except Exception:
            return CallToolResult(
                content=[TextContent(type="text", text="invalid_tool_request")],
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
    from .capture_policy import CapturePolicies

    resident.capture_policies = CapturePolicies(resident, authenticator.list_principals)
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
        request: LeaseRequest, ctx: Context[Any, Any], action: Callable[[], dict[str, Any]]
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
            "authenticated clients and exact local operator approval. Acquire, prepare, then "
            "execute_test with wait_for_approval=true to submit before approval. Local GUI "
            "approval starts only explicitly submitted work without another client request. "
            "Preparation alone does not request execution. Capture requires physical HOME "
            "attestation; Esc can exit another app. "
            "Images require local preview/send approval or an explicit operator key policy. "
            "Read permitted captures within five minutes using the same identity. "
            "Pixels do not prove LCD transfer. "
            "No arbitrary shell/raw serial commands are available. Flash requires "
            "registered device/build/backup and exact local approval. "
            "Restart requires an exact locally approved plan."
        ),
        log_level="WARNING",
    )
    read_only = ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=False)
    mutating = ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=False)
    execution = ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=False)

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

        Backup, flash and restore are independent exact-approval operations.
        Their plans disclose ROM download-mode resets, writes and observation.
        Flash leaves verified new firmware installed; restore requires a separate
        explicit request. No automatic rollback, repeat write or HELLO is added.
        """

        def prepare() -> dict[str, Any]:
            if request.test in (
                TestName.CAPTURE,
                TestName.FIRMWARE,
                TestName.RESTART,
                TestName.BUILD,
                *FLASH_TESTS,
            ):
                owner = principal(ctx, "control")
                resident.require_owner(
                    request.session, owner.origin.value, principal_id=owner.principal_id
                )
            return owned(request, ctx, lambda: resident.prepare(request))

        return _result(prepare)

    @server.tool(annotations=execution)
    async def execute_test(request: ExecuteRequest, ctx: Context[Any, Any]) -> dict[str, Any]:
        """Request execution of an exact plan; repeated requests never replay a job.

        Firmware backup probes/reads private full flash via ROM connections that
        can reset into download mode; it does not reset back into normal boot.
        Flash/restore write only their approved images/backup, verify readback,
        then perform one 100 ms normal-boot RTS pulse and passive observation
        (up to 30 s). A disconnect allows at most one same-USB reopen after close,
        without another reset pulse. Failure stops for review without retry or
        automatic restore. HELLO remains a separate approved request.

        Set wait_for_approval=true to submit before manual approval. When approval
        is still required it returns awaiting_approval immediately; local GUI
        approval then queues that exact submitted job without another client call.
        An already valid permit can queue immediately. Waiting ends with the original
        lease deadline, cancellation, revocation or broker restart. Preparation
        alone never submits execution. False preserves approve-then-execute mode.
        """
        return _result(lambda: owned(request, ctx, lambda: resident.execute(request)))

    @server.tool(annotations=read_only)
    async def job_status(request: JobRequest, ctx: Context[Any, Any]) -> dict[str, Any]:
        """Read persisted job state after reconnecting. Interrupted work is never replayed."""
        return _result(lambda: read(ctx, lambda: resident.job(request.job_id)))

    @server.tool(annotations=read_only)
    async def read_build_logs(request: BuildLogRequest, ctx: Context[Any, Any]) -> dict[str, Any]:
        """Read bounded shared build output by job and current authenticated principal.

        Default warning/error plus progress, with diagnostic context; no path input.
        Cursor reads do not consume output. Expiry, restart and eviction report gaps.
        A successful build includes a hashed manifest; it grants no flash permission.
        """

        def get_logs() -> dict[str, Any]:
            owner = principal(ctx, "read")
            store = resident._store()
            job = store.job(request.job_id)
            plan = store.plan(job["plan_hash"]) if job is not None else None
            if plan is not None and plan.get("test") in {t.value for t in FLASH_TESTS}:
                if plan.get("flash_principal") != owner.principal_id:
                    raise BrokerError("flash_log_not_available")
                return resident.firmware_flash.read_logs(request, owner.principal_id)
            if (
                plan is None
                or plan.get("test") != TestName.BUILD.value
                or plan.get("build_principal") != owner.principal_id
            ):
                raise BrokerError("build_log_not_available")
            if request.job_id not in resident.firmware_build.logs:
                return dict(
                    entries=[],
                    expired=True,
                    gap=True,
                    has_more=False,
                    reason="not_started_or_history_not_retained",
                    next_cursor=dict(epoch=resident.epoch, sequence=0),
                )
            try:
                return resident.firmware_build.read_logs(request, owner.principal_id)
            except ValueError as exc:
                raise BrokerError(str(exc)) from None

        return _result(get_logs)

    @server.tool(annotations=read_only)
    async def read_device_logs(request: JobRequest, ctx: Context[Any, Any]) -> dict[str, Any]:
        """Read your completed firmware_observe or restart_and_observe report from RAM.

        Requires the same authenticated principal and current read permission. The
        exact local plan approval permits delivery of bounded redacted device logs
        to that principal. Known credential patterns are removed, not all secrets.
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

        Requires the same authenticated principal that requested capture_home, current
        read permission, and local image approval or a currently valid image-delivery policy.
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
        """Cancel your awaiting-approval or queued job; never interrupt running work."""
        return _result(lambda: owned(request, ctx, lambda: resident.cancel(request)))

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
