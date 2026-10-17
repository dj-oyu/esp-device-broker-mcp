"""Dedicated ingress isolation with synthetic credentials and mock-only loopback HTTP.

No Tunnel client, cloud service, account, USB device or firmware is contacted.
The wire envelopes exercise the installed official MCP SDK server; they are not
evidence of end-to-end compatibility with an actual Tunnel deployment.
"""

import asyncio
import copy
import itertools
import json
import socket
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any
from unittest.mock import Mock

import httpx2
import pytest
import uvicorn
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.types import (
    CLIENT_CAPABILITIES_META_KEY,
    CLIENT_INFO_META_KEY,
    PROTOCOL_VERSION_META_KEY,
)
from mcp_types.version import HANDSHAKE_PROTOCOL_VERSIONS, LATEST_MODERN_VERSION
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from cardputer_mcp import server as server_module
from cardputer_mcp.auth import AuthState
from cardputer_mcp.backend import MockBackend
from cardputer_mcp.broker import Broker
from cardputer_mcp.config import Settings, TunnelMetadata
from cardputer_mcp.http_diagnostics import DenialReason, HTTPDiagnostics
from cardputer_mcp.key_registry import IssuedKey, KeyRegistry
from cardputer_mcp.models import Origin
from cardputer_mcp.server import LoopbackOnly, Principal, RequestAuth, TunnelIngress, create_app

LOCAL = "a" * 64
REMOTE = "b" * 64
MARKER = "SYNTHETIC_PRIVATE_INGRESS_INPUT"
CLIENT_INFO = {"name": "synthetic-ingress-contract", "version": "0.0.0"}
PROXY_HEADERS = [
    (b"Forwarded", f"for={MARKER};host=public.invalid;proto=https".encode()),
    (b"X-Forwarded-For", b"198.51.100.7"),
    (
        b"X-Forwarded-Client-Cert",
        b'By=proxy;Subject="CN=' + MARKER.encode() + b'";URI=spiffe://synthetic.invalid/admin',
    ),
    (b"X-Forwarded-Host", b"public.invalid"),
    (b"X-Forwarded-Proto", b"https"),
    (b"X-Forwarded-Port", b"443"),
]


def raw_scope(**changes: Any) -> Scope:
    scope: Scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "POST",
        "path": "/tunnel/mcp",
        "raw_path": b"/tunnel/mcp",
        "root_path": "",
        "app_root_path": "",
        "query_string": b"",
        "scheme": "http",
        "server": ("127.0.0.1", 8765),
        "client": ("127.0.0.1", 23456),
        "headers": [
            (b"Host", b"127.0.0.1:8765"),
            (b"Content-Type", b"application/json"),
            (b"Authorization", f"Bearer {REMOTE}".encode()),
            *PROXY_HEADERS,
        ],
    }
    scope.update(changes)
    return scope


async def invoke(app: ASGIApp, scope: Scope) -> list[Message]:
    messages: list[Message] = []

    async def receive() -> Message:
        pytest.fail("Rejected ingress must not consume a request body")

    async def send(message: Message) -> None:
        messages.append(message)

    await app(scope, receive, send)
    return messages


async def unreachable(scope: Scope, receive: Receive, send: Send) -> None:
    pytest.fail("Rejected ingress reached the downstream application")


def assert_denied(
    messages: list[Message], diagnostics: HTTPDiagnostics, status: int, reason: DenialReason
) -> None:
    assert messages[0] == {
        "type": "http.response.start",
        "status": status,
        "headers": [(b"content-type", b"application/json")],
    }
    assert len(messages) == 2
    snapshot = diagnostics.snapshot()
    assert snapshot["total_denials"] == 1
    assert snapshot["counts"][reason.value] == 1
    assert sum(snapshot["counts"].values()) == 1
    assert snapshot["last_denial"]["reason"] == reason.value
    assert snapshot["last_denial"]["route"] == "mcp"
    assert snapshot["last_denial"]["http_status"] == status
    for secret in (MARKER, LOCAL, REMOTE, "198.51.100.7", "public.invalid"):
        assert secret not in repr(messages) + json.dumps(snapshot)


