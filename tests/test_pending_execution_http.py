"""Submitted execution intent across MCP, local approval HTTP and the CLI.

The ASGI transport, credentials, clocks, inventory and serial handles are all
synthetic. These tests never start a network listener or open real USB hardware.
"""

import asyncio
import json
import sqlite3
from contextlib import asynccontextmanager

import pytest
from pydantic import ValidationError
from test_device_observation_integration import LOCAL, LOG_LINE, REMOTE, rpc, rpc_response
from test_restart_observation_integration import http_restart as http_restart
from test_restart_observation_integration import reset_count

from cardputer_mcp import cli
from cardputer_mcp.models import ExecuteRequest

JOB = "submitted-restart"


async def prepare(env):
    lease = (await rpc(env, "acquire_session", {"owner": "remote_mcp", "lease_seconds": 300}))[
        "structuredContent"
    ]
    assert lease["ok"]
    plan = (
        await rpc(
            env,
            "prepare_test",
            {"session": lease["session"], "job_id": JOB, "test": "restart_and_observe"},
        )
    )["structuredContent"]
    assert plan["ok"] and plan["approval_required"]
    return {"session": lease["session"], "plan_hash": plan["plan_hash"]}, plan


async def detail(env, plan_hash):
    response = await env.http.post("/operator/plan", json={"plan_hash": plan_hash})
    assert response.status_code == 200 and response.json()["ok"], response.text
    assert response.headers["cache-control"] == "no-store"
    return response.json()


async def approve(env, reviewed, *, token=LOCAL):
    return await env.http.post(
        "/operator/approve-plan",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "plan_hash": reviewed["plan_hash"],
            "review_revision": reviewed["review_revision"],
            "attestation": "",
        },
    )


async def submit(env, request):
    result = (await rpc(env, "execute_test", {**request, "wait_for_approval": True}))[
        "structuredContent"
    ]
    assert result["ok"] and result["state"] == "awaiting_approval", result
    return result


def assert_no_device_work(env):
    assert env.backend.calls == 0 and reset_count(env) == 0
    assert not env.created and env.broker.queue.empty()
    assert all(port.opens == 0 and port.writes == [] for port in env.ports)


async def test_http_submission_is_durable_inert_and_requires_a_refreshed_review(http_restart):
    env = http_restart
    request, plan = await prepare(env)
    before = await detail(env, request["plan_hash"])
    assert before["execution_submitted"] is False
    assert before["intent_remaining_seconds"] is None

    result = await submit(env, request)
    assert result["id"] == JOB and result["plan_hash"] == request["plan_hash"]
    assert result["deduplicated"] is False
    assert_no_device_work(env)
    assert plan["plan"]["log_delivery"]["principal_id"] == env.keys["owner"].metadata.principal_id
    with sqlite3.connect(env.broker.settings.state_dir / "history.sqlite3") as persisted:
        assert persisted.execute(
            "SELECT plan_hash,state,finished FROM jobs WHERE id=?", (JOB,)
        ).fetchone() == (request["plan_hash"], "awaiting_approval", None)

    stale = await approve(env, before)
    assert stale.json() == {"ok": False, "error": "stale_plan_review"}
    assert_no_device_work(env)
    after = await detail(env, request["plan_hash"])
    assert after["execution_submitted"] is True
    assert 0 < after["intent_remaining_seconds"] <= after["lease_remaining_seconds"]
    assert after["review_revision"] != before["review_revision"]
    for secret in (request["session"], *(key.token for key in env.keys.values())):
        assert secret not in json.dumps(after)

    repeated = await submit(env, request)
    assert repeated["deduplicated"] is True
    again = await detail(env, request["plan_hash"])
    assert again["review_revision"] == after["review_revision"]
    assert again["intent_remaining_seconds"] == after["intent_remaining_seconds"]
    assert env.broker._store().record_count("jobs") == 1
    assert_no_device_work(env)


