"""Synthetic localhost/stdio transport checks; no real keys, USB, or accounts."""

import asyncio
import json
import os
import socket
import sys
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import pytest
import uvicorn
from mcp.server import Server
from mcp.types import (
    CallToolRequestParams,
    CallToolResult,
    ImageContent,
    ListToolsResult,
    TextContent,
    Tool,
    ToolAnnotations,
)
from test_registry_http import registry_service as registry_service

from cardputer_mcp.client import connect
from cardputer_mcp.stdio_adapter import (
    HTTPBrokerAdapter,
    StdioAdapterError,
    create_stdio_server,
)

KEY = "cpk_" + "a" * 32 + "." + "b" * 64
UNKNOWN_KEY = "cpk_" + "c" * 32 + "." + "d" * 64
SECRET = "synthetic-exception-private-marker"
IDENTITY_CALL = ("status", {"request": {"view": "identity"}})
TOOLS = [
    Tool(
        name="status",
        description="Authenticate this connection",
        input_schema={"type": "object"},
    ),
    Tool(
        name="mixed",
        title="Mixed result",
        description="Preserve all current and future content blocks",
        input_schema={
            "type": "object",
            "properties": {"line": {"type": "string"}},
            "additionalProperties": False,
        },
        output_schema={"type": "object", "properties": {"ok": {"type": "boolean"}}},
        annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False),
        _meta={"synthetic/catalog": {"version": 2}},
    ),
]
MIXED = CallToolResult(
    content=[
        TextContent(type="text", text="first line\nsecond line\n日本語"),
        ImageContent(type="image", data="c3ludGhldGlj", mime_type="image/png"),
        TextContent(type="text", text="trailing text", _meta={"synthetic/block": "kept"}),
    ],
    structured_content={"ok": True, "nested": {"lines": ["one", "two"]}},
    _meta={"synthetic/result": "kept"},
)


@dataclass
class SyntheticService:
    url: str = ""
    calls: list[tuple[str, dict[str, Any] | None]] = field(default_factory=list)
    methods: list[str] = field(default_factory=list)
    catalog_params: list[Any] = field(default_factory=list)
    identity: dict[str, Any] = field(
        default_factory=lambda: {
            "ok": True,
            "authenticated": True,
            "origin": "remote_mcp",
            "scopes": ["read", "control"],
        }
    )
    status: int | None = None
    lose_response: bool = False
    malformed_notification: bool = False
    gate: asyncio.Event | None = None
    started: asyncio.Event = field(default_factory=asyncio.Event)
    result: CallToolResult = field(default_factory=lambda: MIXED.model_copy(deep=True))
    active: int = 0
    peak: int = 0


@pytest.fixture
async def synthetic_service() -> AsyncIterator[SyntheticService]:
    service = SyntheticService()

    async def list_tools(ctx, params):
        service.catalog_params.append(params)
        return ListToolsResult(
            tools=TOOLS,
            next_cursor="page-three" if params is not None and params.cursor else None,
            _meta={"synthetic/page": "kept"},
        )

    async def call_tool(ctx, params):
        service.calls.append((params.name, params.arguments))
        if params.name == "status":
            return CallToolResult(content=[], structured_content=dict(service.identity))
        service.active += 1
        service.peak = max(service.peak, service.active)
        service.started.set()
        try:
            if service.gate is not None:
                await service.gate.wait()
            return service.result.model_copy(deep=True)
        finally:
            service.active -= 1

    server = Server(
        "synthetic-upstream",
        instructions="Create a work session explicitly before requesting the device.",
        on_list_tools=list_tools,
        on_call_tool=call_tool,
    )
    inner = server.streamable_http_app(json_response=True, stateless_http=True)

    async def app(scope, receive, send):
        if scope["type"] != "http":
            await inner(scope, receive, send)
            return
        service.methods.append(scope["method"])
        headers = dict(scope["headers"])
        status = service.status
        if headers.get(b"authorization") != f"Bearer {KEY}".encode():
            status = 401
        if status is not None:
            await send(
                {
                    "type": "http.response.start",
                    "status": status,
                    "headers": [(b"content-type", b"application/json")],
                }
            )
            await send({"type": "http.response.body", "body": SECRET.encode()})
            return
        target = False

        async def read():
            nonlocal target
            message = await receive()
            body = message.get("body", b"")
            if body:
                frame = json.loads(body)
                target = frame.get("method") == "tools/call" and frame["params"]["name"] == "mixed"
            return message

        async def write(message):
            if target and service.malformed_notification:
                if message["type"] == "http.response.start":
                    message = {
                        **message,
                        "headers": [
                            (key, value)
                            for key, value in message.get("headers", [])
                            if key not in (b"content-length", b"content-type")
                        ]
                        + [(b"content-type", b"text/event-stream")],
                    }
                elif message["type"] == "http.response.body":
                    notification = {
                        "jsonrpc": "2.0",
                        "method": "notifications/message",
                        "params": {"level": SECRET, "data": "synthetic upstream log"},
                    }
                    message = {
                        **message,
                        "body": b"event: message\ndata: "
                        + json.dumps(notification).encode()
                        + b"\n\nevent: message\ndata: "
                        + message["body"]
                        + b"\n\n",
                    }
            if target and service.lose_response:
                if message["type"] == "http.response.start":
                    message = {
                        **message,
                        "headers": [
                            (key, value)
                            for key, value in message.get("headers", [])
                            if key != b"content-length"
                        ],
                    }
                elif message["type"] == "http.response.body":
                    service.lose_response = False
                    message = {**message, "body": b'{"unparseable":"' + SECRET.encode()}
            await send(message)

        await inner(scope, read, write)

    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    running = uvicorn.Server(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=port,
            log_level="critical",
            access_log=False,
            proxy_headers=False,
        )
    )
    task = asyncio.create_task(running.serve())
    try:
        async with asyncio.timeout(5):
            while not running.started:
                if task.done():
                    await task
                    pytest.fail("Synthetic upstream stopped before readiness")
                await asyncio.sleep(0.01)
        service.url = f"http://127.0.0.1:{port}/mcp"
        yield service
    finally:
        if service.gate is not None:
            service.gate.set()
        running.should_exit = True
        await asyncio.wait_for(task, timeout=5)


