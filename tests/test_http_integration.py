"""Actual localhost TCP + official SDK client, not in-process tool calls."""

import asyncio
import os
import signal
import socket
import sys

import httpx2
import pytest

from cardputer_mcp.client import call, connect


@pytest.fixture
async def service(tmp_path):
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
        "serve",
        "--config",
        str(config),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
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


async def test_real_mcp_http_disconnect_retry_handover(service):
    async with connect(service) as first, connect(service) as second:
        advertised = await first.list_tools()
        names = {tool.name for tool in advertised.tools}
        assert names == {
            "client_identity",
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
            "cancel_job",
            "create_work_session",
            "resume_work_session",
            "request_device",
            "cancel_device_request",
            "close_work_session",
        }
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
    async with connect(service) as later:
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
        acquired = await call(later, "acquire_session", {"owner": "remote_mcp"})
        assert acquired["ok"]
        await call(later, "release_session", {"session": acquired["session"]})


async def test_real_http_invalid_origin_and_oversize(service):
    async with httpx2.AsyncClient(trust_env=False) as http:
        evil = await http.post(service, json={}, headers={"Origin": "https://hostile.invalid"})
        assert evil.status_code == 403
        oversized = await http.post(
            service, content=" " * 20000, headers={"content-type": "application/json"}
        )
        assert oversized.status_code == 413
