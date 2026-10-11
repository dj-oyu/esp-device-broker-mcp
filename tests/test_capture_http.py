"""Real localhost MCP transport, synthetic keys and images; no USB access."""

import asyncio
import base64
import hashlib
import json
from typing import Any

import httpx2
import pytest
from mcp import Client
from mcp.types import CallToolResult, ImageContent, TextContent
from test_http_integration import service as service
from test_registry_http import LOCAL, RegistryService
from test_registry_http import registry_service as registry_service

from cardputer_mcp import server as server_module
from cardputer_mcp.capture import mock_image
from cardputer_mcp.client import call, connect


async def capture(client: Client, job_id: str, session: str) -> dict[str, Any]:
    prepared = await call(
        client,
        "prepare_test",
        {"session": session, "job_id": job_id, "test": "capture_home"},
    )
    assert prepared["ok"]
    executed = await call(
        client, "execute_test", {"session": session, "plan_hash": prepared["plan_hash"]}
    )
    assert executed["ok"]
    return prepared


async def read_capture(client: Client, job_id: str) -> CallToolResult:
    result = await client.call_tool("read_capture", {"request": {"job_id": job_id}})
    assert isinstance(result, CallToolResult)
    return result


async def approve_image(service: RegistryService, job_id: str) -> dict[str, Any]:
    """Use the real, local-only operator route before a remote image read."""
    base = service.url.removesuffix("/mcp") + "/operator"
    async with httpx2.AsyncClient(
        trust_env=False, headers={"Authorization": f"Bearer {LOCAL}"}
    ) as http:
        response = await http.post(base + "/image", json={"job_id": job_id})
        assert response.status_code == 200
        preview = response.json()
        assert preview["ok"] and preview["delivery_state"] == "pending"
        response = await http.post(
            base + "/image-decision",
            json={
                "job_id": job_id,
                "sha256": preview["sha256"],
                "delivery_revision": preview["delivery_revision"],
                "decision": "approve",
            },
        )
        assert response.status_code == 200
        assert response.json() == {"ok": True, "job_id": job_id, "delivery_state": "approved"}
        return preview


def assert_unavailable(result: CallToolResult, error: str = "capture_not_available") -> None:
    assert result.is_error
    assert result.structured_content == {"ok": False, "error": error}
    assert len(result.content) == 1
    assert isinstance(result.content[0], TextContent)
    assert json.loads(result.content[0].text) == result.structured_content


def assert_image(result: CallToolResult) -> bytes:
    assert not result.is_error
    assert len(result.content) == 2
    text, image = result.content
    assert isinstance(text, TextContent)
    assert isinstance(image, ImageContent)
    assert image.mime_type == "image/png"
    pixels = base64.b64decode(image.data, validate=True)
    assert pixels.startswith(b"\x89PNG\r\n\x1a\n")
    assert pixels == mock_image().png
    metadata = result.structured_content
    assert metadata is not None
    assert json.loads(text.text) == metadata
    assert metadata["ok"]
    assert metadata["sha256"] == hashlib.sha256(pixels).hexdigest()
    assert metadata["size_bytes"] == len(pixels)
    assert (metadata["width"], metadata["height"]) == (240, 135)
    assert metadata["source"] == "synthetic_fixture"
    assert metadata["label"] == "TEST / NOT DEVICE"
    assert metadata["is_device_capture"] is False
    assert metadata["coherent_frame"] == "unknown"
    assert metadata["lcd_transfer_ack"] == "unknown"
    return pixels


