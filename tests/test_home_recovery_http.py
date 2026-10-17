"""Authenticated MCP/local approval and optional native CLI; synthetic serial only."""

import asyncio
import hashlib
import os
import sys
from pathlib import Path

import httpx2
import pytest
from test_derby_smoke import Wire
from test_home_recovery import Clock, Port
from test_operator_launch_http import PythonCliProcess
from test_operator_launch_pipe import exchange
from test_registry_http import LOCAL
from test_registry_http import registry_service as registry_service

from cardputer_mcp.client import call, connect
from cardputer_mcp.config import SerialSettings
from cardputer_mcp.home_recovery import ATTESTATION
from cardputer_mcp.home_recovery_launch import HomeRecoveryLaunchScope, HomeRecoveryLaunchSlot
from cardputer_mcp.operator_launch_pipe import WindowsOperatorLaunchPipe, current_user_sid
from cardputer_mcp.ports import PortInfo
from cardputer_mcp.serial_backend import SerialBackend


class BrokenCapture(Wire):
    def write(self, data):
        if data != b"s":
            return super().write(data)
        self.writes.append(data)
        # Valid family/dimensions, invalid first row sequence, then a proved close.
        self.queue.append(b"CAPTURE_BEGIN 240 135\nPIX 1 " + b"0000" * 240 + b"\n")
        return 1


async def fail_derby(env, remote, http):
    source = BrokenCapture()
    home = Port(Clock())
    config = SerialSettings(
        selector={"vid": 1, "pid": 2, "serial_number": "synthetic"},
        expected_firmware_sha256="a" * 64,
    )
    env.resident.backend = SerialBackend(
        config,
        inventory=lambda: [PortInfo(port="COM42", vid=1, pid=2, serial_number="synthetic")],
        factory=lambda: home if source.closed else source,
        clock=lambda: home.clock() if source.closed else source.now,
    )
    env.resident.settings = env.resident.settings.model_copy(
        update={"backend": "serial", "serial": config, "allow_mock_execution": False}
    )
    lease = await call(remote, "acquire_session", {"owner": "remote_mcp", "lease_seconds": 300})
    p = await call(
        remote,
        "prepare_test",
        {"session": lease["session"], "job_id": "derby-source", "test": "derby_smoke"},
    )
    await call(
        remote,
        "execute_test",
        {"session": lease["session"], "plan_hash": p["plan_hash"], "wait_for_approval": True},
    )
    review = (await http.post("/operator/plan", json={"plan_hash": p["plan_hash"]})).json()
    approved = (
        await http.post(
            "/operator/approve-plan",
            json={
                "plan_hash": p["plan_hash"],
                "review_revision": review["review_revision"],
                "attestation": "DEVICE HOME AND FIRMWARE VERIFIED",
            },
        )
    ).json()
    assert approved["ok"], approved
    await env.resident.queue.join()
    source_job = env.resident.job("derby-source")
    assert source_job["result"] == "serial_capture_invalid", source_job
    assert source.closed and b"q" not in source.writes
    assert env.resident._store().home_source("derby-source") is not None
    await call(remote, "release_session", {"session": lease["session"]})
    return home