@pytest.mark.parametrize(
    "change,reason",
    [
        ({"extra": [(b"x-private", b"\xff")]}, DenialReason.NON_ASCII_HEADERS),
        ({"extra": [(b"x-\xff", b"value")]}, DenialReason.NON_ASCII_HEADERS),
        ({"extra": [(b"HOST", b"public.invalid")]}, DenialReason.DUPLICATE_HEADERS),
        ({"extra": [(b"authorization", b"Bearer invalid")]}, DenialReason.DUPLICATE_HEADERS),
        *[
            ({"extra": [(name.lower(), b"duplicate")]}, DenialReason.DUPLICATE_HEADERS)
            for name, _ in PROXY_HEADERS
        ],
        ({"client": None}, DenialReason.NON_LOOPBACK_PEER),
        ({"client": ("198.51.100.7", 1)}, DenialReason.NON_LOOPBACK_PEER),
        ({"client": ("::ffff:127.0.0.1", 1)}, DenialReason.NON_LOOPBACK_PEER),
        ({"host": b"public.invalid"}, DenialReason.HOST_NOT_ALLOWED),
        ({"host": b"127.0.0.1:9999"}, DenialReason.HOST_NOT_ALLOWED),
        ({"extra": [(b"Origin", b"https://public.invalid")]}, DenialReason.ORIGIN_NOT_ALLOWED),
        ({"extra": [(b"Origin", b"null")]}, DenialReason.ORIGIN_NOT_ALLOWED),
        *[
            ({"extra": [(name, b"anything")]}, DenialReason.FORWARDING_HEADER_PRESENT)
            for name in (b"X-Forwarded-Prefix", b"x-forwarded-certificate", b"X-Forwarded-")
        ],
        ({"content_type": b"text/plain"}, DenialReason.CONTENT_TYPE_NOT_JSON),
        ({"content_type": b"Application/JSON"}, DenialReason.CONTENT_TYPE_NOT_JSON),
    ],
)
async def test_raw_guards_run_before_authentication_or_stripping(change, reason):
    scope = raw_scope()
    for name, value in change.items():
        if name == "extra":
            scope["headers"].extend(value)
        elif name in ("host", "content_type"):
            header = b"Host" if name == "host" else b"Content-Type"
            scope["headers"] = [(k, value if k == header else v) for k, v in scope["headers"]]
        else:
            scope[name] = value
    original = copy.deepcopy(scope)
    diagnostics = HTTPDiagnostics()
    authenticate = Mock(side_effect=AssertionError("Authentication preceded a raw guard"))
    guarded = LoopbackOnly(unreachable, 8765, diagnostics=diagnostics)
    messages = await invoke(TunnelIngress(guarded, authenticate), scope)
    assert_denied(messages, diagnostics, 403, reason)
    assert messages[1]["body"] == b'{"error":"local_only"}'
    authenticate.assert_not_called()
    assert scope == original


@pytest.mark.parametrize(
    "changes",
    [
        {"raw_path": b"/tunnel/%6dcp"},
        {"raw_path": b"/tunnel%2fmcp"},
        {"raw_path": b"/tunnel/mcp/"},
        {"raw_path": b"/tunnel/mcp/../operator/state"},
        {"raw_path": b"/operator/state"},
        {"raw_path": b"/TUNNEL/mcp"},
        {"raw_path": b""},
        {"root_path": "/operator"},
        {"app_root_path": "/operator"},
        {"query_string": f"target=/operator/state&private={MARKER}".encode()},
    ],
)
async def test_only_canonical_unprefixed_queryless_transport_alias_is_accepted(changes):
    scope = raw_scope(**changes)
    original = copy.deepcopy(scope)
    authenticate = Mock(side_effect=AssertionError("Invalid route reached authentication"))
    diagnostics = HTTPDiagnostics()
    messages = await invoke(
        TunnelIngress(LoopbackOnly(unreachable, 8765, diagnostics=diagnostics), authenticate),
        scope,
    )
    assert_denied(messages, diagnostics, 400, DenialReason.TUNNEL_ROUTE_REJECTED)
    assert messages[1]["body"] == b'{"error":"invalid_tunnel_route"}'
    authenticate.assert_not_called()
    assert scope == original


