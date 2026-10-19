"""Guarded localhost subscriptions plus deterministic ASGI wait/auth boundaries."""

import asyncio
import json
from types import SimpleNamespace

import httpx2
import pytest
from test_authenticated_mock import authenticated_mock as authenticated_mock
from test_operator_timeline import prepare, running

from cardputer_mcp import operator_live
from cardputer_mcp.broker import Broker, RetainedCapture, RetainedDeviceObservation, digest_plan
from cardputer_mcp.build_logs import BuildLogs
from cardputer_mcp.capture import mock_image
from cardputer_mcp.config import SerialSettings, Settings
from cardputer_mcp.device_observation import DeviceLogCollector
from cardputer_mcp.gui_live_state import LiveState, apply, begin
from cardputer_mcp.models import Origin
from cardputer_mcp.models import TestName as Action
from cardputer_mcp.operator_api import OperatorAPI
from cardputer_mcp.ports import DeviceSelector, PortInfo
from cardputer_mcp.serial_backend import SerialBackend

LOCAL = "a" * 64


def actor(name="owner", scopes=frozenset({"read", "control"})):
    return SimpleNamespace(principal_id=name, origin=Origin.LOCAL, scopes=scopes)


@pytest.fixture
async def operator(tmp_path):
    broker = Broker(Settings(state_dir=tmp_path / "state"), lock_path=tmp_path / "device.lock")
    await broker.start()
    current = [actor()]

    async def unused(scope, receive, send):
        raise AssertionError("unexpected route")

    api = OperatorAPI(unused, broker, lambda header: current[0])
    try:
        yield api, broker, current
    finally:
        await broker.close()


def request(api, body, route="live", *, raw=None):
    received = asyncio.Queue()
    received.put_nowait(
        {"type": "http.request", "body": json.dumps(body).encode() if raw is None else raw}
    )
    output = []

    async def send(message):
        output.append(message)

    task = asyncio.create_task(
        api(
            {
                "type": "http",
                "path": "/operator/" + route,
                "method": "POST",
                "query_string": b"",
                "headers": [(b"content-type", b"application/json")],
            },
            received.get,
            send,
        )
    )
    return task, output, received


async def answer(api, body, route="live", *, raw=None):
    task, output, _ = request(api, body, route, raw=raw)
    await asyncio.wait_for(task, 2)
    return output[0]["status"], json.loads(output[1]["body"])


async def waiting(api, count=1):
    async with asyncio.timeout(2):
        while api.live.waiters != count:
            await asyncio.sleep(0)


async def test_guarded_localhost_snapshot_commit_log_wakeup_and_auth(authenticated_mock):
    url, broker = authenticated_mock
    async with httpx2.AsyncClient(
        base_url=url.removesuffix("/mcp"),
        trust_env=False,
        headers={"Authorization": "Bearer " + LOCAL},
    ) as http:
        response = await http.post("/operator/live", json={"cursor": None})
        snapshot = response.json()
        assert response.headers["cache-control"] == "no-store"
        assert snapshot["ok"] and snapshot["kind"] == "snapshot"
        assert snapshot["schema_version"] == 1
        model = begin(LiveState())
        model = apply(model, snapshot, model.generation, asyncio.get_running_loop().time())
        assert model.connection == "live" and model.cursor == snapshot["cursor"]
        assert model.snapshot is not None and model.timeline is not None
        assert snapshot["state"]["broker_epoch"] == snapshot["timeline"]["broker_epoch"]
        held = asyncio.create_task(http.post("/operator/live", json={"cursor": snapshot["cursor"]}))
        await asyncio.sleep(0.03)
        assert not held.done()
        broker._store().add_job("committed", "c" * 64)
        delta = (await asyncio.wait_for(held, 2)).json()
        assert delta["kind"] == "delta" and delta["base_cursor"] == snapshot["cursor"]
        assert delta["upsert"][0]["job_id"] == "committed" and delta["order"] == ["committed"]
        model = apply(model, delta, model.generation, asyncio.get_running_loop().time())
        assert model.connection == "live" and model.card("committed") is not None
        assert model.cursor == delta["cursor"]
        log = BuildLogs("ephemeral:local_cli", on_change=broker.operator_changes.notify_logs)
        held = asyncio.create_task(http.post("/operator/live", json={"cursor": delta["cursor"]}))
        await asyncio.sleep(0.03)
        log.append("warning: retained output")
        logs = (await asyncio.wait_for(held, 2)).json()
        assert logs["kind"] == "delta" and logs["logs_changed"]
        assert logs["upsert"] == [] and "retained output" not in json.dumps(logs)
        model = apply(model, logs, model.generation, asyncio.get_running_loop().time())
        assert model.connection == "live" and model.cursor == logs["cursor"]
        for headers, status in (
            ({"Authorization": "Bearer " + "b" * 64}, 403),
            ({"Authorization": "Bearer invalid"}, 401),
            ({"Origin": "https://example.invalid"}, 403),
        ):
            denied = await http.post("/operator/live", json={"cursor": None}, headers=headers)
            assert denied.status_code == status
    assert broker.backend.calls == 0


