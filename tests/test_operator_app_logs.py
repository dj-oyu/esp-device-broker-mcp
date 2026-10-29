"""Any local operator with read scope reads app logs, redacted; non-local callers never do."""

import json
from types import SimpleNamespace

import pytest
from test_operator_live_http import actor, answer
from test_operator_live_http import operator as operator

from cardputer_mcp.app_events import AppEvents
from cardputer_mcp.app_models import AppSessionOptions
from cardputer_mcp.app_sessions import RunningApp
from cardputer_mcp.models import AcquireRequest, Origin, PrepareRequest
from cardputer_mcp.models import TestName as Action


@pytest.fixture
async def app_output(operator):
    api, broker, current = operator
    token = broker.acquire(AcquireRequest(owner=Origin.LOCAL), principal_id="owner")["session"]
    options = AppSessionOptions(app_id="local.hello")
    prepared = broker.prepare(
        PrepareRequest(session=token, job_id="app-logs", test=Action.APP_SESSION, app=options)
    )
    store = broker._store()
    store.enqueue_requested("app-logs", prepared["plan_hash"], "owner")
    store.set_job("app-logs", "succeeded", "app_session_stopped")
    run = RunningApp(
        "owner",
        options,
        AppEvents("owner", options.log_bytes, on_logs_change=broker.app_sessions.on_logs_change),
    )
    run.running, run.state = False, "stopped"
    broker.app_sessions.jobs["app-logs"] = run
    return api, broker, current, run, prepared


def payload(**fields):
    return {
        "job_id": "app-logs",
        "levels": ["error", "warning", "info", "debug", "unknown"],
        "context": 0,
        "limit": 100,
        **fields,
    }


async def test_same_principal_reads_only_redacted_logs_without_preflight(app_output, monkeypatch):
    api, broker, _, run, _ = app_output
    run.events.log("info: token=SYNTHETIC_PRIVATE_LOG observed output")
    run.events.emit("ack", command_id=4, control_only="SYNTHETIC_CONTROL_NOT_LOG")
    monkeypatch.setattr(
        broker.backend, "prepare", lambda *_: pytest.fail("log read must not preflight")
    )
    monkeypatch.setattr(
        broker, "read_app_events", lambda *_: pytest.fail("control events are not operator logs")
    )
    status, result = await answer(api, payload(), "job-logs")
    assert status == 200 and result["ok"] and result["status"] == "available"
    assert len(result["entries"]) == 1
    entry = result["entries"][0]
    assert entry["stream"] == "observed_during_run" and "[REDACTED]" in entry["text"]
    rendered = json.dumps(result)
    assert "SYNTHETIC_PRIVATE_LOG" not in rendered and "SYNTHETIC_CONTROL_NOT_LOG" not in rendered
    assert broker.backend.calls == 0 and not broker.backend.hardware_connected


@pytest.mark.parametrize("name", ["other", "ephemeral:remote_mcp"])
async def test_any_local_operator_reads_redacted_app_logs(app_output, name):
    api, broker, current, run, _ = app_output
    run.events.log("info: token=SYNTHETIC_PRIVATE_LOG observed output")
    current[0] = actor(name)
    status, result = await answer(api, payload(), "job-logs")
    assert status == 200 and result["ok"] and result["status"] == "available"
    assert "SYNTHETIC_PRIVATE_LOG" not in json.dumps(result) and broker.backend.calls == 0
    # A read-only local key (the Web UI's) reads too: no control scope is needed.
    current[0] = SimpleNamespace(
        principal_id="webui", origin=Origin.LOCAL, scopes=frozenset({"read"})
    )
    status, result = await answer(api, payload(), "job-logs")
    assert status == 200 and result["ok"] and len(result["entries"]) == 1
    assert "SYNTHETIC_PRIVATE_LOG" not in json.dumps(result)