@pytest.mark.parametrize("method", ["POST", "GET", "DELETE"])
@pytest.mark.parametrize("include_raw_path", [False, True])
async def test_exact_allowed_headers_stripped_only_after_remote_read_and_scope_is_copied(
    method, include_raw_path
):
    scope = raw_scope(method=method, client=("::1", 123))
    scope["headers"].extend(
        [
            (b"MCP-Protocol-Version", LATEST_MODERN_VERSION.encode()),
            (b"Mcp-Method", b"tools/list"),
            (b"Mcp-Session-Id", MARKER.encode()),
            (b"Mcp-Name", b"device_status"),
            (b"X-Request-Id", MARKER.encode()),
            (b"X-Custom", b"preserve"),
            (b"Origin", b"http://127.0.0.1:8765"),
        ]
    )
    if not include_raw_path:
        del scope["raw_path"]
    original = copy.deepcopy(scope)
    principal = Principal(MARKER, Origin.REMOTE, frozenset({"read"}))
    authentication: list[str] = []

    def outer(header):
        assert scope == original
        authentication.append("outer")
        assert header == f"Bearer {REMOTE}"
        return principal

    class InnerAuth:
        def authenticate(self, header):
            authentication.append("inner")
            assert header == f"Bearer {REMOTE}"
            return principal

    request_message = {"type": "http.request", "body": MARKER.encode(), "more_body": False}
    messages: list[Message] = []

    async def receive():
        return request_message

    async def send(message):
        messages.append(message)

    async def downstream(normalized, downstream_receive, downstream_send):
        assert authentication == ["outer", "inner"]
        assert normalized is not scope
        assert normalized["headers"] is not scope["headers"]
        expected = copy.deepcopy(original)
        expected.update(path="/mcp", raw_path=b"/mcp")
        expected["cardputer.diagnostic_tunnel_ingress"] = True
        removed = {name.lower() for name, _ in PROXY_HEADERS}
        expected["headers"] = [(k, v) for k, v in original["headers"] if k.lower() not in removed]
        assert normalized == expected
        assert downstream_receive is receive
        assert await downstream_receive() is request_message
        await downstream_send({"type": "http.response.start", "status": 200, "headers": []})
        await downstream_send({"type": "http.response.body", "body": b"ok"})

    diagnostics = HTTPDiagnostics()
    guarded = LoopbackOnly(downstream, 8765, lambda: InnerAuth(), diagnostics)
    await TunnelIngress(guarded, outer)(scope, receive, send)
    assert messages[-1]["body"] == b"ok"
    assert diagnostics.snapshot()["total_denials"] == 0
    assert scope == original


@pytest.mark.parametrize(
    "principal,status,reason",
    [
        (None, 401, DenialReason.AUTHENTICATION_REQUIRED),
        (
            Principal(MARKER, Origin.LOCAL, frozenset({"read", "control"})),
            403,
            DenialReason.TUNNEL_PRINCIPAL_REJECTED,
        ),
        (
            Principal(MARKER, Origin.REMOTE, frozenset()),
            403,
            DenialReason.TUNNEL_PRINCIPAL_REJECTED,
        ),
        (
            Principal(MARKER, Origin.REMOTE, frozenset({"control"})),
            403,
            DenialReason.TUNNEL_PRINCIPAL_REJECTED,
        ),
    ],
)
async def test_missing_local_or_readless_principal_never_reaches_inner_guard(
    principal, status, reason
):
    inner = Mock(side_effect=AssertionError("Ineligible principal reached inner guard"))
    diagnostics = HTTPDiagnostics()
    scope = raw_scope()
    original = copy.deepcopy(scope)
    guarded = LoopbackOnly(unreachable, 8765, inner, diagnostics)
    messages = await invoke(TunnelIngress(guarded, Mock(return_value=principal)), scope)
    assert_denied(messages, diagnostics, status, reason)
    inner.assert_not_called()
    assert scope == original


@pytest.mark.parametrize("invalidation", ["expired", "revoked", "wrong-secret"])
async def test_registry_requires_current_remote_key_at_every_ingress_request(
    tmp_path, invalidation
):
    now = [1000.0]
    sequence = itertools.count(1)
    registry = KeyRegistry(
        tmp_path / "keys.sqlite3",
        clock=lambda: now[0],
        random_bytes=lambda size: next(sequence).to_bytes(size, "big"),
    )
    try:
        issued = registry.issue("synthetic", Origin.REMOTE, ttl_seconds=1)
        authenticator = RequestAuth([None], [registry])
        header = "Bearer " + issued.token
        assert authenticator.authenticate(header) is not None
        if invalidation == "expired":
            now[0] += 1
        elif invalidation == "revoked":
            registry.revoke(issued.metadata.key_id)
        else:
            header = header[:-1] + ("f" if header[-1] != "f" else "e")
        scope = raw_scope()
        scope["headers"] = [
            (k, header.encode() if k == b"Authorization" else v) for k, v in scope["headers"]
        ]
        diagnostics = HTTPDiagnostics()
        messages = await invoke(
            TunnelIngress(
                LoopbackOnly(unreachable, 8765, diagnostics=diagnostics), authenticator.authenticate
            ),
            scope,
        )
        assert_denied(messages, diagnostics, 401, DenialReason.AUTHENTICATION_REQUIRED)
        visible = repr(messages) + json.dumps(diagnostics.snapshot())
        assert issued.token not in visible
        assert issued.metadata.key_id not in visible
        assert issued.metadata.principal_id not in visible
    finally:
        registry.close()


