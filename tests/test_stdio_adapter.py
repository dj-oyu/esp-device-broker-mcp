"""Synthetic localhost/stdio transport checks; no real keys, USB, or accounts."""

import asyncio
import json
import os
import socket
import sys
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
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
from server_ready import LOG_LEVEL, wait_until_started
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
    keys: set[str] = field(default_factory=lambda: {KEY})
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
        if headers.get(b"authorization") not in {f"Bearer {key}".encode() for key in service.keys}:
            # Same status and fixed body as the broker's pre-dispatch auth guard.
            await send(
                {
                    "type": "http.response.start",
                    "status": 401,
                    "headers": [(b"content-type", b"application/json")],
                }
            )
            await send(
                {"type": "http.response.body", "body": b'{"error":"authentication_required"}'}
            )
            return
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
            log_level=LOG_LEVEL,
            access_log=False,
            proxy_headers=False,
        )
    )
    task = asyncio.create_task(running.serve())
    try:
        await wait_until_started(running, task, "Synthetic upstream")
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
    with pytest.raises(StdioAdapterError, match="^stdio_adapter_client_key_rejected$"):
        await HTTPBrokerAdapter(synthetic_service.url, UNKNOWN_KEY).check_broker()
    # Any other 401/403 body is not the broker's auth guard: no key reload.
    synthetic_service.status = 401
    with pytest.raises(StdioAdapterError, match="^stdio_adapter_authentication_failed$"):
        await HTTPBrokerAdapter(synthetic_service.url, KEY).check_broker()
    synthetic_service.status = None
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
    with pytest.raises(StdioAdapterError, match="^stdio_adapter_client_key_rejected$"):
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


async def _start_stdio(
    url: str, key: str, env_file: Path | None = None
) -> asyncio.subprocess.Process:
    # Test-only synthetic key via environment; never place a bearer token in argv.
    program = (
        "import asyncio,logging,os,sys; from pathlib import Path; "
        "from cardputer_mcp.stdio_adapter import run_stdio,StdioAdapterError; "
        "logging.basicConfig(level=logging.DEBUG); "
        "env_file=Path(os.environ['TEST_ENV_FILE']) if os.environ.get('TEST_ENV_FILE') else None"
        "\ntry: asyncio.run(run_stdio(os.environ['TEST_URL'],os.environ['TEST_KEY'],env_file))"
        "\nexcept StdioAdapterError as exc: print(str(exc),file=sys.stderr);sys.exit(1)"
    )
    return await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        program,
        env={
            **os.environ,
            "TEST_URL": url,
            "TEST_KEY": key,
            "TEST_ENV_FILE": "" if env_file is None else str(env_file),
        },
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
    assert b"stdio_adapter_client_key_rejected" in errors
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


async def _release_after(gate: asyncio.Event, seconds: float) -> None:
    await asyncio.sleep(seconds)
    gate.set()


async def test_enqueue_tools_get_their_own_longer_deadline(synthetic_service, monkeypatch):
    from cardputer_mcp import stdio_adapter

    service = synthetic_service
    monkeypatch.setattr(stdio_adapter, "_REQUEST_SECONDS", 1.0)
    monkeypatch.setattr(stdio_adapter, "_TOOL_SECONDS", {"mixed": 8.0})
    adapter = HTTPBrokerAdapter(service.url, KEY)
    service.gate = asyncio.Event()
    releasing = asyncio.create_task(_release_after(service.gate, 2.0))
    result = await adapter.call_tool(CallToolRequestParams(name="mixed", arguments={}))
    await releasing
    assert result.content == MIXED.content
    # Any other tool keeps the default deadline, which covers the whole request.
    monkeypatch.setattr(stdio_adapter, "_TOOL_SECONDS", {})
    service.gate = asyncio.Event()
    releasing = asyncio.create_task(_release_after(service.gate, 2.0))
    with pytest.raises(StdioAdapterError, match="^stdio_adapter_tool_outcome_unknown$"):
        await adapter.call_tool(CallToolRequestParams(name="mixed", arguments={}))
    await releasing
    assert [name for name, _ in service.calls].count("mixed") == 2


