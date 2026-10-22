"""Offline HTTP contracts for a future official tunnel-client integration.

These tests use the actual MCP 2.2 HTTP app over loopback TCP and a mock device.
The tiny relay models only HTTP request forwarding and the v0.0.15 documented
case-insensitive precedence: connector-forwarded headers override static MCP
headers. It is NOT the official binary, a tunnel-service/cloud test, OAuth
verification, a deployment recipe, or proof of end-to-end Tunnel compatibility.
No real tokens, user payloads, USB, firmware, or external network are used.

Contract references (not downloaded or contacted while tests run):
https://github.com/openai/tunnel-client/blob/v0.0.15/docs/connectors.md
https://github.com/openai/tunnel-client/blob/v0.0.15/docs/configuration.md
"""

import asyncio
import json
import socket
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx2
import pytest
import uvicorn
from mcp.types import (
    CLIENT_CAPABILITIES_META_KEY,
    CLIENT_INFO_META_KEY,
    PROTOCOL_VERSION_META_KEY,
)
from mcp_types.version import HANDSHAKE_PROTOCOL_VERSIONS, LATEST_MODERN_VERSION
from starlette.types import Receive, Scope, Send

from cardputer_mcp.auth import AuthState
from cardputer_mcp.backend import MockBackend
from cardputer_mcp.broker import Broker
from cardputer_mcp.config import Settings
from cardputer_mcp.models import Origin
from cardputer_mcp.server import create_app

LOCAL = "a" * 64
REMOTE = "b" * 64
MODERN = LATEST_MODERN_VERSION
CLIENT_INFO = {"name": "synthetic-tunnel-contract", "version": "0.0.0"}
EXPECTED_TOOLS = {
    "client_identity",
    "list_registered_targets",
    "device_status",
    "read_logs",
    "acquire_session",
    "renew_session",
    "release_session",
    "prepare_test",
    "execute_test",
    "job_status",
    "read_capture",
    "read_device_logs",
    "read_build_logs",
    "read_source_resources",
    "cancel_job",
    "request_home_recovery",
    "cancel_home_recovery",
    "create_work_session",
    "resume_work_session",
    "request_device",
    "cancel_device_request",
    "close_work_session",
    "list_apps",
    "app_command",
    "read_app_events",
}


def wire_request(
    method: str,
    *,
    params: dict[str, Any] | None = None,
    token: str = REMOTE,
    version: str = MODERN,
    request_id: int = 1,
) -> tuple[dict[str, Any], dict[str, str]]:
    """Build valid SDK wire envelopes, including modern routing-header mirrors."""
    body_params = dict(params or {})
    headers = {
        "Accept": "application/json, text/event-stream",
        "Authorization": f"Bearer {token}",
        "MCP-Protocol-Version": version,
    }
    if version not in HANDSHAKE_PROTOCOL_VERSIONS:
        body_params["_meta"] = {
            PROTOCOL_VERSION_META_KEY: version,
            CLIENT_INFO_META_KEY: CLIENT_INFO,
            CLIENT_CAPABILITIES_META_KEY: {},
        }
        headers["Mcp-Method"] = method
        if method == "tools/call":
            headers["Mcp-Name"] = body_params["name"]
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": method,
        "params": body_params,
    }, headers


@dataclass
class MockRelay:
    """HTTP contract simulation only; does not implement any control-plane API."""

    http: httpx2.AsyncClient

    async def post(
        self,
        body: dict[str, Any],
        forwarded: Mapping[str, str],
        static: Mapping[str, str] | None = None,
    ) -> httpx2.Response:
        # v0.0.15 configuration.md: forwarded headers apply last, ignoring case.
        headers = {name.lower(): value for name, value in (static or {}).items()}
        headers.update({name.lower(): value for name, value in forwarded.items()})
        return await self.http.post("/mcp", json=body, headers=headers)