async def test_revocation_between_outer_gate_and_inner_auth_is_not_cached(tmp_path):
    sequence = itertools.count(1)
    registry = KeyRegistry(
        tmp_path / "keys.sqlite3",
        clock=lambda: 1000.0,
        random_bytes=lambda size: next(sequence).to_bytes(size, "big"),
    )
    try:
        issued = registry.issue("synthetic", Origin.REMOTE)
        authenticator = RequestAuth([None], [registry])
        calls = []

        def outer(header):
            principal = authenticator.authenticate(header)
            assert principal is not None
            calls.append("outer")
            assert registry.revoke(issued.metadata.key_id)
            return principal

        class InnerAuth:
            def authenticate(self, header):
                calls.append("inner")
                return authenticator.authenticate(header)

        scope = raw_scope()
        scope["headers"] = [
            (k, ("Bearer " + issued.token).encode() if k == b"Authorization" else v)
            for k, v in scope["headers"]
        ]
        original = copy.deepcopy(scope)
        diagnostics = HTTPDiagnostics()
        guarded = LoopbackOnly(unreachable, 8765, lambda: InnerAuth(), diagnostics)
        messages = await invoke(TunnelIngress(guarded, outer), scope)
        assert_denied(messages, diagnostics, 401, DenialReason.AUTHENTICATION_REQUIRED)
        assert calls == ["outer", "inner"]
        assert scope == original
        assert issued.token not in repr(messages) + json.dumps(diagnostics.snapshot())
    finally:
        registry.close()


@pytest.mark.parametrize(
    "path",
    [
        "/mcp",
        "/mcp/",
        "/operator/state",
        "/operator/http-diagnostics",
        "/tunnel/operator/state",
        "/tunnel/mcp/",
        "/tunnel/mcp/operator/state",
        "/prefix/tunnel/mcp",
        "/TUNNEL/mcp",
    ],
)
async def test_other_paths_retain_original_forwarding_guard(path):
    diagnostics = HTTPDiagnostics()
    outer_auth = Mock(side_effect=AssertionError("Non-alias used ingress authentication"))
    scope = raw_scope(path=path, raw_path=path.encode())
    original = copy.deepcopy(scope)
    messages = await invoke(
        TunnelIngress(LoopbackOnly(unreachable, 8765, diagnostics=diagnostics), outer_auth), scope
    )
    assert messages[0]["status"] == 403
    assert messages[1]["body"] == b'{"error":"local_only"}'
    assert diagnostics.snapshot()["last_denial"]["reason"] == "forwarding_header_present"
    outer_auth.assert_not_called()
    assert scope == original


def test_enabled_ingress_requires_real_authentication_configuration(tmp_path):
    settings = Settings(state_dir=tmp_path / "state", tunnel=TunnelMetadata(ingress_enabled=True))
    # Supplying tokens alone does not activate the inner request/tool auth boundary.
    with pytest.raises(ValueError, match="^tunnel_ingress_requires_authenticated_broker$"):
        create_app(settings, auth=AuthState({Origin.LOCAL: LOCAL, Origin.REMOTE: REMOTE}))
    assert not settings.state_dir.exists()


@pytest.mark.parametrize("tunnel", [None, TunnelMetadata()])
def test_default_configuration_does_not_install_ingress(tunnel, tmp_path):
    settings = Settings(state_dir=tmp_path / "state", tunnel=tunnel)
    app = create_app(
        settings, require_auth=True, auth=AuthState({Origin.LOCAL: LOCAL, Origin.REMOTE: REMOTE})
    )
    assert isinstance(app, LoopbackOnly)
    assert not isinstance(app, TunnelIngress)


def wire_request(method, *, params=None, token=REMOTE, version=LATEST_MODERN_VERSION, request_id=1):
    params = dict(params or {})
    headers = {
        "Accept": "application/json, text/event-stream",
        "Authorization": f"Bearer {token}",
        "MCP-Protocol-Version": version,
    }
    if version not in HANDSHAKE_PROTOCOL_VERSIONS:
        params["_meta"] = {
            PROTOCOL_VERSION_META_KEY: version,
            CLIENT_INFO_META_KEY: CLIENT_INFO,
            CLIENT_CAPABILITIES_META_KEY: {},
        }
        headers["Mcp-Method"] = method
        if method == "tools/call":
            headers["Mcp-Name"] = params["name"]
    return {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}, headers


