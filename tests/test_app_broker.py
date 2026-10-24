"""Resident queue/authority/MCP integration; synthetic devices only."""

import asyncio
import json

import pytest
from test_registry_http import registry_service as registry_service

from cardputer_mcp.app_client import AppSessionClient
from cardputer_mcp.app_models import AppEventsRequest, AppSessionOptions
from cardputer_mcp.broker import Broker, BrokerError
from cardputer_mcp.client import call, connect
from cardputer_mcp.config import Settings
from cardputer_mcp.models import (
    AcquireRequest,
    CancelRequest,
    ExecuteRequest,
    LogRequest,
    Origin,
    PrepareRequest,
)
from cardputer_mcp.models import TestName as Workflow


@pytest.mark.parametrize("cancelled", [False, True])
def test_app_plan_is_immutable_and_resident_worker_deduplicates_execution(tmp_path, cancelled):
    async def scenario():
        broker = Broker(
            Settings(state_dir=tmp_path / "state"),
            lock_path=tmp_path / "device.lock",
            principal_valid=lambda *args: True,
        )
        await broker.start()
        try:
            token = broker.acquire(AcquireRequest(owner=Origin.LOCAL), principal_id="owner")[
                "session"
            ]
            options = AppSessionOptions(app_id="local.hello", duration_ms=1000)
            prepared = broker.prepare(
                PrepareRequest(
                    session=token, job_id="app-one", test=Workflow.APP_SESSION, app=options
                )
            )
            options.allowed_actions.append("up")
            stored = broker._store().plan(prepared["plan_hash"])
            assert stored["app"]["allowed_actions"] == ["accept", "back"]
            assert stored["catalog_binding"] == {"source": "unobserved", "expected": None}
            execute = ExecuteRequest(session=token, plan_hash=prepared["plan_hash"])
            broker.execute(execute)
            assert broker.execute(execute)["deduplicated"]
            if cancelled:
                deadline = asyncio.get_running_loop().time() + 2
                while True:
                    run = broker.app_sessions.jobs.get("app-one")
                    if run is not None and run.state == "running":
                        break
                    assert asyncio.get_running_loop().time() < deadline
                    await asyncio.sleep(0.005)
                broker.cancel(CancelRequest(session=token, job_id="app-one"))
            await asyncio.wait_for(broker.queue.join(), 3)
            assert broker.backend.calls == 1
            expected_state = "cancelled" if cancelled else "failed"
            expected_result = "app_session_cancelled" if cancelled else "app_session_timeout"
            assert broker.job("app-one")["state"] == expected_state
            assert broker.job("app-one")["result"] == expected_result
            events = [entry["event"] for entry in broker.read_logs(LogRequest())["entries"]]
            assert ("job_cancelled" if cancelled else "job_failed") in events
            assert "job_passed" not in events
            report = broker.read_app_events(AppEventsRequest(job_id="app-one"), "owner")
            assert any(event["kind"] == "started" for event in report["entries"])
            with pytest.raises(BrokerError, match="app_events_not_available"):
                broker.read_app_events(AppEventsRequest(job_id="app-one"), "other")
            assert "observed_during_run" not in json.dumps(broker.job("app-one"))
        finally:
            await broker.close()

    asyncio.run(scenario())


async def test_mcp_app_tools_metadata_and_same_host_client_for_registered_apps(registry_service):
    service = registry_service
    async with connect(service.url, service.keys["second"].token) as client:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}
        assert set(tools) == {"status", "submit", "execute", "flash", "cancel", "session"}
        assert tools["status"].annotations.read_only_hint is True
        assert tools["execute"].annotations.read_only_hint is False
        assert tools["execute"].annotations.destructive_hint is False
        status_schema = json.dumps(tools["status"].input_schema)
        for view in ("apps", "logs"):
            assert view in status_schema
        for stream in ("app",):
            assert stream in status_schema
        execute_schema = json.dumps(tools["execute"].input_schema)
        assert "app_input" in execute_schema
        schema = json.dumps(tools["submit"].input_schema)
        assert "app_session" in schema and "AppSessionOptions" in schema
        for index, app_id in enumerate(("local.hello", "local.stress")):
            lease = await call(
                client, "session", {"action": "open", "owner": "remote_mcp", "lease_seconds": 30}
            )
            assert lease["ok"]
            api = AppSessionClient(client)
            started = await api.start(
                lease["session"], f"app-http-{index}", AppSessionOptions(app_id=app_id)
            )
            assert started["synthetic"] is True
            ack = await api.tap("accept")
            assert ack["result"] == "consumed"
            terminal = await api.stop()
            assert terminal["state"] == "succeeded"
            catalog = await api.list_apps()
            assert catalog["fresh"] is True
            await call(client, "session", {"action": "release", "session": lease["session"]})
    assert service.backend.max_active == 1 and service.backend.calls == 2