async def test_heartbeat_is_unchanged_and_commit_before_subscribe_cannot_be_lost(
    operator, monkeypatch
):
    api, broker, _ = operator
    monkeypatch.setattr(operator_live, "HEARTBEAT_SECONDS", 0.01)
    _, initial = await answer(api, {"cursor": None})
    _, beat = await answer(api, {"cursor": initial["cursor"]})
    assert beat == dict(ok=True, schema_version=1, kind="heartbeat", cursor=initial["cursor"])
    broker._store().add_job("race", "f" * 64)
    _, delta = await answer(api, {"cursor": initial["cursor"]})
    assert delta["kind"] == "delta" and delta["upsert"][0]["job_id"] == "race"
    assert api.live.waiters == 0


@pytest.mark.parametrize("change", [None, "read_only", "remote", "another_owner"])
async def test_current_grant_rechecked_after_held_request(operator, change):
    api, broker, current = operator
    _, initial = await answer(api, {"cursor": None})
    task, output, _ = request(api, {"cursor": initial["cursor"]})
    await waiting(api)
    current[0] = (
        None if change is None else actor("other" if change == "another_owner" else "owner")
    )
    if change == "read_only":
        current[0].scopes = frozenset({"read"})
    if change == "remote":
        current[0].origin = Origin.REMOTE
    broker.operator_changes.notify()
    await task
    assert api.live.waiters == 0
    if change == "another_owner":
        assert json.loads(output[1]["body"])["kind"] == "reset"
    else:
        assert output[0]["status"] == (401 if change is None else 403)
        assert b"timeline" not in output[1]["body"]


async def test_reader_limit_disconnect_and_cancellation_release_accounting(operator):
    api, broker, _ = operator
    _, initial = await answer(api, {"cursor": None})
    readers = [request(api, {"cursor": initial["cursor"]}) for _ in range(8)]
    await waiting(api, 8)
    _, denied = await answer(api, {"cursor": initial["cursor"]})
    assert denied == {"ok": False, "error": "unavailable"}
    readers[0][2].put_nowait({"type": "http.disconnect"})
    await readers[0][0]
    assert readers[0][1] == []
    readers[1][0].cancel()
    with pytest.raises(asyncio.CancelledError):
        await readers[1][0]
    await waiting(api, 6)
    broker._store().add_job("unblocked", "c" * 64)
    await asyncio.gather(*(reader[0] for reader in readers[2:]))
    assert api.live.waiters == 0 and broker._store().job("unblocked")["state"] == "queued"
    assert not broker.operator_changes._readers