async def test_received_result_is_kept_when_teardown_overruns(synthetic_service, monkeypatch):
    from cardputer_mcp import stdio_adapter

    monkeypatch.setattr(stdio_adapter, "_TOOL_SECONDS", {"mixed": 2.0})
    original = stdio_adapter.Client.__aexit__

    async def slow_exit(self, *args):
        try:
            return await original(self, *args)
        finally:
            await asyncio.sleep(30)

    monkeypatch.setattr(stdio_adapter.Client, "__aexit__", slow_exit)
    adapter = HTTPBrokerAdapter(synthetic_service.url, KEY)
    started = asyncio.get_running_loop().time()
    result = await adapter.call_tool(CallToolRequestParams(name="mixed", arguments={}))
    assert result.content == MIXED.content
    assert asyncio.get_running_loop().time() - started < 10


def test_outcome_unknown_guidance_echoes_only_the_callers_own_identifiers():
    from cardputer_mcp.stdio_adapter import outcome_unknown_guidance

    plan_hash = "e" * 64
    guidance = json.loads(
        outcome_unknown_guidance(
            CallToolRequestParams(
                name="flash",
                arguments={
                    "request": {"plan_hash": plan_hash, "session": SECRET, "job_id": "job-1"}
                },
            )
        )
    )
    assert guidance["code"] == "stdio_adapter_tool_outcome_unknown"
    assert guidance["plan_hash"] == plan_hash and guidance["job_id"] == "job-1"
    assert "status(view=job)" in guidance["next_step"]
    assert "deduplicated" in guidance["next_step"]
    assert SECRET not in json.dumps(guidance)
    malformed = json.loads(
        outcome_unknown_guidance(
            CallToolRequestParams(name="flash", arguments={"plan_hash": SECRET, "job_id": "x" * 65})
        )
    )
    assert "plan_hash" not in malformed and "job_id" not in malformed
    assert SECRET not in json.dumps(malformed)


async def test_outcome_unknown_result_carries_fixed_guidance(monkeypatch):
    from cardputer_mcp import stdio_adapter

    captured: dict[str, Any] = {}

    def fake_server(name, **kwargs):
        captured.update(kwargs)

    class Failing:
        instructions = None
        code = "stdio_adapter_tool_outcome_unknown"

        async def call_tool(self, params):
            raise StdioAdapterError(self.code)

    monkeypatch.setattr(stdio_adapter, "Server", fake_server)
    failing = Failing()
    create_stdio_server(failing)
    params = CallToolRequestParams(name="flash", arguments={"request": {"plan_hash": "f" * 64}})
    result = await captured["on_call_tool"](None, params)
    assert result.is_error
    assert result.content[0].text == "stdio_adapter_tool_outcome_unknown"
    assert json.loads(result.content[1].text)["plan_hash"] == "f" * 64
    failing.code = "stdio_adapter_broker_unavailable"
    result = await captured["on_call_tool"](None, params)
    assert [item.text for item in result.content] == ["stdio_adapter_broker_unavailable"]


# --- Reloading the private env file after a key rejection -------------------

NEW_KEY = "cpk_" + "e" * 32 + "." + "f" * 64
META = {
    "io.modelcontextprotocol/protocolVersion": "2026-07-28",
    "io.modelcontextprotocol/clientInfo": {"name": "test", "version": "1"},
    "io.modelcontextprotocol/clientCapabilities": {},
}


def _write_env(path: Path, url: str, key: str) -> Path:
    path.write_bytes(f"CARDPUTER_BROKER_URL={url}\nCARDPUTER_CLIENT_KEY={key}\n".encode())
    if os.name == "posix":
        # The env-file reader requires an owner-only 0600/0400 file on POSIX.
        path.chmod(0o600)
    return path


def _count_attempts(monkeypatch, adapter: HTTPBrokerAdapter) -> list[str]:
    tokens: list[str] = []
    original = adapter._attempt

    async def counting(action, token, **kwargs):
        tokens.append(token)
        return await original(action, token, **kwargs)

    monkeypatch.setattr(adapter, "_attempt", counting)
    return tokens