@dataclass
class IngressService:
    http: httpx2.AsyncClient
    broker: Broker
    backend: MockBackend
    keys: dict[str, IssuedKey]
    registry: KeyRegistry
    now: list[float]

    async def post(self, route, body, headers):
        if route == "/tunnel/mcp":
            headers = {**headers, **{k.decode(): v.decode() for k, v in PROXY_HEADERS}}
        return await self.http.post(route, json=body, headers=headers)

    async def tool(self, route, name, request=None, *, token=REMOTE):
        response = await self.post(
            route,
            *wire_request(
                "tools/call",
                token=token,
                params={"name": name, "arguments": {} if request is None else {"request": request}},
            ),
        )
        assert response.status_code == 200, response.text
        result = response.json()["result"]
        assert not result.get("isError", False), result
        return result["structuredContent"]


@asynccontextmanager
async def serve_ingress(tmp_path, monkeypatch, *, enabled=True) -> AsyncIterator[IngressService]:
    now = [1000.0]
    sequence = itertools.count(1)
    registry_path = tmp_path / "keys.sqlite3"
    registry = KeyRegistry(
        registry_path,
        clock=lambda: now[0],
        random_bytes=lambda size: next(sequence).to_bytes(size, "big"),
    )
    keys = {
        "first": registry.issue("First", Origin.REMOTE, scopes={"control"}, ttl_seconds=10),
        "second": registry.issue("Second", Origin.REMOTE, scopes={"control"}),
        "reader": registry.issue("Reader", Origin.REMOTE),
    }
    monkeypatch.setattr(
        server_module,
        "KeyRegistry",
        lambda path, *, create: KeyRegistry(path, clock=lambda: now[0], create=create),
    )
    monkeypatch.setattr(server_module, "time", lambda: now[0])
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        settings = Settings(
            state_dir=tmp_path / "state",
            port=port,
            mock_stage_seconds=0.001,
            tunnel=TunnelMetadata(ingress_enabled=enabled),
        )
        assert settings.allow_mock_execution is False
        backend = MockBackend(0.001)
        broker = Broker(settings, backend=backend, lock_path=tmp_path / "device.lockfile")
        app = create_app(
            settings,
            broker,
            require_auth=True,
            auth=AuthState({Origin.LOCAL: LOCAL, Origin.REMOTE: REMOTE}),
            key_registry_path=registry_path,
        )
        server = uvicorn.Server(
            uvicorn.Config(
                app,
                host="127.0.0.1",
                port=port,
                log_level="critical",
                access_log=False,
                proxy_headers=False,
            )
        )
        task = asyncio.create_task(server.serve(sockets=[listener]))
        try:
            async with asyncio.timeout(5):
                while not server.started:
                    if task.done():
                        await task
                        pytest.fail("Synthetic ingress server stopped before readiness")
                    await asyncio.sleep(0.01)
            async with httpx2.AsyncClient(
                base_url=f"http://127.0.0.1:{port}", trust_env=False, timeout=5
            ) as http:
                yield IngressService(http, broker, backend, keys, registry, now)
        finally:
            server.should_exit = True
            await asyncio.wait_for(task, timeout=5)
            registry.close()
        assert backend.calls == 0
        assert not (settings.state_dir / "runtime-auth.json").exists()


@pytest.fixture
async def ingress_service(tmp_path, monkeypatch):
    async with serve_ingress(tmp_path, monkeypatch) as service:
        yield service


@pytest.mark.parametrize("route", ["/mcp", "/tunnel/mcp"])
async def test_modern_discovery_and_fixed_status_through_real_http(ingress_service, route):
    service = ingress_service
    for method in ("server/discover", "tools/list"):
        response = await service.post(route, *wire_request(method))
        assert response.status_code == 200
        assert "result" in response.json()
        assert "mcp-session-id" not in response.headers
        if method == "tools/list":
            names = {tool["name"] for tool in response.json()["result"]["tools"]}
            assert {"device_status", "prepare_test", "execute_test", "create_work_session"} <= names
            assert "approve_plan" not in names
    status = await service.tool(route, "device_status")
    assert status["backend"] == "mock"
    assert status["hardware_connected"] is False
    assert status["approval_policy"] == "local_operator_per_plan"
    assert status["owner"] is None and status["active_job"] is None