async def test_capture_http_delivers_image_content_hash_and_principal_isolation(
    registry_service: RegistryService, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = registry_service
    monkeypatch.setattr(server_module, "time", lambda: service.now[0])
    async with (
        connect(service.url, service.keys["first"].token) as owner,
        connect(service.url, service.keys["second"].token) as stranger,
        connect(service.url, service.keys["reader"].token) as reader,
    ):
        advertised = await owner.list_tools()
        tool = next(tool for tool in advertised.tools if tool.name == "read_capture")
        assert tool.annotations is not None and tool.annotations.read_only_hint
        acquired = await call(owner, "acquire_session", {"owner": "remote_mcp"})
        session = acquired["session"]
        await capture(owner, "image-content", session)
        await service.resident.queue.join()
        assert_unavailable(
            await read_capture(owner, "image-content"), "capture_delivery_approval_required"
        )
        await approve_image(service, "image-content")
        result = await read_capture(owner, "image-content")
        pixels = assert_image(result)
        assert_unavailable(await read_capture(stranger, "image-content"))
        assert_unavailable(await read_capture(reader, "image-content"))
        assert_unavailable(await read_capture(stranger, "nonexistent"))
        public = [
            await call(reader, "job_status", {"job_id": "image-content"}),
            await call(reader, "read_logs", {}),
            await call(reader, "device_status"),
        ]
        assert public[0]["state"] == "succeeded"
        serialized = json.dumps(public)
        for forbidden in (base64.b64encode(pixels).decode("ascii"), session):
            assert forbidden not in serialized
        for key in service.keys.values():
            assert key.token not in serialized
            assert key.metadata.principal_id not in serialized
        assert service.backend.calls == 1


@pytest.mark.parametrize("invalidation", ["revoke", "expire"])
async def test_capture_http_rechecks_credential_after_success(
    registry_service: RegistryService, invalidation: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = registry_service
    monkeypatch.setattr(server_module, "time", lambda: service.now[0])
    key = service.keys["first"]
    async with connect(service.url, key.token) as owner:
        acquired = await call(owner, "acquire_session", {"owner": "remote_mcp"})
        await capture(owner, "credential-changed", acquired["session"])
        await service.resident.queue.join()
        assert_unavailable(
            await read_capture(owner, "credential-changed"), "capture_delivery_approval_required"
        )
        await approve_image(service, "credential-changed")
        assert_image(await read_capture(owner, "credential-changed"))
        if invalidation == "revoke":
            assert service.registry.revoke(key.metadata.key_id)
        else:
            service.now[0] += 11
        async with httpx2.AsyncClient(trust_env=False) as http:
            for _ in range(2):
                result = await http.post(
                    service.url,
                    headers={"Authorization": f"Bearer {key.token}"},
                    json={
                        "jsonrpc": "2.0",
                        "id": "capture-read",
                        "method": "tools/call",
                        "params": {
                            "name": "read_capture",
                            "arguments": {"request": {"job_id": "credential-changed"}},
                        },
                    },
                )
                assert result.status_code == 401
                assert result.json() == {"error": "authentication_required"}
        async with connect(service.url, service.keys["second"].token) as stranger:
            assert_unavailable(await read_capture(stranger, "credential-changed"))
        assert service.backend.calls == 1
        assert service.resident.job("credential-changed")["state"] == "succeeded"


async def test_capture_http_reconnect_release_and_expiry_never_recaptures(
    registry_service: RegistryService, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = registry_service
    monkeypatch.setattr(server_module, "time", lambda: service.now[0])
    now = [100.0]
    service.resident.clock = lambda: now[0]
    key = service.keys["second"]
    async with connect(service.url, key.token) as owner:
        acquired = await call(owner, "acquire_session", {"owner": "remote_mcp"})
        session = acquired["session"]
        plan = await capture(owner, "reconnect", session)
    await service.resident.queue.join()
    async with connect(service.url, key.token) as reconnected:
        repeated = await call(
            reconnected, "execute_test", {"session": session, "plan_hash": plan["plan_hash"]}
        )
        assert repeated["deduplicated"]
        assert_unavailable(
            await read_capture(reconnected, "reconnect"), "capture_delivery_approval_required"
        )
        await approve_image(service, "reconnect")
        original = assert_image(await read_capture(reconnected, "reconnect"))
        assert (await call(reconnected, "release_session", {"session": session}))["released"]
    async with connect(service.url, key.token) as later:
        now[0] = 399.999
        assert assert_image(await read_capture(later, "reconnect")) == original
        now[0] = 400.0
        assert_unavailable(await read_capture(later, "reconnect"))
        assert_unavailable(await read_capture(later, "reconnect"))
        assert (await call(later, "job_status", {"job_id": "reconnect"}))["state"] == "succeeded"
        assert service.backend.calls == 1


async def test_legacy_mock_capture_reads_and_preparation_still_require_authentication(
    service: str,
) -> None:
    async with connect(service) as anonymous:
        acquired = await call(anonymous, "acquire_session", {"owner": "local_cli"})
        assert acquired["ok"]
        prepared = await call(
            anonymous,
            "prepare_test",
            {"session": acquired["session"], "job_id": "anonymous", "test": "capture_home"},
        )
        assert prepared == {"ok": False, "error": "authentication_required"}
        assert_unavailable(await read_capture(anonymous, "anonymous"), "authentication_required")


async def test_three_clients_share_fifo_and_capture_by_key_without_duplicate_reconnect(
    registry_service: RegistryService, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = registry_service
    # The registry fixture has an explicit clock; FIFO grant checks must use it too.
    monkeypatch.setattr(server_module, "time", lambda: service.now[0])
    shared_key = service.keys["first"].token
    cloud_key = service.keys["second"].token
    backend = service.backend
    backend.gate = asyncio.Event()
    async with connect(service.url, shared_key) as codex, connect(service.url, cloud_key) as cloud:
        async with connect(service.url, shared_key) as claude:
            claude_work = await call(claude, "create_work_session", {"owner": "remote_mcp"})
            codex_work = await call(codex, "create_work_session", {"owner": "remote_mcp"})
            cloud_work = await call(cloud, "create_work_session", {"owner": "remote_mcp"})
            assert (
                len({work["work_session_id"] for work in (claude_work, codex_work, cloud_work)})
                == 3
            )
            claude_state = await call(
                claude, "request_device", {"resume_token": claude_work["resume_token"]}
            )
            codex_state = await call(
                codex, "request_device", {"resume_token": codex_work["resume_token"]}
            )
            cloud_state = await call(
                cloud, "request_device", {"resume_token": cloud_work["resume_token"]}
            )
            assert claude_state["request"]["state"] == "owning"
            assert codex_state["request"]["position"] == 1
            assert cloud_state["request"]["position"] == 2
            session = claude_state["lease"]["session"]
            plan = await capture(claude, "three-clients", session)
            async with asyncio.timeout(2):
                while backend.active != 1:
                    await asyncio.sleep(0.001)
            assert_unavailable(await read_capture(codex, "three-clients"))
        # Claude's transport goes away while its one resident job keeps running.
        assert backend.active == 1
        backend.gate.set()
        await service.resident.queue.join()
        async with connect(service.url, shared_key) as claude_again:
            resumed = await call(
                claude_again,
                "resume_work_session",
                {"resume_token": claude_work["resume_token"]},
            )
            assert resumed["lease"]["session"] == session
            repeated = await call(
                claude_again,
                "execute_test",
                {"session": session, "plan_hash": plan["plan_hash"]},
            )
            assert repeated["deduplicated"]
            assert_unavailable(
                await read_capture(claude_again, "three-clients"),
                "capture_delivery_approval_required",
            )
            await approve_image(service, "three-clients")
            assert_image(await read_capture(claude_again, "three-clients"))
            assert_image(await read_capture(codex, "three-clients"))
            assert_unavailable(await read_capture(cloud, "three-clients"))
            assert (await call(claude_again, "release_session", {"session": session}))["released"]
        codex_state = await call(
            codex, "resume_work_session", {"resume_token": codex_work["resume_token"]}
        )
        cloud_state = await call(
            cloud, "resume_work_session", {"resume_token": cloud_work["resume_token"]}
        )
        assert codex_state["request"]["state"] == "owning"
        assert cloud_state["request"]["position"] == 1
        assert_image(await read_capture(codex, "three-clients"))
        assert (await call(codex, "release_session", {"session": codex_state["lease"]["session"]}))[
            "released"
        ]
        cloud_state = await call(
            cloud, "resume_work_session", {"resume_token": cloud_work["resume_token"]}
        )
        assert cloud_state["request"]["state"] == "owning"
        assert_unavailable(await read_capture(cloud, "three-clients"))
        assert backend.calls == 1
        assert backend.max_active == 1