def _assert_no_secrets(text: str, *secrets: str) -> None:
    # A key id (cpk_<32 hex>) is public broker output; the secret half never is.
    for secret in secrets:
        assert secret not in text
        if secret.startswith("cpk_"):
            assert secret.split(".", 1)[1] not in text
    assert "Bearer" not in text


async def test_rotated_key_in_env_file_is_used_after_one_reconnect(
    registry_service, tmp_path, monkeypatch
):
    service = registry_service
    old = service.keys["second"]
    env_file = _write_env(tmp_path / "client.env", service.url, old.token)
    adapter = HTTPBrokerAdapter(service.url, old.token, env_file)
    await adapter.check_broker()
    rotated = service.registry.rotate(old.metadata.key_id)
    _write_env(env_file, service.url, rotated.token)
    attempts = _count_attempts(monkeypatch, adapter)
    status = await adapter.call_tool(
        CallToolRequestParams(name="status", arguments={"request": {"view": "device"}})
    )
    assert status.structured_content["ok"]
    assert attempts == [old.token, rotated.token]
    # Later requests use the new key directly, with no further reload.
    await adapter.list_tools()
    assert attempts == [old.token, rotated.token, rotated.token]
    assert service.resident.session is None and service.resident.active_job is None


async def test_unchanged_key_is_not_retried_and_names_the_fix(
    registry_service, tmp_path, monkeypatch
):
    service = registry_service
    key = service.keys["second"]
    env_file = _write_env(tmp_path / "client.env", service.url, key.token)
    adapter = HTTPBrokerAdapter(service.url, key.token, env_file)
    service.registry.revoke(key.metadata.key_id)
    attempts = _count_attempts(monkeypatch, adapter)
    with pytest.raises(StdioAdapterError, match="^stdio_adapter_client_key_rejected$"):
        await adapter.call_tool(CallToolRequestParams(name="status", arguments={}))
    assert attempts == [key.token]


async def test_changed_broker_url_is_not_followed(registry_service, tmp_path, monkeypatch):
    service = registry_service
    old = service.keys["second"]
    env_file = _write_env(tmp_path / "client.env", service.url, old.token)
    adapter = HTTPBrokerAdapter(service.url, old.token, env_file)
    rotated = service.registry.rotate(old.metadata.key_id)
    port = int(service.url.rsplit(":", 1)[1].split("/")[0])
    other = f"http://127.0.0.1:{1025 if port != 1025 else 1026}/mcp"
    _write_env(env_file, other, rotated.token)
    attempts = _count_attempts(monkeypatch, adapter)
    with pytest.raises(StdioAdapterError, match="^stdio_adapter_broker_url_changed$"):
        await adapter.list_tools()
    assert attempts == [old.token]
    assert adapter._token == old.token and adapter._url == service.url


@pytest.mark.parametrize(
    "content",
    [b"", b'CARDPUTER_CLIENT_KEY="quoted"\n', b"CARDPUTER_CLIENT_KEY=" + NEW_KEY.encode() * 2],
)
async def test_invalid_env_file_after_rotation_is_a_fixed_error(
    registry_service, tmp_path, monkeypatch, content
):
    service = registry_service
    old = service.keys["second"]
    env_file = _write_env(tmp_path / "client.env", service.url, old.token)
    adapter = HTTPBrokerAdapter(service.url, old.token, env_file)
    service.registry.rotate(old.metadata.key_id)
    env_file.write_bytes(content)
    attempts = _count_attempts(monkeypatch, adapter)
    with pytest.raises(StdioAdapterError, match="^stdio_adapter_client_environment_invalid$"):
        await adapter.list_tools()
    assert attempts == [old.token] and adapter._token == old.token
    # A missing file is reported the same way.
    env_file.unlink()
    with pytest.raises(StdioAdapterError, match="^stdio_adapter_client_environment_invalid$"):
        await adapter.list_tools()