async def test_catalog_and_results_are_lossless(synthetic_service):
    service = synthetic_service
    adapter = HTTPBrokerAdapter(service.url, KEY)
    await adapter.check_broker()
    assert adapter.instructions == "Create a work session explicitly before requesting the device."
    assert create_stdio_server(adapter).instructions == adapter.instructions
    catalog = await adapter.list_tools()
    assert [tool.model_dump() for tool in catalog.tools] == [tool.model_dump() for tool in TOOLS]
    assert catalog.meta["synthetic/page"] == "kept"
    result = await adapter.call_tool(
        CallToolRequestParams(name="mixed", arguments={"line": "a\nb"})
    )
    assert result.content == MIXED.content
    assert result.structured_content == MIXED.structured_content
    assert result.is_error is False
    assert result.meta["synthetic/result"] == "kept"
    assert service.calls == [
        IDENTITY_CALL,
        IDENTITY_CALL,
        IDENTITY_CALL,
        ("mixed", {"line": "a\nb"}),
    ]
    assert "DELETE" not in service.methods
    service.result = MIXED.model_copy(update={"is_error": True})
    errored = await adapter.call_tool(CallToolRequestParams(name="mixed"))
    assert errored.is_error is True and errored.content == MIXED.content


@pytest.mark.parametrize("key", ["", "malformed", KEY + SECRET])
def test_invalid_or_ephemeral_keys_fail_closed(key):
    with pytest.raises(StdioAdapterError, match="^stdio_adapter_invalid_key$"):
        HTTPBrokerAdapter("http://127.0.0.1:9000/mcp", key)


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1:9000/mcp",
        "http://example.invalid:9000/mcp",
        "http://127.0.0.1:9000/mcp?key=private",
        "http://localhost:9000/mcp",
        "http://127.0.0.1:09000/mcp",
    ],
)
def test_only_exact_loopback_endpoint_is_allowed(url):
    with pytest.raises(StdioAdapterError, match="^stdio_adapter_invalid_url$"):
        HTTPBrokerAdapter(url, KEY)


@pytest.mark.parametrize(
    "change",
    [
        {"origin": "local_cli"},
        {"authenticated": False},
        {"ok": False},
        {"scopes": ["control"]},
        {"scopes": "read"},
    ],
)
async def test_identity_must_prove_authenticated_remote_read_scope(synthetic_service, change):
    synthetic_service.identity.update(change)
    with pytest.raises(StdioAdapterError, match="^stdio_adapter_remote_read_key_required$"):
        await HTTPBrokerAdapter(synthetic_service.url, KEY).check_broker()
    assert synthetic_service.calls == [IDENTITY_CALL]


async def test_invalid_auth_and_offline_broker_are_fixed_errors(synthetic_service):
    with pytest.raises(StdioAdapterError, match="^stdio_adapter_authentication_failed$"):
        await HTTPBrokerAdapter(synthetic_service.url, UNKNOWN_KEY).check_broker()
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        unavailable = f"http://127.0.0.1:{listener.getsockname()[1]}/mcp"
        with pytest.raises(StdioAdapterError, match="^stdio_adapter_broker_unavailable$"):
            await HTTPBrokerAdapter(unavailable, KEY).check_broker()