@dataclass
class ContractService:
    http: httpx2.AsyncClient
    relay: MockRelay
    broker: Broker
    backend: MockBackend
    received_versions: list[str | None]

    async def post(
        self, route: str, body: dict[str, Any], headers: dict[str, str]
    ) -> httpx2.Response:
        if route == "direct":
            return await self.http.post("/mcp", json=body, headers=headers)
        assert route == "relay"
        return await self.relay.post(body, headers)

    async def tool(
        self,
        route: str,
        name: str,
        request: dict[str, Any] | None = None,
        *,
        token: str = REMOTE,
        version: str = MODERN,
    ) -> dict[str, Any]:
        body, headers = wire_request(
            "tools/call",
            params={"name": name, "arguments": {} if request is None else {"request": request}},
            token=token,
            version=version,
        )
        response = await self.post(route, body, headers)
        assert response.status_code == 200
        envelope = response.json()
        assert envelope["id"] == body["id"]
        result = envelope["result"]
        assert result.get("isError", False) is False
        assert isinstance(result["structuredContent"], dict)
        return result["structuredContent"]


@pytest.fixture
async def tunnel_contract(tmp_path: Path) -> AsyncIterator[ContractService]:
    # Keep the socket reserved throughout startup; no free-port race.
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        settings = Settings(state_dir=tmp_path / "state", port=port, mock_stage_seconds=0.001)
        backend = MockBackend(0.001)
        broker = Broker(settings, backend=backend, lock_path=tmp_path / "device.lockfile")
        app = create_app(
            settings,
            broker,
            auth=AuthState({Origin.LOCAL: LOCAL, Origin.REMOTE: REMOTE}),
            require_auth=True,
        )
        received_versions: list[str | None] = []

        async def observed_app(scope: Scope, receive: Receive, send: Send) -> None:
            if scope["type"] == "http":
                # Record only a protocol revision, never bodies or credentials.
                headers = dict(scope["headers"])
                version = headers.get(b"mcp-protocol-version")
                received_versions.append(version.decode("ascii") if version else None)
            await app(scope, receive, send)

        server = uvicorn.Server(
            uvicorn.Config(
                observed_app,
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
                        pytest.fail("Synthetic HTTP server stopped before readiness")
                    await asyncio.sleep(0.01)
            async with httpx2.AsyncClient(
                base_url=f"http://127.0.0.1:{port}", trust_env=False, timeout=5
            ) as http:
                yield ContractService(http, MockRelay(http), broker, backend, received_versions)
        finally:
            if backend.gate is not None:
                backend.gate.set()
            server.should_exit = True
            await asyncio.wait_for(task, timeout=5)


@pytest.mark.parametrize("route", ["direct", "relay"])
async def test_modern_self_contained_discovery_and_fixed_status(tunnel_contract, route):
    service = tunnel_contract
    # Modern requests have their own complete envelope and require no initialize.
    for method in ("server/discover", "tools/list"):
        body, headers = wire_request(method)
        response = await service.post(route, body, headers)
        assert response.status_code == 200
        assert response.json()["id"] == body["id"]
        assert "result" in response.json()
        if method == "tools/list":
            assert {tool["name"] for tool in response.json()["result"]["tools"]} == EXPECTED_TOOLS
        assert "mcp-session-id" not in response.headers
    status = await service.tool(route, "device_status")
    assert status["ok"] is True
    assert status["backend"] == "mock"
    assert status["device"] == "mock-cardputer"
    assert status["hardware_connected"] is False
    assert status["owner"] is None and status["active_job"] is None
    assert status["execution_policy"] == "authenticated_explicit_request"
    assert status["tunnel"] == "external_lifecycle_unknown"
    assert service.received_versions == [MODERN, MODERN, MODERN]
    assert service.backend.calls == 0
    assert not (service.broker.settings.state_dir / "runtime-auth.json").exists()


@pytest.mark.parametrize("route", ["direct", "relay"])
@pytest.mark.parametrize(
    "version",
    [
        pytest.param(
            version,
            marks=pytest.mark.skipif(
                version not in HANDSHAKE_PROTOCOL_VERSIONS,
                reason="Installed MCP SDK does not negotiate this legacy revision",
            ),
        )
        for version in ("2025-03-26", "2025-06-18", "2025-11-25")
    ],
)
async def test_legacy_initialize_negotiation_and_header_preservation(
    tunnel_contract, route, version
):
    service = tunnel_contract
    body, headers = wire_request(
        "initialize",
        params={"protocolVersion": version, "capabilities": {}, "clientInfo": CLIENT_INFO},
        version=version,
    )
    initialized = await service.post(route, body, headers)
    assert initialized.status_code == 200
    assert initialized.json()["result"]["protocolVersion"] == version
    # This broker deliberately uses stateless HTTP, also for legacy handshakes.
    assert "mcp-session-id" not in initialized.headers
    notice = {"jsonrpc": "2.0", "method": "notifications/initialized"}
    acknowledged = await service.post(route, notice, headers)
    assert acknowledged.status_code == 202
    listed = await service.post(route, *wire_request("tools/list", version=version))
    assert listed.status_code == 200
    assert {tool["name"] for tool in listed.json()["result"]["tools"]} == EXPECTED_TOOLS
    status = await service.tool(route, "device_status", version=version)
    assert status["backend"] == "mock" and status["hardware_connected"] is False
    assert service.received_versions == [version] * 4
    assert service.backend.calls == 0


async def test_relay_preserves_protocol_instead_of_silently_rewriting(tunnel_contract):
    service = tunnel_contract
    body, headers = wire_request("tools/list")
    response = await service.relay.post(
        body, headers, static={"mcp-protocol-version": "2025-03-26"}
    )
    assert response.status_code == 200
    assert service.received_versions[-1] == MODERN
    # A corrupted forwarded version must reach the validator, not be repaired.
    headers["MCP-Protocol-Version"] = "2099-01-01"
    response = await service.relay.post(body, headers)
    assert response.status_code == 400
    assert response.json() == {"error": "request_rejected"}
    assert service.received_versions[-1] == "2099-01-01"


@pytest.mark.parametrize("authorization", [None, "Bearer " + "c" * 64])
async def test_missing_or_invalid_bearer_cannot_reach_tools(tunnel_contract, authorization):
    body, headers = wire_request("tools/list")
    headers.pop("Authorization")
    if authorization is not None:
        headers["Authorization"] = authorization
    response = await tunnel_contract.relay.post(body, headers)
    assert response.status_code == 401
    assert response.json() == {"error": "authentication_required"}
    assert tunnel_contract.backend.calls == 0


async def test_forwarded_invalid_bearer_overrides_static_valid_bearer(tunnel_contract):
    body, headers = wire_request("tools/list")
    headers.pop("Authorization")
    static = {"AUTHORIZATION": f"Bearer {REMOTE}"}
    assert (await tunnel_contract.relay.post(body, headers, static)).status_code == 200
    headers["authorization"] = "Bearer " + "c" * 64
    rejected = await tunnel_contract.relay.post(body, headers, static)
    assert rejected.status_code == 401
    assert rejected.json() == {"error": "authentication_required"}
    assert REMOTE not in rejected.text


@pytest.mark.parametrize(
    "hostile",
    [
        {"Origin": "https://synthetic-hostile.invalid"},
        {"Forwarded": "for=127.0.0.1;host=localhost"},
        {"X-Forwarded-For": "127.0.0.1"},
        {"X-Forwarded-Host": "localhost"},
        {"X-Forwarded-Proto": "http"},
    ],
)
async def test_relay_does_not_turn_hostile_origin_or_forwarding_into_local_trust(
    tunnel_contract, hostile
):
    body, headers = wire_request("tools/list")
    response = await tunnel_contract.relay.post(body, {**headers, **hostile})
    assert response.status_code == 403
    assert response.json() == {"error": "local_only"}
    assert tunnel_contract.backend.calls == 0


async def test_remote_bearer_cannot_claim_local_owner(tunnel_contract):
    spoofed = await tunnel_contract.tool("relay", "acquire_session", {"owner": "local_cli"})
    assert spoofed == {"ok": False, "error": "authenticated_owner_mismatch"}
    assert tunnel_contract.broker.session is None
    assert tunnel_contract.backend.calls == 0


async def test_direct_and_relay_compete_for_one_broker_and_keep_owner_boundary(tunnel_contract):
    service = tunnel_contract
    actors = [("direct", LOCAL, "local_cli"), ("relay", REMOTE, "remote_mcp")]
    leases = await asyncio.gather(
        *(
            service.tool(route, "acquire_session", {"owner": owner}, token=token)
            for route, token, owner in actors
        )
    )
    assert sum(lease["ok"] for lease in leases) == 1
    winner = next(i for i, lease in enumerate(leases) if lease["ok"])
    loser = 1 - winner
    assert leases[loser] == {"ok": False, "error": "device_busy"}
    route, token, owner = actors[winner]
    other_route, other_token, other_owner = actors[loser]
    session = leases[winner]["session"]
    prepared = await service.tool(
        route, "prepare_test", {"session": session, "job_id": "synthetic-relay"}, token=token
    )
    assert prepared["approval_required"] is False
    digest = prepared["plan_hash"]
    for name, request in (
        ("renew_session", {"session": session}),
        ("release_session", {"session": session}),
        ("prepare_test", {"session": session, "job_id": "synthetic-stolen"}),
        ("execute_test", {"session": session, "plan_hash": digest}),
        ("cancel_job", {"session": session, "job_id": "synthetic-relay"}),
    ):
        stolen = await service.tool(other_route, name, request, token=other_token)
        assert stolen == {"ok": False, "error": "authenticated_owner_mismatch"}
    accepted = await service.tool(
        route, "execute_test", {"session": session, "plan_hash": digest}, token=token
    )
    assert accepted["ok"] is True
    async with asyncio.timeout(5):
        await service.broker.queue.join()
    repeated = await service.tool(
        route, "execute_test", {"session": session, "plan_hash": digest}, token=token
    )
    assert repeated["deduplicated"] is True
    assert repeated["state"] == "succeeded"
    assert service.backend.calls == service.backend.max_active == 1
    status = await service.tool(other_route, "device_status", token=other_token)
    logs = await service.tool(other_route, "read_logs", {}, token=other_token)
    assert status["owner"] == owner and status["hardware_connected"] is False
    assert status["tunnel"] == "external_lifecycle_unknown"
    public = json.dumps([status, logs, repeated])
    persisted = "\n".join(service.broker._store().db.iterdump())
    for secret in (LOCAL, REMOTE, session):
        assert secret not in public + persisted
    await service.tool(route, "release_session", {"session": session}, token=token)
    handover = await service.tool(
        other_route, "acquire_session", {"owner": other_owner}, token=other_token
    )
    assert handover["ok"] is True and handover["owner"] == other_owner
    await service.tool(
        other_route, "release_session", {"session": handover["session"]}, token=other_token
    )


async def test_relay_rejected_input_is_not_echoed_or_persisted(tunnel_contract, caplog):
    service = tunnel_contract
    marker = "SYNTHETIC_RAW_PAYLOAD_DO_NOT_ECHO"
    body, headers = wire_request(
        "tools/call",
        params={
            "name": "prepare_test",
            "arguments": {"request": {"session": "c" * 64, "job_id": f"https://{marker}"}},
        },
    )
    rejected = await service.relay.post(body, headers)
    assert rejected.status_code == 200
    assert rejected.json()["result"]["isError"] is True
    body, headers = wire_request("tools/list")
    body["method"] = {"synthetic_secret": marker}
    malformed = await service.relay.post(body, headers)
    assert malformed.status_code == 400
    assert malformed.json() == {"error": "request_rejected"}
    status = await service.tool("relay", "device_status")
    logs = await service.tool("relay", "read_logs", {})
    persisted = "\n".join(service.broker._store().db.iterdump())
    visible = rejected.text + malformed.text + json.dumps([status, logs]) + caplog.text + persisted
    for secret in (marker, LOCAL, REMOTE):
        assert secret not in visible
    assert service.backend.calls == 0


async def test_remote_relay_explicit_execute_rejects_legacy_approval_fields(tunnel_contract):
    service = tunnel_contract
    acquired = await service.tool("relay", "acquire_session", {"owner": "remote_mcp"})
    session = acquired["session"]
    prepared = await service.tool(
        "relay", "prepare_test", {"session": session, "job_id": "synthetic-explicit"}
    )
    assert prepared["approval_required"] is False
    request = {"session": session, "plan_hash": prepared["plan_hash"]}
    # Retired approval tools and extra grant fields remain invalid inputs.
    for tool, arguments in (
        ("approve_plan", {"plan_hash": prepared["plan_hash"]}),
        ("execute_test", {"request": {**request, "approved": True}}),
    ):
        body, headers = wire_request("tools/call", params={"name": tool, "arguments": arguments})
        rejected = await service.relay.post(body, headers)
        assert rejected.status_code == 200
        assert rejected.json()["result"]["isError"] is True
    assert service.backend.calls == 0
    accepted = await service.tool("relay", "execute_test", request)
    assert accepted["ok"] is True
    async with asyncio.timeout(5):
        await service.broker.queue.join()
    assert service.backend.calls == service.backend.max_active == 1
    assert service.broker.queue.empty()
    await service.tool("relay", "release_session", {"session": session})
