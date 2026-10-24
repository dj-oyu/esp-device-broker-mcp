"""Actual localhost TCP + official SDK client, not in-process tool calls."""

import asyncio
import os
import signal
import socket
import sys
from contextlib import asynccontextmanager

import httpx2
import pytest

from cardputer_mcp.client import call, connect

LOCAL = "a" * 64
REMOTE = "b" * 64


@asynccontextmanager
async def _service(tmp_path, authenticated):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    state = (tmp_path / "state").as_posix()
    config = tmp_path / "mock.toml"
    config.write_text(
        f'port = {port}\nstate_dir = "{state}"\n'
        "allow_mock_execution = true\nmock_stage_seconds = 0.05\n"
    )
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "cardputer_mcp.cli",
        *(["--auth-source", "environment"] if authenticated else []),
        "serve",
        *(["--require-auth"] if authenticated else []),
        "--config",
        str(config),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={**os.environ, "CARDPUTER_LOCAL_TOKEN": LOCAL, "CARDPUTER_REMOTE_TOKEN": REMOTE},
    )
    url = f"http://127.0.0.1:{port}/mcp"
    try:
        async with httpx2.AsyncClient(trust_env=False) as http:
            async with asyncio.timeout(10):
                while True:
                    if process.returncode is not None:
                        _, errors = await process.communicate()
                        pytest.fail(f"Mock server exited before readiness: {errors.decode()}")
                    try:
                        await http.get(url.removesuffix("/mcp") + "/ready")
                        break
                    except httpx2.ConnectError:
                        await asyncio.sleep(0.02)
        yield url
    finally:
        if process.returncode is None:
            if os.name == "nt":
                process.terminate()
            else:
                process.send_signal(signal.SIGINT)
        try:
            await asyncio.wait_for(process.communicate(), timeout=5)
        except TimeoutError:
            process.kill()
            await process.communicate()


@pytest.fixture
async def service(tmp_path):
    async with _service(tmp_path, False) as url:
        yield url


@pytest.fixture
async def authenticated_service(tmp_path):
    async with _service(tmp_path, True) as url:
        yield url


async def test_real_mcp_http_disconnect_retry_handover(authenticated_service):
    service = authenticated_service
    async with connect(service, LOCAL) as first, connect(service, REMOTE) as second:
        advertised = await first.list_tools()
        names = {tool.name for tool in advertised.tools}
        assert names == {"status", "submit", "execute", "flash", "cancel", "session"}
        tools = {tool.name: tool for tool in advertised.tools}
        for name, tool in tools.items():
            assert tool.annotations.open_world_hint is False, name
        # status is the only read-only tool; flash is the only destructive one.
        assert tools["status"].annotations.read_only_hint is True
        assert tools["status"].annotations.destructive_hint is False
        for name in ("submit", "execute", "cancel", "session"):
            assert tools[name].annotations.read_only_hint is False, name
            assert tools[name].annotations.destructive_hint is False, name
        assert tools["flash"].annotations.read_only_hint is False
        assert tools["flash"].annotations.destructive_hint is True
        for name, model, fields, required in (
            (
                "status",
                "StatusRequest",
                {
                    "view",
                    "project_id",
                    "job_id",
                    "resume_token",
                    "stream",
                    "cursor",
                    "limit",
                    "levels",
                    "context",
                    "include_source_cache",
                },
                {"view"},
            ),
            (
                "execute",
                "ExecuteToolRequest",
                {
                    "op",
                    "plan_hash",
                    "session",
                    "job_id",
                    "source_job_id",
                    "boot_id",
                    "run_id",
                    "control_session",
                    "command_id",
                    "seq",
                    "operation",
                    "action",
                },
                {"op"},
            ),
            ("flash", "FlashToolRequest", {"plan_hash", "session"}, {"plan_hash"}),
            (
                "cancel",
                "CancelToolRequest",
                {"target", "job_id", "session", "plan_hash", "resume_token", "request_id"},
                {"target"},
            ),
            (
                "session",
                "SessionRequest",
                {
                    "action",
                    "owner",
                    "ttl_seconds",
                    "lease_seconds",
                    "wait_seconds",
                    "resume_token",
                    "session",
                },
                {"action"},
            ),
        ):
            shape = tools[name].input_schema["$defs"][model]
            assert set(shape["properties"]) == fields, name
            assert set(shape["required"]) == required, name
            assert shape["additionalProperties"] is False, name
        status_shape = tools["status"].input_schema["$defs"]["StatusRequest"]
        assert set(status_shape["properties"]["limit"].get("anyOf", [{}])[0]) >= {"maximum"}
        assert status_shape["properties"]["limit"]["anyOf"][0]["maximum"] == 100
        command_shape = tools["execute"].input_schema["$defs"]["ExecuteToolRequest"]
        assert set(command_shape["properties"]["operation"]["anyOf"][0]["enum"]) == {
            "action.tap",
            "status",
            "suspend",
            "resume",
            "stop",
        }
        assert set(tools["submit"].input_schema["$defs"]["SubmitRequest"]["required"]) == {
            "kind",
            "job_id",
        }
        acquired = await call(
            first, "session", {"action": "open", "owner": "local_cli", "lease_seconds": 30}
        )
        token = acquired["session"]
        busy = await call(
            second, "session", {"action": "open", "owner": "remote_mcp", "lease_seconds": 30}
        )
        assert busy["error"] == "device_busy"
        prepared = await call(
            first, "submit", {"session": token, "job_id": "http-reconnect", "kind": "workflow"}
        )
        executed = await call(
            first, "execute", {"op": "run", "session": token, "plan_hash": prepared["plan_hash"]}
        )
        assert executed["state"] in ("queued", "running")
    # Transport disconnect is not device-workflow cancellation.
    async with connect(service, LOCAL) as later:
        async with asyncio.timeout(5):
            while True:
                finished = await call(later, "status", {"view": "job", "job_id": "http-reconnect"})
                if finished["state"] not in ("queued", "running"):
                    break
                await asyncio.sleep(0.02)
        assert finished["state"] == "succeeded"
        repeated = await call(
            later, "execute", {"op": "run", "session": token, "plan_hash": prepared["plan_hash"]}
        )
        assert repeated["deduplicated"]
        assert repeated["id"] == finished["id"]
        status = await call(later, "status", {"view": "device"})
        assert status["owner"] == "local_cli"
        assert status["hardware_connected"] is False
        logs = await call(later, "status", {"view": "logs", "stream": "broker"})
        stages = [entry["stage"] for entry in logs["entries"] if entry["stage"]]
        assert stages == ["mock_flash", "mock_boot", "mock_test"]
        assert token not in str(status) + str(logs)
        await call(later, "session", {"action": "release", "session": token})
    async with connect(service, REMOTE) as remote:
        acquired = await call(
            remote, "session", {"action": "open", "owner": "remote_mcp", "lease_seconds": 30}
        )
        assert acquired["ok"]
        await call(remote, "session", {"action": "release", "session": acquired["session"]})


async def test_real_http_invalid_origin_and_oversize(authenticated_service):
    service = authenticated_service
    async with httpx2.AsyncClient(trust_env=False) as http:
        evil = await http.post(
            service,
            json={},
            headers={"Origin": "https://hostile.invalid", "Authorization": "Bearer " + LOCAL},
        )
        assert evil.status_code == 403
        oversized = await http.post(
            service,
            content=" " * 20000,
            headers={"content-type": "application/json", "Authorization": "Bearer " + LOCAL},
        )
        assert oversized.status_code == 413
