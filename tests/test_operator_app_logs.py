"""Same-principal operator reads of app RAM logs never broaden private access."""

import asyncio
import json
import time
from types import SimpleNamespace

import pytest
from test_operator_live_http import actor, answer, request, waiting
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
async def test_foreign_principal_cannot_read_local_app_logs(app_output, name):
    api, broker, current, run, _ = app_output
    run.events.log("info: SYNTHETIC_OWNED_TEXT")
    current[0] = actor(name)
    status, result = await answer(api, payload(), "job-logs")
    assert status == 200 and result == {"ok": False, "error": "not_authorized"}
    assert "SYNTHETIC_OWNED_TEXT" not in json.dumps(result) and broker.backend.calls == 0


@pytest.mark.parametrize(
    "origin,scopes,status,error",
    [
        (Origin.LOCAL, frozenset({"control"}), 200, "not_authorized"),
        (Origin.LOCAL, frozenset({"read"}), 403, "local_operator_required"),
        (Origin.REMOTE, frozenset({"read", "control"}), 403, "local_operator_required"),
    ],
)
async def test_existing_local_control_and_read_requirements_remain(
    app_output, origin, scopes, status, error
):
    api, _, current, _, _ = app_output
    current[0] = SimpleNamespace(principal_id="owner", origin=origin, scopes=scopes)
    observed_status, result = await answer(api, payload(), "job-logs")
    assert observed_status == status and result == {"ok": False, "error": error}


@pytest.mark.parametrize("component", ["run", "events", "logs", "delivery", "origin"])
async def test_all_runtime_and_immutable_recipient_bindings_must_match(app_output, component):
    api, broker, _, run, prepared = app_output
    if component == "run":
        run.principal = "other"
    elif component == "events":
        run.events.principal = "other"
    elif component == "logs":
        run.events.logs.principal = "other"
    else:
        plan = broker._store().plan(prepared["plan_hash"])
        plan["log_delivery"]["principal_id" if component == "delivery" else "origin"] = "other"
        broker._store().db.execute(
            "UPDATE plans SET payload=? WHERE hash=?", (json.dumps(plan), prepared["plan_hash"])
        )
        broker._store().db.commit()
    _, result = await answer(api, payload(), "job-logs")
    assert result == {"ok": False, "error": "not_authorized"}


@pytest.mark.parametrize("mismatch", ["epoch", "options", "digest", "receipt", "missing", "queued"])
async def test_job_plan_epoch_and_execution_binding_fail_closed(app_output, mismatch):
    api, broker, _, run, prepared = app_output
    store = broker._store()
    if mismatch == "epoch":
        broker.epoch = "1" * 32
    elif mismatch == "options":
        run.options = AppSessionOptions(app_id="local.stress")
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
    elif mismatch == "missing":
        broker.app_sessions.jobs.clear()
    else:
        store.set_job("app-logs", "queued")
    _, result = await answer(api, payload(), "job-logs")
    assert result == {"ok": False, "error": "unavailable"} and broker.backend.calls == 0


async def test_expiry_and_flood_preserve_bounded_private_cursor_stream(app_output):
    api, broker, _, run, _ = app_output
    for _ in range(300):
        run.events.log("warning: secret=SYNTHETIC_SECRET " + "\x01" * 2000)
    status, result = await answer(api, payload(), "job-logs")
    assert status == 200 and result["ok"] and len(result["entries"]) == 16
    assert result["has_more"] and result["gap"] and result["oldest_sequence"] > 1
    assert len(json.dumps(result).encode()) < 512 * 1024
    assert "SYNTHETIC_SECRET" not in json.dumps(result)
    assert all(len(entry["text"].encode()) <= 1024 for entry in result["entries"])
    _, after = await answer(api, payload(cursor=result["next_cursor"]), "job-logs")
    assert after["entries"][0]["sequence"] > result["entries"][-1]["sequence"]
    run.events.logs.deadline = 0
    _, expired = await answer(api, payload(cursor=after["next_cursor"]), "job-logs")
    assert expired["ok"] and expired["status"] == "expired" and expired["entries"] == []
    assert expired["gap"] and not expired["has_more"] and broker.backend.calls == 0


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


