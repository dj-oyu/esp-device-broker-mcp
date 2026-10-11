"""Logical work sessions and FIFO grants through actual localhost MCP HTTP."""

import asyncio

import pytest
from test_authenticated_mock import authenticated_mock as authenticated_mock
from test_http_integration import service as service
from test_registry_http import RegistryService
from test_registry_http import registry_service as registry_service

from cardputer_mcp import server as server_module
from cardputer_mcp.broker import Broker
from cardputer_mcp.client import call, connect

LOCAL = "a" * 64
REMOTE = "b" * 64


async def test_new_work_session_tools_always_require_authentication(service: str) -> None:
    async with connect(service) as client:
        assert (await call(client, "device_status"))["ok"]
        requests = {
            "create_work_session": {"owner": "local_cli"},
            "resume_work_session": {"resume_token": LOCAL},
            "request_device": {"resume_token": LOCAL},
            "cancel_device_request": {"resume_token": LOCAL, "request_id": "synthetic"},
            "close_work_session": {"resume_token": LOCAL},
        }
        for tool, request in requests.items():
            denied = await call(client, tool, request)
            assert denied == {"ok": False, "error": "authentication_required"}, tool


async def test_work_session_reconnect_fifo_and_separate_capabilities(
    authenticated_mock: tuple[str, Broker],
) -> None:
    url, resident = authenticated_mock
    async with connect(url, LOCAL) as local, connect(url, REMOTE) as remote:
        spoof = await call(remote, "create_work_session", {"owner": "local_cli"})
        assert spoof["error"] == "authenticated_owner_mismatch"
        first = await call(local, "create_work_session", {"owner": "local_cli"})
        second = await call(remote, "create_work_session", {"owner": "remote_mcp"})
        assert first["ok"] and second["ok"]
        assert resident.session is None
        assert first["lease"] is None and second["lease"] is None
        first_resume, second_resume = first["resume_token"], second["resume_token"]
        denied = await call(remote, "resume_work_session", {"resume_token": first_resume})
        assert not denied["ok"]
        granted = await call(local, "request_device", {"resume_token": first_resume})
        assert granted["ok"] and granted["lease"] is not None
        physical = granted["lease"]["session"]
        assert physical != first_resume
        invalid = await call(
            local, "prepare_test", {"session": first_resume, "job_id": "wrong-capability"}
        )
        assert invalid["error"] == "invalid_session"
        waiting = await call(remote, "request_device", {"resume_token": second_resume})
        assert waiting["ok"] and waiting["lease"] is None
        assert waiting["request"]["state"] == "waiting"
        repeated = await call(remote, "request_device", {"resume_token": second_resume})
        assert repeated["request"]["request_id"] == waiting["request"]["request_id"]
        assert len(resident.waiters) == 1
    # Disconnect is not close/cancel. A fresh MCP connection resumes the same capabilities.
    async with connect(url, LOCAL) as local, connect(url, REMOTE) as remote:
        resumed = await call(local, "resume_work_session", {"resume_token": first_resume})
        assert resumed["lease"]["session"] == physical
        assert (await call(local, "release_session", {"session": physical}))["released"]
        async with asyncio.timeout(5):
            while resident.session is None or resident.session.owner != "remote_mcp":
                await asyncio.sleep(0.01)
        resumed = await call(remote, "resume_work_session", {"resume_token": second_resume})
        assert resumed["lease"] is not None
        assert resumed["lease"]["session"] not in (physical, second_resume)
        assert resumed["request"]["state"] == "owning"
        assert (await call(remote, "close_work_session", {"resume_token": second_resume}))["ok"]
        assert (await call(local, "close_work_session", {"resume_token": first_resume}))["ok"]
        assert resident.session is None


async def test_work_session_scope_and_same_origin_key_isolation(
    registry_service: RegistryService, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = registry_service
    monkeypatch.setattr(server_module, "time", lambda: service.now[0])
    async with (
        connect(service.url, service.keys["first"].token) as owner,
        connect(service.url, service.keys["second"].token) as other,
        connect(service.url, service.keys["reader"].token) as reader,
    ):
        denied = await call(reader, "create_work_session", {"owner": "remote_mcp"})
        assert denied["error"] == "insufficient_scope"
        work = await call(owner, "create_work_session", {"owner": "remote_mcp"})
        resume = work["resume_token"]
        for tool, fields in {
            "resume_work_session": {},
            "request_device": {},
            "cancel_device_request": {"request_id": "unknown"},
            "close_work_session": {},
        }.items():
            denied = await call(other, tool, {"resume_token": resume, **fields})
            assert not denied["ok"], tool
        for tool in ("request_device", "close_work_session"):
            denied = await call(reader, tool, {"resume_token": resume})
            assert denied["error"] == "insufficient_scope"
        assert (await call(owner, "close_work_session", {"resume_token": resume}))["ok"]


@pytest.mark.parametrize("invalidation", ["revoke", "expire"])
async def test_fifo_auto_grant_rechecks_registry_authorization(
    registry_service: RegistryService, monkeypatch: pytest.MonkeyPatch, invalidation: str
) -> None:
    service = registry_service
    monkeypatch.setattr(server_module, "time", lambda: service.now[0])
    async with (
        connect(service.url, LOCAL) as local,
        connect(service.url, service.keys["first"].token) as first,
        connect(service.url, service.keys["second"].token) as second,
    ):
        held = await call(local, "acquire_session", {"owner": "local_cli"})
        first_work = await call(first, "create_work_session", {"owner": "remote_mcp"})
        second_work = await call(second, "create_work_session", {"owner": "remote_mcp"})
        await call(first, "request_device", {"resume_token": first_work["resume_token"]})
        await call(second, "request_device", {"resume_token": second_work["resume_token"]})
        assert len(service.resident.waiters) == 2
        if invalidation == "revoke":
            assert service.registry.revoke(service.keys["first"].metadata.key_id)
        else:
            service.now[0] += 11
        assert (await call(local, "release_session", {"session": held["session"]}))["released"]
        async with asyncio.timeout(5):
            while (
                service.resident.session is None
                or service.resident.session.principal_id
                != service.keys["second"].metadata.principal_id
            ):
                await asyncio.sleep(0.01)
        granted = await call(
            second, "resume_work_session", {"resume_token": second_work["resume_token"]}
        )
        assert granted["lease"] is not None
        assert len(service.resident.waiters) == 0
        assert (
            await call(second, "close_work_session", {"resume_token": second_work["resume_token"]})
        )["ok"]
