"""Explicit MCP HOME recovery after a proved serial close; synthetic serial only."""

import httpx2
import pytest
from test_derby_http import derby_service as derby_service
from test_derby_smoke import Wire
from test_home_recovery import Clock, Port
from test_registry_http import LOCAL
from test_serial_http import serial_service as serial_service

from cardputer_mcp.client import call, connect
from cardputer_mcp.config import SerialSettings
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
    await env.resident.queue.join()
    source_job = env.resident.job("derby-source")
    assert source_job["result"] == "serial_capture_invalid", source_job
    assert source.closed and b"q" not in source.writes
    assert env.resident._store().home_source("derby-source") is not None
    await call(remote, "release_session", {"session": lease["session"]})
    return home


@pytest.mark.parametrize("requester", ["second", "local"])
async def test_closed_derby_recovery_requires_same_authenticated_requester(
    derby_service, requester
):
    env = derby_service
    async with (
        httpx2.AsyncClient(
            base_url=env.url.removesuffix("/mcp"),
            trust_env=False,
            headers={"Authorization": "Bearer " + LOCAL},
        ) as http,
        connect(env.url, env.keys["second"].token) as source_remote,
    ):
        home = await fail_derby(env, source_remote, http)
        async with connect(env.url, env.keys[requester].token) as remote:
            names = {t.name for t in (await remote.list_tools()).tools}
            assert {"request_home_recovery", "cancel_home_recovery"} <= names
            response = await remote.call_tool(
                "request_home_recovery",
                {"request": {"job_id": "home-one", "source_job_id": "derby-source"}},
            )
            if requester != "second":
                assert response.structured_content["ok"] is False
                assert not home.opens and not home.writes
                assert env.resident.job("derby-source")["reviewed"] == 0
                return
            assert not response.is_error, response
            p = response.structured_content
            assert p["ok"] and p["state"] == "running", p
            assert "attestation" not in p and "review_revision" not in p
            await env.resident.home_recovery.entries[p["plan_hash"]].task
            status = await call(remote, "job_status", {"job_id": "home-one"})
            assert status["state"] == "succeeded", status
            assert home.writes == [b"q"] and home.closes == 1
            assert [x["event"] for x in status["recovery_audit"]] == [
                "source_close_confirmed",
                "attempt_consumed",
                "serial_open",
                "q_written",
                "fresh_home_observed",
                "serial_close",
                "fresh_home_and_close_confirmed",
            ]
            assert env.resident.job("derby-source")["reviewed"] == 1
            repeated = await remote.call_tool(
                "request_home_recovery",
                {"request": {"job_id": "home-two", "source_job_id": "derby-source"}},
            )
            assert repeated.structured_content["ok"] is False and home.writes == [b"q"]
            for route in ("home-recovery-review", "home-recovery-approve"):
                retired = await http.post("/operator/" + route, json={"plan_hash": p["plan_hash"]})
                assert retired.status_code == 410
            assert home.writes == [b"q"]