@pytest.mark.parametrize("version", sorted(HANDSHAKE_PROTOCOL_VERSIONS))
async def test_legacy_initialize_and_discovery_through_ingress(ingress_service, version):
    service = ingress_service
    body, headers = wire_request(
        "initialize",
        version=version,
        params={"protocolVersion": version, "capabilities": {}, "clientInfo": CLIENT_INFO},
    )
    initialized = await service.post("/tunnel/mcp", body, headers)
    assert initialized.status_code == 200
    assert initialized.json()["result"]["protocolVersion"] == version
    assert "mcp-session-id" not in initialized.headers
    acknowledged = await service.post(
        "/tunnel/mcp", {"jsonrpc": "2.0", "method": "notifications/initialized"}, headers
    )
    assert acknowledged.status_code == 202
    listed = await service.post("/tunnel/mcp", *wire_request("tools/list", version=version))
    assert listed.status_code == 200
    assert len(listed.json()["result"]["tools"]) == 20
    assert "read_build_logs" in {tool["name"] for tool in listed.json()["result"]["tools"]}
    assert "read_capture" in {tool["name"] for tool in listed.json()["result"]["tools"]}


async def test_official_sdk_client_can_discover_through_ingress(ingress_service):
    headers = {
        "Authorization": f"Bearer {REMOTE}",
        **{k.decode(): v.decode() for k, v in PROXY_HEADERS},
    }
    async with httpx2.AsyncClient(headers=headers, trust_env=False, timeout=5) as http:
        url = str(ingress_service.http.base_url).rstrip("/") + "/tunnel/mcp"
        async with Client(streamable_http_client(url, http_client=http)) as client:
            tools = await client.list_tools()
            assert "device_status" in {tool.name for tool in tools.tools}
            result = await client.call_tool("device_status", {})
            assert result.structured_content["backend"] == "mock"


@pytest.mark.parametrize("method", ["GET", "DELETE", "PUT", "PATCH", "HEAD", "OPTIONS"])
async def test_http_methods_retain_sdk_transport_status(ingress_service, method):
    service = ingress_service
    _, headers = wire_request("tools/list")
    headers["Mcp-Session-Id"] = MARKER
    direct = await service.http.request(method, "/mcp", headers=headers)
    headers.update({k.decode(): v.decode() for k, v in PROXY_HEADERS})
    tunneled = await service.http.request(method, "/tunnel/mcp", headers=headers)
    assert tunneled.status_code == direct.status_code == 405
    if method == "HEAD":
        assert tunneled.content == direct.content == b""
    else:
        assert tunneled.json() == direct.json() == {"error": "request_rejected"}
    assert MARKER not in tunneled.text + str(tunneled.headers)


@pytest.mark.parametrize("token,status", [(LOCAL, 403), ("c" * 64, 401), ("", 401)])
async def test_remote_only_bearer_gate_over_real_http(ingress_service, token, status):
    body, headers = wire_request("tools/list", token=token)
    if not token:
        headers.pop("Authorization")
    response = await ingress_service.post("/tunnel/mcp", body, headers)
    assert response.status_code == status
    if token:
        assert token not in response.text


@pytest.mark.parametrize("invalidation", ["revoke", "expire"])
async def test_registry_invalidation_is_immediate_over_real_http(ingress_service, invalidation):
    service = ingress_service
    key = service.keys["first"]
    assert (await service.tool("/tunnel/mcp", "device_status", token=key.token))["ok"]
    if invalidation == "revoke":
        assert service.registry.revoke(key.metadata.key_id)
    else:
        service.now[0] += 10
    denied = await service.post("/tunnel/mcp", *wire_request("tools/list", token=key.token))
    assert denied.status_code == 401
    assert denied.json() == {"error": "authentication_required"}


async def test_read_only_ingress_key_keeps_control_scope_restriction(ingress_service):
    service = ingress_service
    token = service.keys["reader"].token
    assert (await service.tool("/tunnel/mcp", "device_status", token=token))["ok"]
    denied = await service.tool(
        "/tunnel/mcp", "acquire_session", {"owner": "remote_mcp"}, token=token
    )
    assert denied == {"ok": False, "error": "insufficient_scope"}
    assert service.broker.session is None


