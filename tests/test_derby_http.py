"""Actual local approval and MCP/image boundaries with a synthetic serial port."""

import httpx2
from test_derby_smoke import Wire
from test_registry_http import registry_service as registry_service

from cardputer_mcp.client import call, connect
from cardputer_mcp.config import SerialSettings
from cardputer_mcp.ports import PortInfo
from cardputer_mcp.serial_backend import SerialBackend


async def test_fixed_smoke_local_approval_then_principal_bound_image(registry_service):
    service = registry_service
    key = service.keys["second"]
    wire = Wire()
    config = SerialSettings(
        selector={"vid": 1, "pid": 2, "serial_number": "synthetic"},
        expected_firmware_sha256="a" * 64,
    )
    backend = SerialBackend(
        config,
        inventory=lambda: [PortInfo(port="COM42", vid=1, pid=2, serial_number="synthetic")],
        factory=lambda: wire,
        clock=lambda: wire.now,
    )
    service.resident.backend = backend
    service.resident.settings = service.resident.settings.model_copy(
        update={"backend": "serial", "serial": config, "allow_mock_execution": False}
    )
    async with (
        httpx2.AsyncClient(
            base_url=service.url.removesuffix("/mcp"), trust_env=False,
            headers={"Authorization": "Bearer " + "a" * 64},
        ) as http,
        connect(service.url, key.token) as remote,
    ):
        lease = await call(remote, "acquire_session", {"owner": "remote_mcp", "lease_seconds": 300})
        prepared = await call(remote, "prepare_test", {
            "session": lease["session"], "job_id": "derby-one", "test": "derby_smoke",
        })
        assert prepared["ok"] and prepared["approval_required"], prepared
        assert not wire.writes and not wire.is_open
        execution = {"session": lease["session"], "plan_hash": prepared["plan_hash"]}
        denied = await call(remote, "execute_test", execution)
        assert denied["error"] == "operator_approval_required"
        detail = (await http.post("/operator/plan", json={"plan_hash": prepared["plan_hash"]})).json()
        approved = await http.post("/operator/approve-plan", json={
            "plan_hash": prepared["plan_hash"], "review_revision": detail["review_revision"],
            "attestation": "DEVICE HOME AND FIRMWARE VERIFIED",
        })
        assert approved.status_code == 200 and approved.json()["ok"], approved.text
        result = await call(remote, "execute_test", execution)
        assert result["ok"], result
        await service.resident.queue.join()
        job = service.resident.job("derby-one")
        assert job["state"] == "succeeded", job
        assert job["result"] == "derby_demo_cycle_observed_render_buffer_lcd_unverified"
        assert wire.closed and wire.writes[-2:] == [b"s", b"q"]
        preview = service.resident.operator_image("derby-one")
        assert preview["delivery_state"] == "pending"
        denied_image = await call(remote, "read_capture", {"job_id": "derby-one"})
        assert denied_image["error"] == "capture_delivery_approval_required"
        service.resident.operator_decide_image(
            "derby-one", preview["sha256"], preview["delivery_revision"], "approve"
        )
        assert service.resident.read_capture("derby-one", key.metadata.principal_id).png