async def test_exact_local_http_approval_queues_submitted_restart_without_second_execute(
    http_restart, monkeypatch
):
    env = http_restart
    request, _ = await prepare(env)
    await submit(env, request)
    reviewed = await detail(env, request["plan_hash"])
    queued = []
    original_put = env.broker.queue.put_nowait

    def observe_queue(job_id):
        if job_id == JOB:
            queued.append(env.broker._store().job(job_id)["state"])
        original_put(job_id)

    monkeypatch.setattr(env.broker.queue, "put_nowait", observe_queue)
    for token in (env.keys["owner"].token, env.keys["other"].token, REMOTE):
        denied = await approve(env, reviewed, token=token)
        assert denied.status_code == 403
    assert_no_device_work(env)

    approved = await approve(env, reviewed)
    assert approved.status_code == 200 and approved.json()["ok"], approved.text
    assert approved.json()["approved"] is True
    assert queued == ["queued"]
    await asyncio.wait_for(env.broker.queue.join(), timeout=5)
    completed = (await rpc(env, "job_status", {"job_id": JOB}))["structuredContent"]
    assert completed["ok"] and completed["state"] == "succeeded"
    assert reset_count(env) == env.backend.calls == 1
    assert len(env.created) == 1
    assert env.ports[0].opens == env.ports[0].closes == 1
    assert all(port.writes == [] for port in env.ports)

    for _ in range(3):
        repeated = (await rpc(env, "execute_test", {**request, "wait_for_approval": True}))[
            "structuredContent"
        ]
        assert repeated["ok"] and repeated["deduplicated"] is True
        assert repeated["state"] == "succeeded"
        # A stale or completed approval may be rejected, but can never replay.
        await approve(env, reviewed)
    await asyncio.wait_for(env.broker.queue.join(), timeout=5)
    assert queued == ["queued"] and reset_count(env) == env.backend.calls == 1

    report = (await rpc(env, "read_device_logs", {"job_id": JOB}))["structuredContent"]
    assert report["ok"] and LOG_LINE in report["logs"]
    for token in (LOCAL, REMOTE, env.keys["other"].token, env.keys["reader"].token):
        assert (await rpc(env, "read_device_logs", {"job_id": JOB}, token=token))[
            "structuredContent"
        ] == {"ok": False, "error": "device_logs_not_available"}


@pytest.mark.parametrize("explicit_false", [False, True])
async def test_prepare_or_default_execute_does_not_submit_execution(http_restart, explicit_false):
    env = http_restart
    request, _ = await prepare(env)
    if explicit_false:
        request["wait_for_approval"] = False
    assert (await rpc(env, "execute_test", request))["structuredContent"] == {
        "ok": False,
        "error": "operator_approval_required",
    }
    reviewed = await detail(env, request["plan_hash"])
    assert reviewed["execution_submitted"] is False
    assert reviewed["intent_remaining_seconds"] is None
    approved = await approve(env, reviewed)
    assert approved.status_code == 200 and approved.json()["ok"], approved.text
    assert env.broker._store().job(JOB) is None
    assert_no_device_work(env)
    await asyncio.wait_for(env.broker.queue.join(), timeout=5)
    assert_no_device_work(env)


@pytest.mark.parametrize(
    "credential,error",
    [("other", "authenticated_owner_mismatch"), ("reader", "insufficient_scope")],
)
async def test_only_original_control_principal_can_submit_execution(
    http_restart, credential, error
):
    env = http_restart
    request, _ = await prepare(env)
    denied = await rpc(
        env,
        "execute_test",
        {**request, "wait_for_approval": True},
        token=env.keys[credential].token,
    )
    assert denied["structuredContent"] == {"ok": False, "error": error}
    assert env.broker._store().job(JOB) is None
    assert (await detail(env, request["plan_hash"]))["execution_submitted"] is False
    assert_no_device_work(env)
    await submit(env, request)
    assert (await detail(env, request["plan_hash"]))["execution_submitted"] is True
    assert_no_device_work(env)


