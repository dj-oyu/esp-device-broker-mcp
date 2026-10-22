"""Actual localhost MCP HTTP with hash-only registries and synthetic key material."""

import asyncio
import itertools
import os
import shutil
import socket
import tempfile
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

import httpx2
import pytest
import uvicorn

from cardputer_mcp import server as server_module
from cardputer_mcp.auth import AuthState
from cardputer_mcp.backend import MockBackend
from cardputer_mcp.broker import Broker
from cardputer_mcp.client import call, connect
from cardputer_mcp.config import Settings
from cardputer_mcp.key_registry import IssuedKey, KeyRegistry, KeyRegistryError
from cardputer_mcp.models import Origin
from cardputer_mcp.server import create_app

LOCAL = "a" * 64
REMOTE = "b" * 64


@dataclass
class RegistryService:
    url: str
    resident: Broker
    backend: MockBackend
    registry: KeyRegistry
    keys: dict[str, IssuedKey]
    now: list[float]


@pytest.fixture
async def registry_service(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> AsyncIterator[RegistryService]:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    now = [1000.0]
    monkeypatch.setattr(server_module, "time", lambda: now[0])
    sequence = itertools.count(1)
    path = tmp_path / "keys.sqlite3"
    if os.name == "nt" and getattr(request, "param", None) == "automatic":
        directory = tempfile.mkdtemp(prefix="broker-registry-http-", dir=os.environ["LOCALAPPDATA"])
        request.addfinalizer(lambda: shutil.rmtree(directory))
        path = Path(directory) / "keys.sqlite3"
    if getattr(request, "param", None) == "automatic":
        from test_registry_defaults import private_directory

        private_directory(path.parent)
    registry = KeyRegistry(
        path, clock=lambda: now[0], random_bytes=lambda size: bytes([next(sequence)]) * size
    )
    keys = {
        "first": registry.issue("First", Origin.REMOTE, scopes={"control"}, ttl_seconds=10),
        "second": registry.issue("Second", Origin.REMOTE, scopes={"control"}),
        "reader": registry.issue("Reader", Origin.REMOTE, scopes={"read"}),
    }
    monkeypatch.setattr(
        server_module,
        "KeyRegistry",
        lambda path, *, create: KeyRegistry(path, clock=lambda: now[0], create=create),
    )
    settings = Settings(
        state_dir=tmp_path / "state", port=port, allow_mock_execution=True, mock_stage_seconds=0.001
    )
    backend = MockBackend(0.001)
    resident = Broker(settings, backend=backend, lock_path=tmp_path / "device.lockfile")
    auth = AuthState({Origin.LOCAL: LOCAL, Origin.REMOTE: REMOTE})
    # Registry presence alone requires auth, even without --require-auth.
    if getattr(request, "param", None) == "automatic":
        monkeypatch.setattr(server_module, "default_key_registry_path", lambda: path)
        monkeypatch.setattr(
            "cardputer_mcp.registry_defaults.KeyRegistry",
            lambda path, *, create: KeyRegistry(path, clock=lambda: now[0], create=create),
        )
        app = create_app(settings, resident, auth=auth, require_auth=True)
    else:
        app = create_app(settings, resident, auth=auth, key_registry_path=path)
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
    task = asyncio.create_task(server.serve())
    try:
        async with asyncio.timeout(5):
            while not server.started:
                if task.done():
                    await task
                    pytest.fail("Registry HTTP server stopped before readiness")
                await asyncio.sleep(0.01)
        yield RegistryService(
            f"http://127.0.0.1:{port}/mcp", resident, backend, registry, keys, now
        )
    finally:
        if backend.gate is not None:
            backend.gate.set()
        server.should_exit = True
        await asyncio.wait_for(task, timeout=5)
        registry.close()


async def test_registry_read_scopes_and_unique_principal_binding(
    registry_service: RegistryService,
) -> None:
    service = registry_service
    first = service.keys["first"]
    second = service.keys["second"]
    reader = service.keys["reader"]
    async with (
        connect(service.url, first.token) as owner,
        connect(service.url, second.token) as stranger,
        connect(service.url, reader.token) as observer,
    ):
        status = await call(observer, "device_status")
        logs = await call(observer, "read_logs", {})
        job = await call(observer, "job_status", {"job_id": "missing"})
        assert status["ok"] and logs["ok"]
        assert job["error"] == "job_not_found"
        denied = await call(observer, "acquire_session", {"owner": "remote_mcp"})
        assert denied == {"ok": False, "error": "insufficient_scope"}
        spoof = await call(owner, "acquire_session", {"owner": "local_cli"})
        assert spoof["error"] == "authenticated_owner_mismatch"
        acquired = await call(owner, "acquire_session", {"owner": "remote_mcp"})
        session = acquired["session"]
        prepared = await call(
            owner, "prepare_test", {"session": session, "job_id": "registry-control"}
        )
        assert prepared["ok"]
        actions = {
            "renew_session": {},
            "release_session": {},
            "prepare_test": {"job_id": "unauthorized"},
            "execute_test": {"plan_hash": prepared["plan_hash"]},
            "cancel_job": {"job_id": "registry-control"},
        }
        for tool, fields in actions.items():
            stolen = await call(stranger, tool, {"session": session, **fields})
            assert stolen == {"ok": False, "error": "authenticated_owner_mismatch"}, tool
            denied = await call(observer, tool, {"session": session, **fields})
            assert denied == {"ok": False, "error": "insufficient_scope"}, tool
        assert (await call(owner, "renew_session", {"session": session}))["renewed"]
        result = await call(
            owner, "execute_test", {"session": session, "plan_hash": prepared["plan_hash"]}
        )
        assert result["ok"]
        await service.resident.queue.join()
        job = await call(observer, "job_status", {"job_id": "registry-control"})
        assert job["state"] == "succeeded"
        status = await call(observer, "device_status")
        logs = await call(observer, "read_logs", {})
        public = str(status) + str(logs) + str(job)
        for key in service.keys.values():
            assert key.token not in public
            assert key.metadata.principal_id not in public
            assert key.metadata.key_id not in public
        assert session not in public
        assert (await call(owner, "release_session", {"session": session}))["released"]


async def test_registry_and_ephemeral_credentials_coexist_without_session_theft(
    registry_service: RegistryService,
) -> None:
    service = registry_service
    key = service.keys["first"]
    async with (
        connect(service.url, LOCAL) as local,
        connect(service.url, REMOTE) as remote,
        connect(service.url, key.token) as key_client,
    ):
        assert (await call(local, "device_status"))["ok"]
        acquired = await call(remote, "acquire_session", {"owner": "remote_mcp"})
        session = acquired["session"]
        assert service.resident.session is not None
        assert service.resident.session.principal_id == "ephemeral:remote_mcp"
        denied = await call(key_client, "release_session", {"session": session})
        assert denied["error"] == "authenticated_owner_mismatch"
        assert (await call(remote, "renew_session", {"session": session}))["renewed"]
        assert (await call(remote, "release_session", {"session": session}))["released"]
        acquired = await call(key_client, "acquire_session", {"owner": "remote_mcp"})
        session = acquired["session"]
        assert service.resident.session is not None
        assert service.resident.session.principal_id == key.metadata.principal_id
        denied = await call(remote, "release_session", {"session": session})
        assert denied["error"] == "authenticated_owner_mismatch"
        assert (await call(key_client, "release_session", {"session": session}))["released"]
    async with httpx2.AsyncClient(trust_env=False) as http:
        assert (await http.post(service.url, json={})).status_code == 401


@pytest.mark.parametrize("invalidate", ["revoke", "expire"])
async def test_registry_invalidation_blocks_new_calls_without_cancelling_active_job(
    registry_service: RegistryService, invalidate: str
) -> None:
    service = registry_service
    key = service.keys["first"]
    backend = service.backend
    backend.gate = asyncio.Event()
    async with connect(service.url, key.token) as owner:
        acquired = await call(owner, "acquire_session", {"owner": "remote_mcp"})
        session = acquired["session"]
        prepared = await call(owner, "prepare_test", {"session": session, "job_id": "active"})
        executed = await call(
            owner, "execute_test", {"session": session, "plan_hash": prepared["plan_hash"]}
        )
        assert executed["ok"]
        async with asyncio.timeout(5):
            while backend.active != 1:
                await asyncio.sleep(0.01)
        if invalidate == "revoke":
            assert service.registry.revoke(key.metadata.key_id)
        else:
            service.now[0] += 11
        async with httpx2.AsyncClient(trust_env=False) as http:
            rejected = await http.post(
                service.url,
                json={},
                headers={"Authorization": f"Bearer {key.token}"},
            )
            assert rejected.status_code == 401
            assert rejected.json() == {"error": "authentication_required"}
        async with connect(service.url, service.keys["second"].token) as other:
            job = await call(other, "job_status", {"job_id": "active"})
            assert job["state"] == "running"
            assert backend.active == 1
            busy = await call(other, "acquire_session", {"owner": "remote_mcp"})
            assert busy["error"] == "device_busy"
            stolen = await call(other, "cancel_job", {"session": session, "job_id": "active"})
            assert stolen["error"] in {
                "authenticated_owner_mismatch",
                "lease_expired_active_job_retains_ownership",
            }
            assert service.resident.session is not None
            assert service.resident.session.principal_id == key.metadata.principal_id
            backend.gate.set()
            await service.resident.queue.join()
            job = await call(other, "job_status", {"job_id": "active"})
            assert job["state"] == "succeeded"
            assert backend.calls == 1 and backend.active == 0
            # Revocation expires the lease, but ownership ended only after the job closed.
            assert service.resident.session is None


async def test_server_requires_existing_registry_without_creating_one(tmp_path: Path) -> None:
    settings = Settings(state_dir=tmp_path / "state")
    resident = Broker(settings, lock_path=tmp_path / "device.lockfile")
    path = tmp_path / "does-not-exist.sqlite3"
    auth = AuthState({Origin.LOCAL: LOCAL, Origin.REMOTE: REMOTE})
    app = create_app(settings, resident, auth=auth, key_registry_path=path)
    inner = app
    while not hasattr(inner, "router"):
        inner = inner.app
    with pytest.raises(KeyRegistryError):
        async with inner.router.lifespan_context(inner):
            pytest.fail("Server accepted nonexistent registry")
    assert not path.exists()
    assert resident.store is None
