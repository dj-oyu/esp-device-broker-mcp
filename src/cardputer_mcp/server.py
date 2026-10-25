"""Official MCP SDK v2, one lifespan-owned broker, Streamable HTTP only."""

import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from math import isfinite
from pathlib import Path
from time import time
from typing import Any, Literal, Protocol, overload

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import (
    CallToolResult,
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
from .input_diagnostics import invalid_arguments, validate_arguments
from .key_registry import KeyRegistry, KeyRegistryError
from .key_registry import Scope as KeyScope
from .models import Origin
from .operator_api import OperatorAPI
from .registry_defaults import LiveKeyRegistry, default_key_registry_path
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

    def same_owner(self, recorded: str, current: str) -> bool:
        """Whether `current` may use inputs recorded under `recorded`.

        Identical principals always match. Otherwise only two registry keys
        linked by an explicit local rotation share an owner; ephemeral
        principals and freshly issued keys never inherit another principal's
        inputs. Lineage grants no authority: the caller still authenticates
        `current` and checks its scope. Failures return False.
        """
        if type(recorded) is not str or type(current) is not str:
            return False
        if recorded == current:
            return True
        if not (recorded.startswith("key:") and current.startswith("key:")):
            return False
        registry = self.registry[0]
        if registry is None:
            return False
        try:
            return registry.same_owner(recorded[4:], current[4:]) is True
        except (KeyRegistryError, ValueError, OSError):
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
    resident.principal_lineage = authenticator.same_owner
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
            "Esc can exit another app. Any caller with current read scope can read a "
            "succeeded capture; it is kept in RAM for five minutes and saved in state "
            "until the oldest are pruned beyond 200 images or 64 MiB. "
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
    register_surface(
        server,
        resident=resident,
        require=require,
        auth_required=auth_required,
        tunnel_ingress_enabled=settings.tunnel is not None and settings.tunnel.ingress_enabled,
        read_only=read_only,
        mutating=mutating,
        destructive=execution,
    )
    return server


def end_operator_streams(app: object) -> None:
    """End open operator event streams before uvicorn waits for open responses.

    Uvicorn's graceful shutdown waits for every in-flight response before the
    lifespan (and so the broker) closes, and a live stream never completes on
    its own. Only the wrappers built by ``create_app`` are followed.
    """
    current: object | None = app
    for _ in range(4):
        if isinstance(current, OperatorAPI):
            current.live.close()
            return
        current = getattr(current, "guarded", None) or getattr(current, "app", None)


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