def selected_app_view(initial, logs=None):
    from test_gui_live_presentation import live_window
    from test_gui_live_state import ready

    from cardputer_mcp.gui_live_presentation import LogView, read_log_result
    from cardputer_mcp.gui_live_state import select

    window = live_window()
    window.state = select(ready(initial), "app-logs")
    window.logs = read_log_result(LogView(), logs) if logs else LogView()
    window.log_requests = window.log_requests.settled()
    return window


def deliver_app_delta(window, update, logs):
    from test_gui_live_presentation import drain

    from cardputer_mcp.gui_live_transport import TransportEvent

    generation = window.state.generation
    drain(window, TransportEvent("message", generation, update, time.monotonic()))
    window.runner.read_logs.assert_called_once_with(generation, "app-logs", window.logs.cursor)
    assert window.log_requests.pending
    response = {
        "job_id": "app-logs",
        "request_id": window.log_requests.request_id,
        "response": logs,
    }
    drain(window, TransportEvent("logs", generation, response, time.monotonic()))
    assert not window.log_requests.pending
    assert window.state.connection == "live"


async def test_app_log_append_wakes_live_reader_without_publishing_private_text(app_output):
    api, broker, current, run, _ = app_output
    before = broker.operator_changes.revision()
    run.events.emit("ack", control_only="SYNTHETIC_CONTROL_NOT_LOG")
    assert broker.operator_changes.revision() == before
    _, initial = await answer(api, {"cursor": None})
    window = selected_app_view(initial)
    held, output, _ = request(api, {"cursor": initial["cursor"]})
    await waiting(api)
    await asyncio.to_thread(run.events.log, "info: SYNTHETIC_OWNER_OUTPUT token=PRIVATE_TOKEN")
    await asyncio.wait_for(held, 2)
    delta = json.loads(output[1]["body"])
    assert delta["kind"] == "delta" and delta["logs_changed"]
    assert "SYNTHETIC_OWNER_OUTPUT" not in json.dumps(delta)
    _, logs = await answer(api, payload(), "job-logs")
    assert logs["ok"] and "SYNTHETIC_OWNER_OUTPUT" in logs["entries"][0]["text"]
    assert "PRIVATE_TOKEN" not in json.dumps(logs)
    deliver_app_delta(window, delta, logs)
    assert window.logs.phase == "available"
    assert "SYNTHETIC_OWNER_OUTPUT" in "\n".join(window.logs.lines)
    assert "PRIVATE_TOKEN" not in "\n".join(window.logs.lines)
    current[0] = actor("other")
    assert (await answer(api, payload(), "job-logs"))[1] == {
        "ok": False,
        "error": "not_authorized",
    }
    assert not broker.operator_changes._readers and broker.backend.calls == 0


async def test_app_log_retention_deadline_wakes_live_reader_and_clears_output(app_output):
    api, broker, _, run, _ = app_output
    # Prove the held read's retention timer works without periodic maintenance.
    broker.maintenance.cancel()
    await asyncio.gather(broker.maintenance, return_exceptions=True)
    broker.maintenance = None
    run.events.log("info: SYNTHETIC_EXPIRING_OUTPUT")
    _, logs = await answer(api, payload(), "job-logs")
    assert logs["entries"]
    run.events.logs.deadline = time.monotonic() + 0.05
    _, initial = await answer(api, {"cursor": None})
    window = selected_app_view(initial, logs)
    _, expired_delta = await answer(api, {"cursor": initial["cursor"]})
    assert expired_delta["kind"] == "delta" and expired_delta["logs_changed"]
    assert "SYNTHETIC_EXPIRING_OUTPUT" not in json.dumps(expired_delta)
    _, expired = await answer(api, payload(cursor=logs["next_cursor"]), "job-logs")
    assert expired["ok"] and expired["status"] == "expired"
    assert expired["entries"] == [] and expired["gap"] and not expired["has_more"]
    deliver_app_delta(window, expired_delta, expired)
    assert window.logs.phase == "expired" and not window.logs.lines
    assert not run.events.logs.entries and run.events.logs.bytes == 0
    assert api.live.waiters == 0 and broker.backend.calls == 0