async def test_baselines_principal_retention_ahead_and_old_epoch_reset(operator):
    api, broker, current = operator
    _, initial = await answer(api, {"cursor": None})
    current[0] = actor("stranger")
    assert (await answer(api, {"cursor": initial["cursor"]}))[1]["kind"] == "reset"
    current[0] = actor()
    for _ in range(33):
        broker.operator_changes.notify()
        await answer(api, {"cursor": None})
    assert len(api.live.baselines) == 32
    for cursor in (
        initial["cursor"],
        {"epoch": "0" * 32, "sequence": 0},
        {"epoch": broker.epoch, "sequence": 2**63 - 1},
    ):
        assert (await answer(api, {"cursor": cursor}))[1]["kind"] == "reset"


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"cursor": None, "extra": True},
        {"cursor": {"epoch": "a" * 32, "sequence": True}},
        {"cursor": {"epoch": "A" * 32, "sequence": 0}},
        {"cursor": {"epoch": "a" * 32, "sequence": -1}},
    ],
)
async def test_strict_cursor_json(operator, body):
    api, _, _ = operator
    assert (await answer(api, body))[0] == 400


async def test_duplicate_and_oversized_json(operator):
    api, _, _ = operator
    assert (await answer(api, None, raw=b'{"cursor":null,"cursor":null}'))[0] == 400
    assert (await answer(api, None, raw=b" " * 2049))[0] == 413


@pytest.mark.parametrize(
    "test,field,service_name",
    [
        ("firmware_build", "build_principal", "firmware_build"),
        ("firmware_fetch_source", "source_principal", "firmware_source"),
        ("firmware_flash", "flash_principal", "firmware_flash"),
    ],
)
async def test_owned_ram_logs_redaction_permission_expiry_and_no_receipt_io(
    operator, monkeypatch, test, field, service_name
):
    api, broker, current = operator
    plan = {"test": test, field: "owner"}
    digest = digest_plan(plan)
    broker._store().put_plan(digest, plan)
    broker._store().add_job("logs", digest)
    service = getattr(broker, service_name)
    logs = service.logs["logs"] = BuildLogs("owner")
    for _ in range(20):
        logs.append("warning: token=fixture-secret " + "\x01" * 4000)
    monkeypatch.setattr(service, "read_logs", lambda *a, **k: pytest.fail("receipt IO"))
    payload = {"job_id": "logs", "levels": ["warning"], "context": 0, "limit": 100}
    _, result = await answer(api, payload, "job-logs")
    assert result["status"] == "available" and len(result["entries"]) == 16
    assert result["has_more"] and result["next_cursor"]["sequence"] == 16
    assert "fixture-secret" not in json.dumps(result)
    assert len(json.dumps(result).encode()) < 512 * 1024
    _, rest = await answer(api, {**payload, "cursor": result["next_cursor"]}, "job-logs")
    assert [item["sequence"] for item in rest["entries"]] == [17, 18, 19, 20]
    current[0] = actor("stranger")
    assert (await answer(api, payload, "job-logs"))[1] == {"ok": False, "error": "not_authorized"}
    current[0] = actor(scopes=frozenset({"control"}))
    assert (await answer(api, payload, "job-logs"))[1]["error"] == "not_authorized"
    current[0] = actor()
    logs.deadline = 0
    _, expired = await answer(api, payload, "job-logs")
    assert expired["status"] == "expired" and expired["entries"] == []
    service.logs.clear()
    assert (await answer(api, payload, "job-logs"))[1]["error"] == "unavailable"
    assert broker.backend.calls == 0


async def test_common_revision_pair_retries_boundedly(operator, monkeypatch):
    api, broker, _ = operator
    original = api.timeline.snapshot
    calls = []

    def capture():
        calls.append(True)
        if len(calls) == 1:
            broker._store().add_job("during-capture", "f" * 64)
        return original()

    monkeypatch.setattr(api.timeline, "snapshot", capture)
    _, result = await answer(api, {"cursor": None})
    assert result["kind"] == "snapshot" and len(calls) == 2
    assert result["timeline"]["cards"][0]["job_id"] == "during-capture"

    def unstable():
        broker.operator_changes.notify()
        return original()

    monkeypatch.setattr(api.timeline, "snapshot", unstable)
    assert (await answer(api, {"cursor": None}))[1] == {"ok": False, "error": "unavailable"}