async def test_same_broker_lease_job_and_plan_approval_boundaries(ingress_service):
    service = ingress_service
    first, second = service.keys["first"].token, service.keys["second"].token
    actors = [("/mcp", first), ("/tunnel/mcp", second)]
    leases = await asyncio.gather(
        *[
            service.tool(route, "acquire_session", {"owner": "remote_mcp"}, token=token)
            for route, token in actors
        ]
    )
    assert sum(item["ok"] for item in leases) == 1
    winner = next(i for i, item in enumerate(leases) if item["ok"])
    route, token = actors[winner]
    other_route, other_token = actors[1 - winner]
    assert leases[1 - winner] == {"ok": False, "error": "device_busy"}
    session = leases[winner]["session"]
    # A principal's existing lease works through either transport path.
    assert (await service.tool(other_route, "renew_session", {"session": session}, token=token))[
        "ok"
    ]
    prepared = await service.tool(
        route, "prepare_test", {"session": session, "job_id": "synthetic-ingress"}, token=token
    )
    assert prepared["approval_required"] is True
    digest = prepared["plan_hash"]
    for name, fields in {
        "renew_session": {},
        "release_session": {},
        "prepare_test": {"job_id": "synthetic-theft"},
        "execute_test": {"plan_hash": digest},
        "cancel_job": {"job_id": "synthetic-ingress"},
    }.items():
        denied = await service.tool(
            other_route, name, {"session": session, **fields}, token=other_token
        )
        assert denied == {"ok": False, "error": "authenticated_owner_mismatch"}
    for path in ("/mcp", "/tunnel/mcp"):
        denied = await service.tool(
            path, "execute_test", {"session": session, "plan_hash": digest}, token=token
        )
        assert denied == {"ok": False, "error": "operator_approval_required"}
        job = await service.tool(path, "job_status", {"job_id": "synthetic-ingress"}, token=token)
        assert job == {"ok": False, "error": "job_not_found"}
    assert service.backend.calls == 0 and service.broker.queue.empty()
    assert (await service.tool(other_route, "release_session", {"session": session}, token=token))[
        "ok"
    ]


async def test_work_session_ownership_survives_route_change(ingress_service):
    service = ingress_service
    owner, stranger = service.keys["first"].token, service.keys["second"].token
    created = await service.tool(
        "/tunnel/mcp", "create_work_session", {"owner": "remote_mcp"}, token=owner
    )
    resume = created["resume_token"]
    resumed = await service.tool(
        "/mcp", "resume_work_session", {"resume_token": resume}, token=owner
    )
    assert resumed["ok"] and resumed["lease"] is None
    for tool in ("resume_work_session", "request_device", "close_work_session"):
        denied = await service.tool("/mcp", tool, {"resume_token": resume}, token=stranger)
        assert not denied["ok"]
    granted = await service.tool("/mcp", "request_device", {"resume_token": resume}, token=owner)
    leased = await service.tool(
        "/tunnel/mcp", "resume_work_session", {"resume_token": resume}, token=owner
    )
    assert leased["lease"]["session"] == granted["lease"]["session"]
    assert (
        await service.tool(
            "/tunnel/mcp", "close_work_session", {"resume_token": resume}, token=owner
        )
    )["ok"]
    assert service.broker.session is None


async def test_ingress_cannot_reach_operator_or_supply_approval(ingress_service):
    service = ingress_service
    body, headers = wire_request("tools/list")
    forwarded = {**headers, **{k.decode(): v.decode() for k, v in PROXY_HEADERS}}
    for path in (
        "/operator/state",
        "/operator/http-diagnostics",
        "/tunnel/operator/state",
        "/tunnel/mcp/../operator/state",
        "/tunnel/mcp/%2e%2e/operator/state",
        "/tunnel/mcp%2f..%2foperator/state",
        "/tunnel/%6dcp",
    ):
        response = await service.http.post(path, json=body, headers=forwarded)
        assert response.status_code in (400, 403, 404)
        assert "waiters" not in response.text
    # The inner operator API still independently requires a local operator.
    denied = await service.http.get(
        "/operator/state", headers={"Authorization": f"Bearer {REMOTE}"}
    )
    assert denied.status_code == 403
    local = await service.http.get("/operator/state", headers={"Authorization": f"Bearer {LOCAL}"})
    assert local.status_code == 200
    for name, arguments in (
        ("approve_plan", {"plan_hash": "c" * 64}),
        (
            "execute_test",
            {"request": {"session": "c" * 64, "plan_hash": "d" * 64, "approved": True}},
        ),
    ):
        rejected = await service.post(
            "/tunnel/mcp",
            *wire_request("tools/call", params={"name": name, "arguments": arguments}),
        )
        assert rejected.status_code == 200
        assert rejected.json()["result"]["isError"] is True


