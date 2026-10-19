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
        assert names == {
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
        }
        build_logs = next(tool for tool in advertised.tools if tool.name == "read_build_logs")
        assert build_logs.annotations.read_only_hint is True
        tool_data = build_logs.model_dump()
        schema = tool_data.get("input_schema", tool_data.get("inputSchema"))
        build_request = schema["$defs"]["BuildLogRequest"]
        assert set(build_request["properties"]) == {
            "job_id",
            "include_source_cache",
            "cursor",
            "levels",
            "context",
            "limit",
        }
        assert build_request["additionalProperties"] is False
        resources = next(tool for tool in advertised.tools if tool.name == "read_source_resources")
        assert resources.annotations.read_only_hint is True
        assert resources.annotations.destructive_hint is False
        resource_schema = resources.input_schema["$defs"]["SourceResourcesRequest"]
        assert set(resource_schema["properties"]) == {"project_id"}
        assert resource_schema["additionalProperties"] is False
        for name, model, fields in (
            ("request_home_recovery", "RequestHomeRecovery", {"job_id", "source_job_id"}),
            ("cancel_home_recovery", "CancelHomeRecovery", {"job_id", "plan_hash"}),
        ):
            tool = next(tool for tool in advertised.tools if tool.name == name)
            assert tool.annotations.read_only_hint is False
            data = tool.model_dump()
            shape = data.get("input_schema", data.get("inputSchema"))["$defs"][model]
            assert set(shape["properties"]) == fields and shape["additionalProperties"] is False
        acquired = await call(first, "acquire_session", {"owner": "local_cli"})
        token = acquired["session"]
        busy = await call(second, "acquire_session", {"owner": "remote_mcp"})
        assert busy["error"] == "device_busy"
        prepared = await call(
            first,
            "prepare_test",
            {"session": token, "job_id": "http-reconnect", "test": "firmware_workflow"},
        )
        executed = await call(
            first, "execute_test", {"session": token, "plan_hash": prepared["plan_hash"]}
        )
        assert executed["state"] in ("queued", "running")
    # Transport disconnect is not device-workflow cancellation.
    async with connect(service, LOCAL) as later:
        async with asyncio.timeout(5):
            while True:
                finished = await call(later, "job_status", {"job_id": "http-reconnect"})
                if finished["state"] not in ("queued", "running"):
                    break
                await asyncio.sleep(0.02)
        assert finished["state"] == "succeeded"
        repeated = await call(
            later, "execute_test", {"session": token, "plan_hash": prepared["plan_hash"]}
        )
        assert repeated["deduplicated"]
        assert repeated["id"] == finished["id"]
        status = await call(later, "device_status")
        assert status["owner"] == "local_cli"
        assert status["hardware_connected"] is False
        logs = await call(later, "read_logs", {})
        stages = [entry["stage"] for entry in logs["entries"] if entry["stage"]]
        assert stages == ["mock_flash", "mock_boot", "mock_test"]
        assert token not in str(status) + str(logs)
        await call(later, "release_session", {"session": token})
    async with connect(service, REMOTE) as remote:
        acquired = await call(remote, "acquire_session", {"owner": "remote_mcp"})
        assert acquired["ok"]
        await call(remote, "release_session", {"session": acquired["session"]})


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
