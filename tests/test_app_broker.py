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
from cardputer_mcp.models import AcquireRequest, ExecuteRequest, Origin, PrepareRequest
from cardputer_mcp.models import TestName as Workflow


def test_app_plan_is_immutable_and_resident_worker_deduplicates_execution(tmp_path):
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
            await asyncio.wait_for(broker.queue.join(), 3)
            assert broker.backend.calls == 1
            assert broker.job("app-one")["state"] == "failed"
            assert broker.job("app-one")["result"] == "app_session_timeout"
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
        for name in ("list_apps", "app_command", "read_app_events"):
            assert name in tools
        assert tools["list_apps"].annotations.read_only_hint is True
        assert tools["read_app_events"].annotations.read_only_hint is True
        assert tools["app_command"].annotations.read_only_hint is False
        schema = json.dumps(tools["prepare_test"].input_schema)
        assert "app_session" in schema and "AppSessionOptions" in schema
        for index, app_id in enumerate(("local.hello", "local.stress")):
            lease = await call(client, "acquire_session", {"owner": "remote_mcp"})
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
            await call(client, "release_session", {"session": lease["session"]})
    assert service.backend.max_active == 1 and service.backend.calls == 2