@pytest.mark.parametrize("invalidate", ["revoke", "key_expiry", "lease_expiry"])
async def test_queued_submission_rechecks_authority_before_dispatch(
    http_restart, monkeypatch, invalidate
):
    env = http_restart
    request, _ = await prepare(env)
    await submit(env, request)
    reviewed = await detail(env, request["plan_hash"])
    original_put = env.broker.queue.put_nowait
    queued = []

    def invalidate_after_enqueue(job_id):
        original_put(job_id)
        if job_id != JOB:
            return
        queued.append(env.broker._store().job(job_id)["state"])
        if invalidate == "revoke":
            assert env.registry.revoke(env.keys["owner"].metadata.key_id)
        elif invalidate == "key_expiry":
            env.key_clock.now += 11
        else:
            env.clock.now += 301

    monkeypatch.setattr(env.broker.queue, "put_nowait", invalidate_after_enqueue)
    approved = await approve(env, reviewed)
    assert approved.status_code == 200 and approved.json()["ok"], approved.text
    assert queued == ["queued"]
    await asyncio.wait_for(env.broker.queue.join(), timeout=5)
    assert env.broker.job(JOB)["state"] == "cancelled"
    assert_no_device_work(env)


async def test_mcp_schema_adds_boolean_intent_but_never_an_operator_approval_tool(http_restart):
    env = http_restart
    catalog = await rpc_response(env, "tools/list", {}, token=env.keys["owner"].token)
    assert catalog.status_code == 200
    tools = catalog.json()["result"]["tools"]
    assert not any("approv" in item["name"] or "operator" in item["name"] for item in tools)
    execute = next(item for item in tools if item["name"] == "execute_test")
    schema = execute["inputSchema"]
    request_schema = schema["properties"]["request"]
    if "$ref" in request_schema:
        for component in request_schema["$ref"].removeprefix("#/").split("/"):
            schema = schema[component]
        request_schema = schema
    field = request_schema["properties"]["wait_for_approval"]
    assert field["type"] == "boolean" and field["default"] is False
    assert "wait_for_approval" not in request_schema.get("required", [])
    assert_no_device_work(env)


@pytest.mark.parametrize("invalid", [None, 0, 1, "true", "false", [], {}])
async def test_mcp_rejects_non_boolean_intent_before_any_durable_submission(http_restart, invalid):
    env = http_restart
    request, _ = await prepare(env)
    rejected = await rpc(env, "execute_test", {**request, "wait_for_approval": invalid})
    assert rejected["isError"] is True
    assert request["session"] not in json.dumps(rejected)
    assert env.broker._store().job(JOB) is None
    assert_no_device_work(env)


def test_execute_request_intent_is_strict_and_defaults_to_legacy_mode():
    request = {"session": "a" * 64, "plan_hash": "b" * 64}
    assert ExecuteRequest(**request).wait_for_approval is False
    assert ExecuteRequest(**request, wait_for_approval=True).wait_for_approval is True
    for invalid in (None, 0, 1, "true", "false", [], {}):
        with pytest.raises(ValidationError):
            ExecuteRequest(**request, wait_for_approval=invalid)


@pytest.mark.parametrize("wait_for_approval", [False, True])
async def test_cli_explicit_flag_is_transmitted_to_execute_test(monkeypatch, wait_for_approval):
    arguments = ["execute", "--session", "a" * 64, "--plan", "b" * 64]
    if wait_for_approval:
        arguments.append("--wait-for-approval")
    args = cli.parser().parse_args(arguments)
    assert args.wait_for_approval is wait_for_approval
    client = object()
    seen = []

    @asynccontextmanager
    async def connect(url, token):
        assert url == "http://127.0.0.1:8765/mcp" and token is None
        yield client

    async def call(actual_client, tool, request):
        assert actual_client is client
        seen.append((tool, request))
        return {"ok": True, "state": "awaiting_approval" if wait_for_approval else "queued"}

    monkeypatch.setattr(cli, "connect", connect)
    monkeypatch.setattr(cli, "call", call)
    monkeypatch.setattr(cli, "output", lambda _result: None)
    await cli.run_client(args)
    expected = {"session": "a" * 64, "plan_hash": "b" * 64}
    if wait_for_approval:
        expected["wait_for_approval"] = True
    assert seen == [("execute_test", expected)]