@pytest.mark.parametrize(
    "change", [None, "remote_approve", "wrong_attestation", "cancel", "revoke"]
)
async def test_real_closed_derby_failure_has_dedicated_approved_home_transition(
    registry_service, change
):
    env = registry_service
    async with (
        httpx2.AsyncClient(
            base_url=env.url.removesuffix("/mcp"),
            trust_env=False,
            headers={"Authorization": "Bearer " + LOCAL},
        ) as http,
        connect(env.url, env.keys["second"].token) as remote,
    ):
        home = await fail_derby(env, remote, http)
        names = {t.name for t in (await remote.list_tools()).tools}
        assert {"request_home_recovery", "cancel_home_recovery"} <= names
        p = await call(
            remote, "request_home_recovery", {"job_id": "home-one", "source_job_id": "derby-source"}
        )
        assert p["ok"] and p["state"] == "awaiting_approval", p
        assert not home.opens
        # The old generic/capture approval route must not approve this operation.
        old = (await http.post("/operator/plan", json={"plan_hash": p["plan_hash"]})).json()
        assert not old["ok"] and not home.opens
        r = (
            await http.post("/operator/home-recovery-review", json={"plan_hash": p["plan_hash"]})
        ).json()
        assert r["ok"], r
        body = {
            "plan_hash": p["plan_hash"],
            "review_revision": r["review_revision"],
            "attestation": ATTESTATION,
        }
        headers = None
        if change == "remote_approve":
            headers = {"Authorization": "Bearer " + env.keys["second"].token}
        elif change == "wrong_attestation":
            body["attestation"] = "DEVICE HOME AND FIRMWARE VERIFIED"
        elif change == "cancel":
            await call(
                remote, "cancel_home_recovery", {"job_id": "home-one", "plan_hash": p["plan_hash"]}
            )
        elif change == "revoke":
            env.registry.revoke(env.keys["second"].metadata.key_id)
        response = await http.post("/operator/home-recovery-approve", json=body, headers=headers)
        approval = response.json()
        if change:
            assert not approval.get("ok", False) and not home.opens, approval
            assert env.resident.job("derby-source")["reviewed"] == 0
            return
        assert approval["ok"], approval
        await env.resident.home_recovery.entries[p["plan_hash"]].task
        status = await call(remote, "job_status", {"job_id": "home-one"})
        assert status["state"] == "succeeded", status
        assert home.writes == [b"q"] and home.closes == 1
        assert [x["event"] for x in status["recovery_audit"]] == [
            "source_close_confirmed",
            "requested",
            "attempt_consumed",
            "serial_open",
            "q_written",
            "fresh_home_observed",
            "serial_close",
            "fresh_home_and_close_confirmed",
        ]
        assert env.resident.job("derby-source")["reviewed"] == 1


@pytest.mark.skipif(os.name != "nt", reason="Native Windows same-user pipe and formal CLI")
async def test_exact_recovery_operator_window_through_formal_cli(registry_service, tmp_path):
    env = registry_service
    async with (
        httpx2.AsyncClient(
            base_url=env.url.removesuffix("/mcp"),
            trust_env=False,
            headers={"Authorization": "Bearer " + LOCAL},
        ) as http,
        connect(env.url, env.keys["second"].token) as remote,
    ):
        home = await fail_derby(env, remote, http)
        p = await call(
            remote,
            "request_home_recovery",
            {"job_id": "home-window", "source_job_id": "derby-source"},
        )
        assert p["ok"], p
        plan = p["plan"]
        scope = HomeRecoveryLaunchScope(
            plan_hash=p["plan_hash"],
            **{k: plan[k] for k in HomeRecoveryLaunchScope.model_fields if k != "plan_hash"},
        )
        exe = Path(sys.executable)
        digest = hashlib.sha256(await asyncio.to_thread(exe.read_bytes)).hexdigest()
        slot = HomeRecoveryLaunchSlot(
            exe,
            digest,
            tmp_path,
            env.url,
            LOCAL,
            current_user_sid(),
            scope=scope,
            process=PythonCliProcess(),
        )
        endpoint = WindowsOperatorLaunchPipe(slot)
        try:
            r = await exchange(endpoint, {**scope.model_dump(), "action": "home-recovery-review"})
            assert r["ok"], r
            approved = await exchange(
                endpoint,
                {
                    **scope.model_dump(),
                    "action": "home-recovery-approve",
                    "review_revision": r["review"]["review_revision"],
                    "attestation": ATTESTATION,
                },
            )
            assert approved["ok"] and slot.closed and slot._local_token is None, approved
            await env.resident.home_recovery.entries[p["plan_hash"]].task
            assert env.resident.job("home-window")["state"] == "succeeded"
            assert home.writes == [b"q"]
        finally:
            endpoint.close()