async def test_retry_happens_at_most_once_even_if_the_file_changes_again(
    synthetic_service, tmp_path, monkeypatch
):
    from cardputer_mcp import client_environment

    service = synthetic_service
    service.keys = set()
    env_file = _write_env(tmp_path / "client.env", service.url, NEW_KEY)
    adapter = HTTPBrokerAdapter(service.url, KEY, env_file)
    attempts = _count_attempts(monkeypatch, adapter)
    original = client_environment.load_client_environment
    loads: list[Path] = []

    def load_and_rotate_again(path):
        loads.append(path)
        loaded = original(path)
        _write_env(env_file, service.url, UNKNOWN_KEY)
        return loaded

    monkeypatch.setattr(client_environment, "load_client_environment", load_and_rotate_again)
    with pytest.raises(StdioAdapterError, match="^stdio_adapter_client_key_rejected$"):
        await adapter.call_tool(CallToolRequestParams(name="mixed", arguments={}))
    assert attempts == [KEY, NEW_KEY] and loads == [env_file]
    assert service.calls == []


async def test_rejected_tool_call_is_sent_once_with_the_new_key(synthetic_service, tmp_path):
    service = synthetic_service
    service.keys = {NEW_KEY}
    env_file = _write_env(tmp_path / "client.env", service.url, NEW_KEY)
    adapter = HTTPBrokerAdapter(service.url, KEY, env_file)
    result = await adapter.call_tool(CallToolRequestParams(name="mixed", arguments={}))
    assert result.content == MIXED.content
    assert service.calls == [IDENTITY_CALL, ("mixed", {})]


async def test_executed_call_is_never_replayed_even_with_a_newer_key(
    synthetic_service, tmp_path, monkeypatch
):
    service = synthetic_service
    service.keys = {KEY, NEW_KEY}
    env_file = _write_env(tmp_path / "client.env", service.url, NEW_KEY)
    adapter = HTTPBrokerAdapter(service.url, KEY, env_file)
    attempts = _count_attempts(monkeypatch, adapter)
    service.lose_response = True
    with pytest.raises(StdioAdapterError, match="^stdio_adapter_tool_outcome_unknown$"):
        await adapter.call_tool(CallToolRequestParams(name="mixed", arguments={}))
    assert attempts == [KEY] and adapter._token == KEY
    assert [name for name, _ in service.calls].count("mixed") == 1


async def test_rejection_that_does_not_prove_non_execution_is_outcome_unknown(
    monkeypatch, tmp_path
):
    from cardputer_mcp import stdio_adapter

    adapter = HTTPBrokerAdapter("http://127.0.0.1:9000/mcp", KEY, tmp_path / "unused.env")
    calls: list[str] = []

    async def rejected(action, token, **kwargs):
        calls.append(token)
        raise stdio_adapter._KeyRejected(False)

    def reload(path):
        raise AssertionError("must not re-read the env file")

    monkeypatch.setattr(adapter, "_attempt", rejected)
    monkeypatch.setattr(stdio_adapter.client_environment, "load_client_environment", reload)
    with pytest.raises(StdioAdapterError, match="^stdio_adapter_tool_outcome_unknown$"):
        await adapter.call_tool(CallToolRequestParams(name="mixed", arguments={}))
    assert calls == [KEY]


def test_only_a_jsonrpc_request_proves_the_operation_was_rejected():
    import httpx2

    from cardputer_mcp.stdio_adapter import _is_jsonrpc_request

    url = "http://127.0.0.1:9000/mcp"

    def post(frame: object) -> httpx2.Request:
        return httpx2.Request("POST", url, json=frame)

    assert _is_jsonrpc_request(post({"jsonrpc": "2.0", "id": 3, "method": "tools/call"}))
    assert not _is_jsonrpc_request(post({"jsonrpc": "2.0", "method": "notifications/cancelled"}))
    assert not _is_jsonrpc_request(post([{"jsonrpc": "2.0", "id": 3, "method": "tools/call"}]))
    assert not _is_jsonrpc_request(httpx2.Request("POST", url, content=b"{"))


async def test_adapter_without_env_file_does_not_reload(synthetic_service):
    adapter = HTTPBrokerAdapter(synthetic_service.url, UNKNOWN_KEY)
    with pytest.raises(StdioAdapterError, match="^stdio_adapter_client_key_rejected$"):
        await adapter.list_tools()