async def test_non_local_or_unscoped_callers_cannot_read_app_logs(app_output):
    api, _, current, run, _ = app_output
    run.events.log("info: SYNTHETIC_OWNED_TEXT")
    current[0] = SimpleNamespace(
        principal_id="owner", origin=Origin.REMOTE, scopes=frozenset({"read", "control"})
    )
    status, result = await answer(api, payload(), "job-logs")
    assert status == 403 and result == {"ok": False, "error": "local_operator_required"}
    current[0] = SimpleNamespace(
        principal_id="owner", origin=Origin.LOCAL, scopes=frozenset({"control"})
    )
    status, result = await answer(api, payload(), "job-logs")
    assert result == {"ok": False, "error": "not_authorized"}
    assert "SYNTHETIC_OWNED_TEXT" not in json.dumps(result)


@pytest.mark.parametrize("component", ["run", "events", "logs"])
async def test_runtime_log_owner_bindings_must_agree(app_output, component):
    api, _, _, run, _ = app_output
    if component == "run":
        run.principal = "other"
    elif component == "events":
        run.events.principal = "other"
    else:
        run.events.logs.principal = "other"
    _, result = await answer(api, payload(), "job-logs")
    assert result == {"ok": False, "error": "not_authorized"}


@pytest.mark.parametrize("mismatch", ["epoch", "digest", "receipt", "queued"])
async def test_job_plan_epoch_and_execution_binding_fail_closed(app_output, mismatch):
    api, broker, _, run, prepared = app_output
    store = broker._store()
    if mismatch == "epoch":
        broker.epoch = "1" * 32
    elif mismatch == "digest":
        plan = store.plan(prepared["plan_hash"])
        plan["app"]["duration_ms"] = 1000
        store.db.execute(
            "UPDATE plans SET payload=? WHERE hash=?", (json.dumps(plan), prepared["plan_hash"])
        )
        store.db.commit()
    elif mismatch == "receipt":
        store.db.execute(
            "UPDATE execution_requests SET principal_id='other' WHERE job_id='app-logs'"
        )
        store.db.commit()
    else:
        store.set_job("app-logs", "queued")
    _, result = await answer(api, payload(), "job-logs")
    assert result == {"ok": False, "error": "unavailable"} and broker.backend.calls == 0


async def test_authentication_is_rechecked_after_receiving_request_body(app_output):
    api, _, _, run, _ = app_output
    run.events.log("info: SYNTHETIC_OWNED_TEXT")
    calls = []

    def authenticate(_header):
        calls.append(True)
        return actor() if len(calls) == 1 else None

    api.authenticate = authenticate
    status, result = await answer(api, payload(), "job-logs")
    assert status == 401 and result == {"ok": False, "error": "authentication_required"}
    assert len(calls) == 2 and "SYNTHETIC_OWNED_TEXT" not in json.dumps(result)


async def test_app_logs_survive_a_restart_from_the_durable_copy(app_output):
    api, broker, current, _, _ = app_output
    log = broker.job_logs.begin("app-logs", "owner", "app_session")
    log.append("app", "I (1) app: written before the restart", "info")
    log.close()
    broker.app_sessions.jobs.clear()  # a restarted broker has no run for the job
    status, result = await answer(api, payload(), "job-logs")
    assert status == 200 and result["ok"] and result["status"] == "available"
    assert [entry["text"] for entry in result["entries"]] == [
        "I (1) app: written before the restart"
    ]
    assert result["source"] == "durable_job_log"
    # The persisted copy is readable by any local operator, as the RAM copy is.
    current[0] = actor("other")
    status, result = await answer(api, payload(), "job-logs")
    assert status == 200 and result["ok"] and result["source"] == "durable_job_log"


async def test_app_logs_without_a_run_or_a_durable_copy_are_unavailable(app_output):
    api, broker, _, _, _ = app_output
    broker.app_sessions.jobs.clear()
    _, result = await answer(api, payload(), "job-logs")
    assert result == {"ok": False, "error": "unavailable"}