async def test_delta_retention_removes_old_card_and_preserves_timeline_order(operator):
    api, broker, _ = operator
    broker._store().add_job("old", "f" * 64)
    broker._store().set_job("old", "succeeded", "mock_pass")
    _, initial = await answer(api, {"cursor": None})
    for index in range(32):
        job_id = f"new-{index}"
        broker._store().add_job(job_id, "f" * 64)
        broker._store().set_job(job_id, "succeeded", "mock_pass")
    _, delta = await answer(api, {"cursor": initial["cursor"]})
    assert delta["remove"] == ["old"] and delta["has_more"]
    assert delta["order"] == [f"new-{index}" for index in reversed(range(32))]
    assert [card["job_id"] for card in delta["upsert"]] == delta["order"]


async def test_job_log_internal_failure_is_fixed_unavailable(operator, monkeypatch):
    api, broker, _ = operator

    def failure(job):
        raise ValueError("private diagnostic")

    monkeypatch.setattr(broker._store(), "job", failure)
    status, body = await answer(api, {"job_id": "fixture"}, "job-logs")
    assert status == 200 and body == {"ok": False, "error": "unavailable"}


async def test_live_envelopes_with_changed_state_reset_and_heartbeat_apply(operator, monkeypatch):
    api, broker, _ = operator
    _, snapshot = await answer(api, {"cursor": None})
    model = begin(LiveState())
    model = apply(model, snapshot, model.generation, 1000)
    assert model.connection == "live"
    # Existing state projections are unchanged; only the live envelope adds ok.
    assert "ok" not in broker.operator_snapshot()
    monkeypatch.setattr(
        broker,
        "operator_snapshot",
        lambda: {
            **snapshot["state"],
            "needs_review": True,
        },
    )
    broker.operator_changes.notify()
    _, delta = await answer(api, {"cursor": snapshot["cursor"]})
    assert delta["state"]["ok"] is True
    model = apply(model, delta, model.generation, 1001)
    assert model.connection == "live" and model.snapshot.needs_review
    monkeypatch.setattr(operator_live, "HEARTBEAT_SECONDS", 0.01)
    _, heartbeat = await answer(api, {"cursor": model.cursor})
    assert heartbeat["kind"] == "heartbeat"
    assert apply(model, heartbeat, model.generation, 1002) is model
    _, reset = await answer(api, {"cursor": {"epoch": "0" * 32, "sequence": 0}})
    reconnecting = begin(model)
    restored = apply(reconnecting, reset, reconnecting.generation, 1003)
    assert restored.connection == "live" and restored.cursor == reset["cursor"]


async def test_reconnect_snapshot_does_not_reuse_expired_capture_baseline(tmp_path):
    async with running(tmp_path) as (broker, _, _, clock):
        principal = actor("synthetic-owner")
        api = OperatorAPI(None, broker, lambda _: principal)
        plan = prepare(broker, test=Action.CAPTURE)
        broker._store().add_job("one", plan["plan_hash"])
        broker._store().set_job("one", "succeeded", "synthetic_capture_not_device")
        broker.captures["one"] = RetainedCapture(
            "synthetic-owner",
            mock_image(),
            clock.now + 2,
            "local_cli",
            "2026-10-18T10:00:00+00:00",
        )
        _, first = await answer(api, {"cursor": None})
        assert first["timeline"]["cards"][0]["image"]["remaining_seconds"] == 2
        clock.now += 1
        _, before_expiry = await answer(api, {"cursor": None})
        assert before_expiry["timeline"]["cards"][0]["image"]["remaining_seconds"] == 1
        assert before_expiry["cursor"] != first["cursor"]
        clock.now += 2
        _, second = await answer(api, {"cursor": None})
        assert second["timeline"]["cards"][0]["image"] is None
        assert second["cursor"] != first["cursor"]
        assert first["timeline"]["cards"][0]["image"]["remaining_seconds"] == 2
        model = begin(LiveState())
        model = apply(model, first, model.generation, 1000)
        model = begin(model)
        model = apply(model, second, model.generation, 1003)
        assert model.connection == "live" and model.card("one").image is None