async def test_redirect_is_rejected_without_following(synthetic_service):
    service = synthetic_service
    service.status = 307
    with pytest.raises(StdioAdapterError, match="^stdio_adapter_endpoint_rejected$"):
        await HTTPBrokerAdapter(service.url, KEY).check_broker()
    assert service.methods == ["POST"]
    assert service.calls == []


@pytest.mark.parametrize("invalidation", ["revoke", "expire"])
async def test_live_registry_invalidation_fails_without_mutation(registry_service, invalidation):
    service = registry_service
    key = service.keys["first"]
    adapter = HTTPBrokerAdapter(service.url, key.token)
    await adapter.check_broker()
    if invalidation == "expire":
        service.now[0] += 11
    else:
        service.registry.revoke(key.metadata.key_id)
    with pytest.raises(StdioAdapterError, match="^stdio_adapter_authentication_failed$"):
        await adapter.list_tools()
    assert service.resident.session is None and service.resident.active_job is None


async def test_actual_broker_catalog_matches_and_connections_do_not_acquire(registry_service):
    service = registry_service
    key = service.keys["reader"].token
    adapter = HTTPBrokerAdapter(service.url, key)
    async with connect(service.url, key) as upstream:
        expected = await upstream.list_tools()
    actual = await adapter.list_tools()
    assert actual.tools == expected.tools
    status = await adapter.call_tool(
        CallToolRequestParams(name="status", arguments={"request": {"view": "device"}})
    )
    assert status.structured_content["ok"]
    assert service.resident.session is None and service.resident.active_job is None
    assert service.resident.work_sessions == {}


async def test_lost_response_is_not_replayed_and_later_request_reconnects(synthetic_service):
    service = synthetic_service
    adapter = HTTPBrokerAdapter(service.url, KEY)
    service.lose_response = True
    with pytest.raises(StdioAdapterError, match="^stdio_adapter_tool_outcome_unknown$"):
        await adapter.call_tool(CallToolRequestParams(name="mixed", arguments={}))
    assert [name for name, _ in service.calls].count("mixed") == 1
    await adapter.list_tools()
    assert [name for name, _ in service.calls].count("mixed") == 1
    result = await adapter.call_tool(CallToolRequestParams(name="mixed", arguments={}))
    assert result.content == MIXED.content
    assert [name for name, _ in service.calls].count("mixed") == 2
    assert "DELETE" not in service.methods


async def test_cancelled_request_does_not_issue_cancel_release_or_replay(synthetic_service):
    service = synthetic_service
    service.gate = asyncio.Event()
    adapter = HTTPBrokerAdapter(service.url, KEY)
    task = asyncio.create_task(adapter.call_tool(CallToolRequestParams(name="mixed")))
    await asyncio.wait_for(service.started.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    service.gate.set()
    await adapter.list_tools()
    assert [name for name, _ in service.calls].count("mixed") == 1
    assert {name for name, _ in service.calls} == {"status", "mixed"}
    assert "DELETE" not in service.methods


async def _start_stdio(url: str, key: str) -> asyncio.subprocess.Process:
    # Test-only synthetic key via environment; never place a bearer token in argv.
    program = (
        "import asyncio,logging,os,sys; "
        "from cardputer_mcp.stdio_adapter import run_stdio,StdioAdapterError; "
        "logging.basicConfig(level=logging.DEBUG); "
        "\ntry: asyncio.run(run_stdio(os.environ['TEST_URL'],os.environ['TEST_KEY']))"
        "\nexcept StdioAdapterError as exc: print(str(exc),file=sys.stderr);sys.exit(1)"
    )
    return await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        program,
        env={**os.environ, "TEST_URL": url, "TEST_KEY": key},
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )


async def _exchange(process, frame):
    process.stdin.write(json.dumps(frame).encode() + b"\n")
    await process.stdin.drain()
    return json.loads(await asyncio.wait_for(process.stdout.readline(), timeout=10))


