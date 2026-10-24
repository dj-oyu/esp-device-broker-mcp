"""Explicit authenticated execution and MCP/image boundaries with a synthetic serial port."""

import asyncio
import json
import threading
from types import SimpleNamespace

import httpx2
import pytest
from test_derby_smoke import Wire
from test_serial_http import serial_service as serial_service

from cardputer_mcp.client import call, connect
from cardputer_mcp.config import SerialSettings
from cardputer_mcp.ports import PortInfo
from cardputer_mcp.serial_backend import SerialBackend


@pytest.fixture
async def derby_service(serial_service):
    """Exercise actual HTTP authentication without a filesystem key registry."""
    url, resident, _ = serial_service
    return SimpleNamespace(
        url=url,
        resident=resident,
        keys={
            "second": SimpleNamespace(
                token="b" * 64,
                metadata=SimpleNamespace(principal_id="ephemeral:remote_mcp"),
            ),
            "local": SimpleNamespace(
                token="a" * 64,
                metadata=SimpleNamespace(principal_id="ephemeral:local_cli"),
            ),
        },
    )


@pytest.mark.parametrize("interruption", [None, "no_photo", "cancel", "lease_expired"])
async def test_fixed_smoke_explicit_request_then_principal_bound_image(derby_service, interruption):
    service = derby_service
    key = service.keys["second"]
    wire = Wire(phases=interruption != "no_photo")
    entered = threading.Event()
    release = threading.Event()
    read = wire.read

    def gated_read(size=1):
        if interruption == "no_photo" and wire.running and not wire.queue:
            # Advance only the synthetic idle clock; avoid thousands of real
            # authenticated thread/loop round trips for a 120-second fake wait.
            wire.now += 4.9
        if interruption in ("cancel", "lease_expired") and wire.running:
            entered.set()
            if not release.wait(5):
                raise TimeoutError
        return read(size)

    wire.read = gated_read
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
            base_url=service.url.removesuffix("/mcp"),
            trust_env=False,
            headers={"Authorization": "Bearer " + "a" * 64},
        ) as _http,
        connect(service.url, key.token) as remote,
    ):
        catalog = await remote.list_tools()
        preparation = next(tool for tool in catalog.tools if tool.name == "submit")
        assert "derby" in json.dumps(preparation.input_schema)
        lease = await call(
            remote, "session", {"action": "open", "owner": "remote_mcp", "lease_seconds": 300}
        )
        prepared = await call(
            remote, "submit", {"session": lease["session"], "job_id": "derby-one", "kind": "derby"}
        )
        assert prepared["ok"] and not prepared["approval_required"], prepared
        assert not wire.writes and not wire.is_open
        execution = {"session": lease["session"], "plan_hash": prepared["plan_hash"]}
        result = await call(remote, "execute", {"op": "run", **execution})
        assert result["ok"], result
        if interruption in ("cancel", "lease_expired"):
            assert await asyncio.to_thread(entered.wait, 3)
            if interruption == "cancel":
                cancelled = await call(
                    remote,
                    "cancel",
                    {"target": "job", "session": lease["session"], "job_id": "derby-one"},
                )
                assert cancelled["stop_requested"]
            else:
                service.resident.session.deadline = service.resident.clock() - 1
            release.set()
        await service.resident.queue.join()
        # Running-job lease fix: passing the lease deadline while the derby runs no
        # longer revokes it (it was needs_review); it completes like the normal case.
        if interruption == "cancel":
            job = service.resident.job("derby-one")
            assert job["state"] == "needs_review", json.dumps(job)
            assert wire.closed and wire.writes[-1] == b"e"
            assert "derby-one" not in service.resident.captures
            return
        job = service.resident.job("derby-one")
        assert job["state"] == "succeeded", json.dumps(job, sort_keys=True)
        report = job["diagnostic"]["derby_observation"]
        assert report["exit_home_observed"] is True and report["photo_pixels_verified"] is False
        if interruption == "no_photo":
            assert job["result"] == "derby_photo_not_verified_no_capture"
            assert report["image_acquired"] is False and report["demo_cycle_observed"] is False
            assert report["capture_reason"] == "photo_not_observed_before_deadline"
            assert wire.closed and wire.writes[-1] == b"q" and b"s" not in wire.writes
            assert "derby-one" not in service.resident.captures
            return
        assert report["demo_cycle_observed"] is True and report["image_acquired"] is True
        assert job["result"] == "derby_demo_cycle_observed_render_buffer_lcd_unverified"
        assert wire.closed and wire.writes[-2:] == [b"s", b"q"]
        preview = service.resident.operator_image("derby-one")
        assert preview["delivery_state"] == "available"
        captured = await remote.call_tool(
            "status", {"request": {"view": "capture", "job_id": "derby-one"}}
        )
        assert not captured.is_error and any(c.type == "image" for c in captured.content)
        assert service.resident.read_capture("derby-one", key.metadata.principal_id).png
