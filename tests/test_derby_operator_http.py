"""Formal CLI children and Windows pipe against synthetic serial and real HTTP auth."""

import asyncio
import hashlib
import os
import sys
from dataclasses import replace
from pathlib import Path

import pytest
from test_derby_smoke import Wire
from test_operator_launch_http import PythonCliProcess
from test_operator_launch_pipe import exchange
from test_registry_http import LOCAL
from test_registry_http import registry_service as registry_service

from cardputer_mcp.client import call, connect
from cardputer_mcp.config import SerialSettings
from cardputer_mcp.derby_operator_launch import DerbyLaunchScope, DerbyOperatorLaunchSlot
from cardputer_mcp.operator_launch import HOME_ATTESTATION
from cardputer_mcp.operator_launch_pipe import WindowsOperatorLaunchPipe, current_user_sid
from cardputer_mcp.ports import PortInfo
from cardputer_mcp.serial_backend import SerialBackend

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows same-user pipe and formal CLI")


@pytest.mark.parametrize(
    "change",
    [
        None,
        "cancel",
        "revoke",
        "expire",
        "renew",
        "epoch",
        "firmware",
        "device",
        "image_revoke",
        "image_expire",
        "image_revision",
        "image_review_epoch",
    ],
)
async def test_exact_derby_cli_execution_then_image_decision(registry_service, tmp_path, change):
    env = registry_service
    wire = Wire()
    config = SerialSettings(
        selector={"vid": 1, "pid": 2, "serial_number": "synthetic"},
        expected_firmware_sha256="a" * 64,
    )
    env.resident.backend = SerialBackend(
        config,
        inventory=lambda: [PortInfo(port="COM42", vid=1, pid=2, serial_number="synthetic")],
        factory=lambda: wire,
        clock=lambda: wire.now,
    )
    env.resident.settings = env.resident.settings.model_copy(
        update={"backend": "serial", "serial": config, "allow_mock_execution": False}
    )
    async with connect(env.url, env.keys["second"].token) as remote:
        lease = await call(remote, "acquire_session", {"owner": "remote_mcp", "lease_seconds": 300})
        prepared = await call(
            remote,
            "prepare_test",
            {"session": lease["session"], "job_id": "derby-operator-one", "test": "derby_smoke"},
        )
        assert prepared["ok"], prepared
        plan = prepared["plan"]
        scope = DerbyLaunchScope(
            broker_epoch=plan["broker_epoch"],
            job_id=plan["job_id"],
            plan_hash=prepared["plan_hash"],
            principal_id=env.keys["second"].metadata.principal_id,
            device_fingerprint=plan["device_fingerprint"],
            expected_firmware_sha256=plan["expected_firmware_sha256"],
        )
        submitted = await call(
            remote,
            "execute_test",
            {"session": lease["session"], "plan_hash": scope.plan_hash, "wait_for_approval": True},
        )
        assert submitted["state"] == "awaiting_approval" and not wire.writes
        exe = Path(sys.executable)
        exe_hash = hashlib.sha256(await asyncio.to_thread(exe.read_bytes)).hexdigest()

        def arm(phase):
            slot = DerbyOperatorLaunchSlot(
                exe,
                exe_hash,
                tmp_path,
                env.url,
                LOCAL,
                current_user_sid(),
                scope=scope,
                phase=phase,
                lifetime_seconds=60,
                process=PythonCliProcess(),
            )
            return slot, WindowsOperatorLaunchPipe(slot)

        slot, endpoint = arm("execution")
        try:
            review = await exchange(endpoint, {**scope.model_dump(), "action": "derby-plan-review"})
            assert review["ok"], review
            assert "q" in review["review"]["capture_notice"]
            assert not wire.writes
            if change == "cancel":
                await call(
                    remote, "cancel_job", {"session": lease["session"], "job_id": scope.job_id}
                )
            elif change == "revoke":
                env.registry.revoke(env.keys["second"].metadata.key_id)
            elif change == "expire":
                env.resident.session.deadline = env.resident.clock() - 1
            elif change == "renew":
                await call(
                    remote, "renew_session", {"session": lease["session"], "lease_seconds": 300}
                )
            elif change == "epoch":
                env.resident.epoch = "9" * 32
            elif change == "firmware":
                env.resident.backend.config = config.model_copy(
                    update={"expected_firmware_sha256": "9" * 64}
                )
            elif change == "device":
                env.resident.backend.inventory = lambda: [
                    PortInfo(port="COM42", vid=1, pid=2, serial_number="other-device")
                ]
            approved = await exchange(
                endpoint,
                {
                    **scope.model_dump(),
                    "action": "derby-approve-and-start",
                    "review_revision": review["review"]["review_revision"],
                    "attestation": HOME_ATTESTATION,
                },
            )
            assert slot.closed and slot._local_token is None
            if change and not change.startswith("image_"):
                assert not approved["ok"] and not wire.writes, approved
                return
            assert approved["ok"], approved
        finally:
            endpoint.close()
        await env.resident.queue.join()
        assert env.resident.job(scope.job_id)["state"] == "succeeded"
        assert wire.closed and wire.writes[-2:] == [b"s", b"q"]
        denied = await remote.call_tool("read_capture", {"request": {"job_id": scope.job_id}})
        assert denied.is_error
        slot, endpoint = arm("image")
        try:
            if change == "image_review_epoch":
                summary = env.resident._image_summary

                def changed_epoch(*args):
                    env.resident.epoch = "9" * 32
                    return summary(*args)

                env.resident._image_summary = changed_epoch
            image = await exchange(endpoint, {**scope.model_dump(), "action": "derby-image-review"})
            if change == "image_review_epoch":
                assert not image["ok"] and slot.closed
                return
            assert image["ok"] and "image_base64" not in image, image
            if change == "image_revoke":
                env.registry.revoke(env.keys["second"].metadata.key_id)
            elif change == "image_expire":
                retained = env.resident.captures[scope.job_id]
                env.resident.captures[scope.job_id] = replace(
                    retained, deadline=env.resident.clock() - 1
                )
            elif change == "image_revision":
                image["delivery_revision"] = "9" * 64
            delivered = await exchange(
                endpoint,
                {
                    **scope.model_dump(),
                    "action": "derby-image-decision",
                    "sha256": image["sha256"],
                    "delivery_revision": image["delivery_revision"],
                    "decision": "approve",
                },
            )
            assert slot.closed and slot._local_token is None
            if change and change.startswith("image_"):
                assert not delivered["ok"], delivered
                if scope.job_id in env.resident.captures:
                    assert env.resident.captures[scope.job_id].delivery_state == "pending"
                return
            assert delivered["delivery_state"] == "approved", delivered
            captured = await remote.call_tool("read_capture", {"request": {"job_id": scope.job_id}})
            assert not captured.is_error and any(c.type == "image" for c in captured.content)
        finally:
            endpoint.close()