@pytest.mark.parametrize("version", ["2025-06-18", "2026-07-28"])
async def test_real_stdio_frames_are_clean_and_eof_has_no_side_effects(synthetic_service, version):
    service = synthetic_service
    process = await _start_stdio(service.url, KEY)
    try:
        if version == "2025-06-18":
            first = await _exchange(
                process,
                {
                    "jsonrpc": "2.0",
                    "id": 0,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": version,
                        "clientInfo": {"name": "test", "version": "1"},
                        "capabilities": {},
                    },
                },
            )
            assert first["result"]["capabilities"]["tools"] == {"listChanged": False}
            process.stdin.write(b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
            await process.stdin.drain()
            meta = {}
        else:
            meta = {
                "io.modelcontextprotocol/protocolVersion": version,
                "io.modelcontextprotocol/clientInfo": {"name": "test", "version": "1"},
                "io.modelcontextprotocol/clientCapabilities": {},
            }
        listing = await _exchange(
            process,
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {"_meta": meta}},
        )
        assert "result" in listing, listing
        assert [tool["name"] for tool in listing["result"]["tools"]] == [t.name for t in TOOLS]
        result = await _exchange(
            process,
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "mixed", "arguments": {}, "_meta": meta},
            },
        )
        assert result["result"]["content"] == [
            content.model_dump(by_alias=True, exclude_unset=True) for content in MIXED.content
        ]
        process.stdin.close()
        tail, errors = await asyncio.wait_for(process.communicate(), timeout=10)
        assert process.returncode == 0
        assert tail == b""
        assert KEY.encode() not in errors and SECRET.encode() not in errors
        assert b"Bearer" not in errors
        assert {name for name, _ in service.calls} == {"status", "mixed"}
        assert "DELETE" not in service.methods
    finally:
        if process.returncode is None:
            process.kill()
            await process.communicate()


async def test_stdio_auth_failure_has_empty_stdout_and_sanitized_stderr(synthetic_service):
    process = await _start_stdio(synthetic_service.url, UNKNOWN_KEY)
    output, errors = await asyncio.wait_for(process.communicate(), timeout=10)
    assert process.returncode == 1
    assert output == b""
    assert b"stdio_adapter_authentication_failed" in errors
    assert SECRET.encode() not in errors and UNKNOWN_KEY.encode() not in errors


async def test_malformed_upstream_notification_cannot_leak_through_sdk_client_logger(
    synthetic_service,
):
    service = synthetic_service
    service.malformed_notification = True
    process = await _start_stdio(service.url, KEY)
    try:
        result = await _exchange(
            process,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "mixed",
                    "_meta": {
                        "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                        "io.modelcontextprotocol/clientInfo": {"name": "test", "version": "1"},
                        "io.modelcontextprotocol/clientCapabilities": {},
                    },
                },
            },
        )
        assert result["result"]["content"] == [
            content.model_dump(by_alias=True, exclude_unset=True) for content in MIXED.content
        ]
        process.stdin.close()
        tail, errors = await asyncio.wait_for(process.communicate(), timeout=10)
        assert process.returncode == 0 and tail == b""
        assert SECRET.encode() not in errors
        assert KEY.encode() not in errors
        assert b"Failed to validate notification" not in errors
        assert b"Traceback" not in errors
    finally:
        if process.returncode is None:
            process.kill()
            await process.communicate()


async def test_stdio_eof_keeps_resident_job_and_ownership(registry_service):
    service = registry_service
    service.backend.gate = asyncio.Event()
    key = service.keys["first"].token
    process = await _start_stdio(service.url, key)
    counter = 0

    async def call(name, request):
        nonlocal counter
        counter += 1
        result = await _exchange(
            process,
            {
                "jsonrpc": "2.0",
                "id": counter,
                "method": "tools/call",
                "params": {
                    "name": name,
                    "arguments": {"request": request},
                    "_meta": {
                        "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                        "io.modelcontextprotocol/clientInfo": {"name": "test", "version": "1"},
                        "io.modelcontextprotocol/clientCapabilities": {},
                    },
                },
            },
        )
        return result["result"]["structuredContent"]

    try:
        lease = await call(
            "session", {"action": "open", "owner": "remote_mcp", "lease_seconds": 30}
        )
        session = lease["session"]
        plan = await call(
            "submit", {"kind": "diagnostics", "session": session, "job_id": "stdio-eof"}
        )
        executed = await call(
            "execute", {"op": "run", "session": session, "plan_hash": plan["plan_hash"]}
        )
        assert executed["ok"]
        process.stdin.close()
        tail, errors = await asyncio.wait_for(process.communicate(), timeout=10)
        assert process.returncode == 0 and tail == b""
        assert key.encode() not in errors and session.encode() not in errors
        assert service.resident.session is not None
        assert service.resident.active_job is not None
        service.backend.gate.set()
        await asyncio.wait_for(service.resident.queue.join(), timeout=5)
        result = await HTTPBrokerAdapter(service.url, key).call_tool(
            CallToolRequestParams(
                name="status", arguments={"request": {"view": "job", "job_id": "stdio-eof"}}
            )
        )
        assert result.structured_content["state"] == "succeeded"
        assert service.resident.session is not None
    finally:
        if process.returncode is None:
            process.kill()
            await process.communicate()
