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


async def test_remote_bearer_cannot_claim_local_owner(tunnel_contract):
    spoofed = await tunnel_contract.tool(
        "relay", "session", {"action": "open", "owner": "local_cli", "lease_seconds": 30}
    )
    assert spoofed == {"ok": False, "error": "authenticated_owner_mismatch"}
    assert tunnel_contract.broker.session is None
    assert tunnel_contract.backend.calls == 0
