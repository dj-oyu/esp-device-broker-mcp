"""Explicit execution across authenticated MCP transport; synthetic serial handles only."""

import asyncio
import json

import pytest
from test_device_observation_integration import LOCAL, LOG_LINE, REMOTE, rpc, rpc_response
from test_restart_observation_integration import http_restart as http_restart
from test_restart_observation_integration import reset_count

JOB = "submitted-restart"


async def prepare(env):
    lease = (
        await rpc(env, "session", {"action": "open", "owner": "remote_mcp", "lease_seconds": 300})
    )["structuredContent"]
    assert lease["ok"]
    plan = (
        await rpc(
            env,
            "submit",
            {
                "session": lease["session"],
                "job_id": JOB,
                "kind": "restart",
            },
        )
    )["structuredContent"]
    assert plan["ok"] and plan["approval_required"] is False
    assert plan["execute_tool"] == "execute"
    return {"session": lease["session"], "plan_hash": plan["plan_hash"]}, plan


def assert_no_device_work(env):
    assert env.backend.calls == 0 and reset_count(env) == 0
    assert not env.created and env.broker.queue.empty()
    assert all(port.opens == 0 and port.writes == [] for port in env.ports)


async def test_explicit_mcp_execute_runs_once_and_private_logs_remain_owner_bound(http_restart):
    env = http_restart
    request, plan = await prepare(env)
    assert_no_device_work(env)
    assert plan["plan"]["log_delivery"]["principal_id"] == env.keys["owner"].metadata.principal_id
    result = (await rpc(env, "execute", {"op": "run", **request}))["structuredContent"]
    assert result["ok"] and result["deduplicated"] is False
    assert result["state"] in {"queued", "running", "succeeded"}
    await asyncio.wait_for(env.broker.queue.join(), timeout=5)
    assert env.broker.job(JOB)["state"] == "succeeded"
    assert reset_count(env) == env.backend.calls == 1
    assert env.ports[0].opens == env.ports[0].closes == 1
    for _ in range(3):
        repeated = (await rpc(env, "execute", {"op": "run", **request}))["structuredContent"]
        assert repeated["ok"] and repeated["deduplicated"]
    assert reset_count(env) == env.backend.calls == 1
    report = (await rpc(env, "status", {"view": "logs", "stream": "device", "job_id": JOB}))[
        "structuredContent"
    ]
    assert report["ok"] and LOG_LINE in report["logs"]
    for token in (LOCAL, REMOTE, env.keys["other"].token, env.keys["reader"].token):
        assert (
            await rpc(
                env, "status", {"view": "logs", "stream": "device", "job_id": JOB}, token=token
            )
        )["structuredContent"] == {"ok": False, "error": "device_logs_not_available"}


@pytest.mark.parametrize(
    "credential,error",
    [
        ("other", "authenticated_owner_mismatch"),
        ("reader", "insufficient_scope"),
    ],
)
async def test_only_original_control_principal_can_execute(http_restart, credential, error):
    env = http_restart
    request, _ = await prepare(env)
    denied = await rpc(env, "execute", {"op": "run", **request}, token=env.keys[credential].token)
    assert denied["structuredContent"] == {"ok": False, "error": error}
    assert env.broker._store().job(JOB) is None
    assert_no_device_work(env)


async def test_mcp_schema_has_no_approval_intent_or_operator_tool(http_restart):
    env = http_restart
    catalog = await rpc_response(env, "tools/list", {}, token=env.keys["owner"].token)
    assert catalog.status_code == 200
    tools = catalog.json()["result"]["tools"]
    assert not any("approv" in item["name"] or "operator" in item["name"] for item in tools)
    for name in ("execute", "flash"):
        item = next(item for item in tools if item["name"] == name)
        assert "wait_for_approval" not in json.dumps(item["inputSchema"])
    assert_no_device_work(env)


# The retired field is rejected by the tool schema whatever its value; ExecuteRequest's
# strict value set is covered by test_execute_request_compatibility_field_is_strict.
@pytest.mark.parametrize("invalid", ["true"])
async def test_mcp_rejects_retired_approval_intent_before_any_durable_submission(
    http_restart, invalid
):
    env = http_restart
    request, _ = await prepare(env)
    for tool, arguments in (
        ("execute", {"op": "run", **request}),
        ("flash", request),
    ):
        rejected = await rpc(env, tool, {**arguments, "wait_for_approval": invalid})
        assert rejected["isError"] is True
        assert request["session"] not in json.dumps(rejected)
        assert env.broker._store().job(JOB) is None
    assert_no_device_work(env)
