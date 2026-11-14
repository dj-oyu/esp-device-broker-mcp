"""Explicit MCP HOME recovery after a proved serial close; synthetic serial only."""

from copy import deepcopy
from types import SimpleNamespace

import httpx2
import pytest
from test_home_recovery import Clock, Port
from test_registry_http import LOCAL
from test_serial_http import serial_service as serial_service

from cardputer_mcp.broker import digest_plan
from cardputer_mcp.client import call, connect
from cardputer_mcp.config import SerialSettings
from cardputer_mcp.models import TestName as Action
from cardputer_mcp.ports import PortInfo
from cardputer_mcp.serial_backend import SerialBackend


def seed_closed_derby_source(env, principal):
    """Legacy history: a removed derby_smoke job whose capture failed and whose close was proved."""
    home = Port(Clock())
    config = SerialSettings(
        selector={"vid": 1, "pid": 2, "serial_number": "synthetic"},
        expected_firmware_sha256="a" * 64,
    )
    backend = SerialBackend(
        config,
        inventory=lambda: [PortInfo(port="COM42", vid=1, pid=2, serial_number="synthetic")],
        factory=lambda: home,
        clock=home.clock,
    )
    broker = env.resident
    broker.backend = backend
    broker.settings = broker.settings.model_copy(
        update={"backend": "serial", "serial": config, "allow_mock_execution": False}
    )
    plan = {
        "version": 1,
        "broker_epoch": broker.epoch,
        "session_id": "d" * 32,
        "backend": "serial",
        "device": "configured-cardputer",
        "job_id": "derby-source",
        **backend.prepare(Action.HELLO),
        "test": "derby_smoke",
        "stages": ["serial_open", "derby_smoke", "serial_close"],
        "protocol": "pocketjs-derby-smoke-v1",
    }
    store = broker._store()
    digest = digest_plan(plan)
    store.put_plan(digest, plan)
    store.add_job(plan["job_id"], digest)
    store.db.execute("UPDATE jobs SET stage='serial_close' WHERE id=?", (plan["job_id"],))
    store.db.commit()
    store.set_job(
        plan["job_id"],
        "needs_review",
        "serial_capture_invalid",
        {
            "expected_marker": "CAPTURE_END",
            "receive_summary": {"last_command": "capture_frame", "command_write_completed": True},
        },
    )
    backend.last_closed_plan = deepcopy(plan)
    broker.home_recovery.record_closed(plan, principal, "remote_mcp")
    assert store.home_source("derby-source") is not None
    return home


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
    ):
        home = seed_closed_derby_source(env, env.keys["second"].metadata.principal_id)
        async with connect(env.url, env.keys[requester].token) as remote:
            names = {t.name for t in (await remote.list_tools()).tools}
            assert {"execute", "cancel"} <= names
            response = await remote.call_tool(
                "execute",
                {
                    "request": {
                        "op": "home_recovery",
                        "job_id": "home-one",
                        "source_job_id": "derby-source",
                    }
                },
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
            status = await call(remote, "status", {"view": "job", "job_id": "home-one"})
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
                "execute",
                {
                    "request": {
                        "op": "home_recovery",
                        "job_id": "home-two",
                        "source_job_id": "derby-source",
                    }
                },
            )
            assert repeated.structured_content["ok"] is False and home.writes == [b"q"]
            for route in ("home-recovery-review", "home-recovery-approve"):
                retired = await http.post("/operator/" + route, json={"plan_hash": p["plan_hash"]})
                assert retired.status_code == 410
            assert home.writes == [b"q"]