async def test_retention_deadline_wakes_held_read_without_periodic_projection(operator):
    api, broker, _ = operator
    # Stop the broker's existing maintenance timer to prove the subscription's
    # nearest expiry deadline independently wakes this held read.
    broker.maintenance.cancel()
    await asyncio.gather(broker.maintenance, return_exceptions=True)
    broker.maintenance = None
    plan = prepare(broker, test=Action.CAPTURE)
    broker._store().add_job("one", plan["plan_hash"])
    broker._store().set_job("one", "succeeded", "synthetic_capture_not_device")
    broker.captures["one"] = RetainedCapture(
        "synthetic-owner",
        mock_image(),
        broker.clock() + 0.05,
        "local_cli",
        "2026-10-18T10:00:00+00:00",
    )
    _, first = await answer(api, {"cursor": None})
    assert first["timeline"]["cards"][0]["image"] is not None
    _, expired = await answer(api, {"cursor": first["cursor"]})
    assert expired["kind"] == "delta" and expired["upsert"][0]["image"] is None
    assert "one" not in broker.captures and broker.backend.calls == 0


@pytest.mark.parametrize("reason", ["revoked", "expired"])
async def test_report_retention_wakes_reader_and_removes_metadata(tmp_path, reason, monkeypatch):
    monkeypatch.setattr(operator_live, "HEARTBEAT_SECONDS", 0.02)
    config = SerialSettings(
        selector=DeviceSelector(vid=0xCAFE, pid=1, serial_number="synthetic"),
        expected_firmware_sha256="a" * 64,
    )
    backend = SerialBackend(
        config,
        inventory=lambda: [PortInfo(port="COM42", vid=0xCAFE, pid=1, serial_number="synthetic")],
        factory=lambda: pytest.fail("no device IO"),
    )
    async with running(tmp_path, backend=backend) as (broker, _, _, clock):
        principal = actor("operator")
        api = OperatorAPI(None, broker, lambda _: principal)
        plan = prepare(broker, test=Action.FIRMWARE)
        broker._store().add_job("one", plan["plan_hash"])
        broker._store().set_job("one", "succeeded", "firmware_observation_completed_no_commands")
        broker.device_observations["one"] = RetainedDeviceObservation(
            "synthetic-owner",
            "local_cli",
            DeviceLogCollector().finish(),
            clock.now + 200,
        )
        _, first = await answer(api, {"cursor": None})
        assert first["timeline"]["cards"][0]["device_observation"]
        held, output, _ = request(api, {"cursor": first["cursor"]})
        await waiting(api)
        if reason == "revoked":
            broker.principal_valid = lambda principal_id, *_: principal_id != "synthetic-owner"
        else:
            clock.now += 201
        await asyncio.wait_for(held, 2)
        removed = json.loads(output[1]["body"])
        assert removed["kind"] == "delta"
        assert "device_observation" not in removed["upsert"][0]
        assert "one" not in broker.device_observations
        _, reset = await answer(api, {"cursor": None})
        assert "device_observation" not in reset["timeline"]["cards"][0]
        assert backend.calls == 0 and not backend.hardware_connected


async def test_normal_heartbeat_avoids_full_projection_and_report_read(operator, monkeypatch):
    api, broker, _ = operator
    _, snapshot = await answer(api, {"cursor": None})
    monkeypatch.setattr(operator_live, "HEARTBEAT_SECONDS", 0.01)
    monkeypatch.setattr(api.timeline, "snapshot", lambda: pytest.fail("heartbeat projection"))
    monkeypatch.setattr(broker, "operator_snapshot", lambda: pytest.fail("heartbeat projection"))
    monkeypatch.setattr(
        broker, "operator_device_observation_metadata", lambda _: pytest.fail("no report baseline")
    )
    _, heartbeat = await answer(api, {"cursor": snapshot["cursor"]})
    assert heartbeat["kind"] == "heartbeat" and heartbeat["cursor"] == snapshot["cursor"]