async def test_default_disabled_route_and_direct_forwarding_remain_closed(tmp_path, monkeypatch):
    async with serve_ingress(tmp_path, monkeypatch, enabled=False) as service:
        body, headers = wire_request("tools/list")
        assert (await service.http.post("/mcp", json=body, headers=headers)).status_code == 200
        alias = await service.http.post("/tunnel/mcp", json=body, headers=headers)
        assert alias.status_code == 404
        for path in ("/mcp", "/tunnel/mcp", "/operator/state"):
            headers["X-Forwarded-For"] = "127.0.0.1"
            denied = await service.http.post(path, json=body, headers=headers)
            assert denied.status_code == 403
            assert denied.json() == {"error": "local_only"}


async def test_no_body_header_query_token_or_principal_leaks_in_errors_or_diagnostics(
    ingress_service, caplog
):
    service = ingress_service
    key = service.keys["reader"]
    body, headers = wire_request("tools/list", token=key.token)
    headers.update({"Mcp-Session-Id": MARKER, "X-Request-Id": MARKER})
    body["method"] = {"private": MARKER}
    malformed = await service.post("/tunnel/mcp", body, headers)
    assert malformed.status_code == 400
    assert malformed.json() == {"error": "request_rejected"}
    headers["X-Forwarded-Secret"] = MARKER
    guarded = await service.post("/tunnel/mcp", body, headers)
    assert guarded.status_code == 403
    diagnostic = await service.http.get(
        "/operator/http-diagnostics", headers={"Authorization": f"Bearer {LOCAL}"}
    )
    assert diagnostic.status_code == 200
    assert diagnostic.json()["total_denials"] == 2
    assert diagnostic.json()["last_denial"]["reason"] == "forwarding_header_present"
    status = await service.tool("/tunnel/mcp", "device_status")
    logs = await service.tool("/tunnel/mcp", "read_logs", {})
    persisted = "\n".join(service.broker._store().db.iterdump())
    visible = malformed.text + guarded.text + str(malformed.headers) + str(guarded.headers)
    visible += diagnostic.text + json.dumps([status, logs]) + caplog.text + persisted
    for secret in (
        MARKER,
        LOCAL,
        REMOTE,
        key.token,
        key.metadata.key_id,
        key.metadata.principal_id,
    ):
        assert secret not in visible


@pytest.mark.parametrize("token,status", [(None, 401), (LOCAL, 403), ("c" * 64, 401)])
async def test_xfcc_claims_cannot_authenticate_or_select_operator(ingress_service, token, status):
    service = ingress_service
    body, headers = wire_request("tools/list")
    if token is None:
        headers.pop("Authorization")
    else:
        headers["Authorization"] = "Bearer " + token
    headers["X-Forwarded-Client-Cert"] = 'Subject="CN=local_cli";URI=' + MARKER
    response = await service.http.post("/tunnel/mcp", json=body, headers=headers)
    assert response.status_code == status
    assert MARKER not in response.text
    assert service.backend.calls == 0


async def test_xfcc_is_discarded_only_on_authenticated_alias(ingress_service):
    service = ingress_service
    body, headers = wire_request("tools/call", params={"name": "client_identity", "arguments": {}})
    headers["X-Forwarded-Client-Cert"] = 'Subject="CN=local_cli";URI=' + MARKER
    alias = await service.http.post("/tunnel/mcp", json=body, headers=headers)
    assert alias.status_code == 200
    identity = alias.json()["result"]["structuredContent"]
    assert identity["origin"] == "remote_mcp"
    assert identity["principal_id"] == "ephemeral:remote_mcp"
    assert MARKER not in alias.text
    direct = await service.http.post("/mcp", json=body, headers=headers)
    assert direct.status_code == 403
    headers["Authorization"] = "Bearer " + LOCAL
    operator = await service.http.get("/operator/state", headers=headers)
    assert operator.status_code == 403
    assert service.backend.calls == 0


async def test_status_reports_enabled_ingress_without_claiming_cloud_connection(ingress_service):
    service = ingress_service
    for route in ("/mcp", "/tunnel/mcp"):
        status = await service.tool(route, "device_status")
        assert status["tunnel_ingress_enabled"] is True
        assert status["tunnel"] == "external_lifecycle_unknown"
        assert status["hardware_connected"] is False
