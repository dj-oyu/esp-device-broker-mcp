"""Real HTTP and serial backend code; serial I/O and credential values are synthetic."""

import asyncio
import socket

import httpx2
import pytest
import uvicorn
from test_serial_backend import backend, config

from cardputer_mcp.auth import AuthState
from cardputer_mcp.broker import Broker
from cardputer_mcp.client import call, connect
from cardputer_mcp.config import Settings
from cardputer_mcp.models import Origin
from cardputer_mcp.server import create_app


@pytest.fixture
async def serial_service(tmp_path):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port_number = listener.getsockname()[1]
    settings = Settings(backend="serial", serial=config(), state_dir=tmp_path, port=port_number)
    device, port = backend(split=3)
    resident = Broker(
        settings, backend=device, serial_enabled=True, lock_path=tmp_path / "device.lockfile"
    )
    auth = AuthState({Origin.LOCAL: "a" * 64, Origin.REMOTE: "b" * 64})
    app = create_app(settings, resident, serial_enabled=True, auth=auth)
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=port_number,
            log_level="critical",
            access_log=False,
            proxy_headers=False,
        )
    )
    task = asyncio.create_task(server.serve())
    try:
        async with asyncio.timeout(5):
            while not server.started:
                if task.done():
                    await task
                await asyncio.sleep(0.01)
        yield f"http://127.0.0.1:{port_number}/mcp", resident, port
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, timeout=5)


async def test_authenticated_serial_http_explicit_execution_and_owner_binding(serial_service):
    url, resident, port = serial_service
    async with httpx2.AsyncClient(trust_env=False) as http:
        rejected = await http.post(url, json={})
        assert rejected.status_code == 401
        rejected = await http.post(url, json={}, headers={"Authorization": "Bearer " + "c" * 64})
        assert rejected.status_code == 401
    assert port.opens == 0
    async with connect(url, "a" * 64) as local, connect(url, "b" * 64) as remote:
        status = await call(local, "status", {"view": "device"})
        assert status["backend"] == "serial" and not status["hardware_connected"]
        spoof = await call(
            remote, "session", {"action": "open", "owner": "local_cli", "lease_seconds": 30}
        )
        assert spoof["error"] == "authenticated_owner_mismatch"
        lease = await call(
            local, "session", {"action": "open", "owner": "local_cli", "lease_seconds": 30}
        )
        token = lease["session"]
        prepared = await call(
            local,
            "submit",
            {"kind": "hello", "session": token, "job_id": "serial-http"},
        )
        assert prepared["ok"] and not prepared["approval_required"]
        assert port.opens == 0
        stolen = await call(
            remote,
            "execute",
            {"op": "run", "session": token, "plan_hash": prepared["plan_hash"]},
        )
        assert stolen["error"] == "authenticated_owner_mismatch"
        assert port.opens == 0
        executed = await call(
            local,
            "execute",
            {"op": "run", "session": token, "plan_hash": prepared["plan_hash"]},
        )
        assert executed["ok"]
        await resident.queue.join()
        job = await call(remote, "status", {"view": "job", "job_id": "serial-http"})
        assert job["state"] == "succeeded"
        assert job["result"] == "hello_markers_passed_physical_checks_not_assessed"
        assert port.opens == port.closes == 1
        logs = await call(remote, "status", {"view": "logs", "stream": "broker"})
        assert "serial_identity_hello" in str(logs)
        for credential in ("a" * 64, "b" * 64, token):
            assert credential not in str(logs) + str(status)
        await call(local, "session", {"action": "release", "session": token})
        other = await call(
            remote, "session", {"action": "open", "owner": "remote_mcp", "lease_seconds": 30}
        )
        assert other["ok"]
        await call(remote, "session", {"action": "release", "session": other["session"]})