async def test_concurrent_rejections_reload_the_env_file_once(
    synthetic_service, tmp_path, monkeypatch
):
    from cardputer_mcp import client_environment

    service = synthetic_service
    service.keys = {NEW_KEY}
    env_file = _write_env(tmp_path / "client.env", service.url, NEW_KEY)
    adapter = HTTPBrokerAdapter(service.url, KEY, env_file)
    original = client_environment.load_client_environment
    loads: list[Path] = []

    def counting(path):
        loads.append(path)
        return original(path)

    monkeypatch.setattr(client_environment, "load_client_environment", counting)
    results = await asyncio.gather(*(adapter.list_tools() for _ in range(3)))
    assert all(
        [tool.name for tool in result.tools] == [tool.name for tool in TOOLS] for result in results
    )
    assert loads == [env_file]


@pytest.mark.parametrize(
    "code",
    [
        "stdio_adapter_client_key_rejected",
        "stdio_adapter_client_environment_invalid",
        "stdio_adapter_broker_url_changed",
    ],
)
async def test_key_rejection_results_carry_fixed_guidance(monkeypatch, code):
    from mcp.shared.exceptions import MCPError

    from cardputer_mcp import stdio_adapter

    captured: dict[str, Any] = {}

    def fake_server(name, **kwargs):
        captured.update(kwargs)

    class Failing:
        instructions = None

        async def call_tool(self, params):
            raise StdioAdapterError(code)

        async def list_tools(self, params):
            raise StdioAdapterError(code)

    monkeypatch.setattr(stdio_adapter, "Server", fake_server)
    create_stdio_server(Failing())
    params = CallToolRequestParams(name="status", arguments={"request": {"key": KEY}})
    result = await captured["on_call_tool"](None, params)
    assert result.is_error and result.content[0].text == code
    guidance = json.loads(result.content[1].text)
    assert guidance["code"] == code and guidance["client_key_rejected"] is True
    if code == "stdio_adapter_client_key_rejected":
        assert "keys rotate --key-id <key-id> --write-env <env-file>" in guidance["next_step"]
        assert "keys issue" in guidance["next_step"]
        assert "no client restart is needed" in guidance["next_step"]
    _assert_no_secrets(json.dumps(guidance), KEY)
    with pytest.raises(MCPError) as listing:
        await captured["on_list_tools"](None, None)
    assert listing.value.message == code and listing.value.data == guidance


async def test_stdio_process_picks_up_rotated_key_without_restart(registry_service, tmp_path):
    service = registry_service
    old = service.keys["second"]
    env_file = _write_env(tmp_path / "client.env", service.url, old.token)
    process = await _start_stdio(service.url, old.token, env_file)

    def identity(frame_id: int) -> dict[str, Any]:
        return {
            "jsonrpc": "2.0",
            "id": frame_id,
            "method": "tools/call",
            "params": {
                "name": "status",
                "arguments": {"request": {"view": "identity"}},
                "_meta": META,
            },
        }

    try:
        first = await _exchange(process, identity(1))
        assert first["result"]["structuredContent"]["authenticated"] is True
        rotated = service.registry.rotate(old.metadata.key_id)
        _write_env(env_file, service.url, rotated.token)
        second = await _exchange(process, identity(2))
        assert second["result"]["structuredContent"]["authenticated"] is True
        service.registry.revoke(rotated.metadata.key_id)
        third = await _exchange(process, identity(3))
        assert third["result"]["isError"] is True
        texts = [item["text"] for item in third["result"]["content"]]
        assert texts[0] == "stdio_adapter_client_key_rejected"
        assert "keys rotate" in json.loads(texts[1])["next_step"]
        process.stdin.close()
        tail, errors = await asyncio.wait_for(process.communicate(), timeout=10)
        assert process.returncode == 0 and tail == b""
        visible = json.dumps([second, third]) + errors.decode(errors="replace")
        _assert_no_secrets(visible, old.token, rotated.token, str(tmp_path))
    finally:
        if process.returncode is None:
            process.kill()
            await process.communicate()
