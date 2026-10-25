"""Guarded localhost event streams plus deterministic ASGI stream/auth boundaries."""

import asyncio
import json
import socket
from types import SimpleNamespace

import httpx2
import pytest
import uvicorn
from server_ready import LOG_LEVEL, wait_until_started
from test_authenticated_mock import authenticated_mock as authenticated_mock
from test_authenticated_mock import event_loop_policy as event_loop_policy
from test_operator_timeline import prepare, running

from cardputer_mcp import cli, operator_live
from cardputer_mcp.auth import AuthState
from cardputer_mcp.broker import Broker, RetainedDeviceObservation, digest_plan
from cardputer_mcp.build_logs import BuildLogs
from cardputer_mcp.config import SerialSettings, Settings
from cardputer_mcp.device_observation import DeviceLogCollector
from cardputer_mcp.gui_live_state import LiveState, apply, begin
from cardputer_mcp.gui_live_transport import EventStreamParser
from cardputer_mcp.models import Origin
from cardputer_mcp.models import TestName as Action
from cardputer_mcp.operator_api import OperatorAPI
from cardputer_mcp.operator_live import OperatorLive
from cardputer_mcp.ports import DeviceSelector, PortInfo
from cardputer_mcp.serial_backend import SerialBackend
from cardputer_mcp.server import create_app

LOCAL = "a" * 64
REMOTE = "b" * 64
PING = object()


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


def request(api, body, route="job-logs", *, raw=None):
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


async def answer(api, body, route="job-logs", *, raw=None):
    task, output, _ = request(api, body, route, raw=raw)
    await asyncio.wait_for(task, 2)
    return output[0]["status"], json.loads(output[1]["body"])


class Stream:
    """One direct ASGI ``GET /operator/events``; events are parsed like the GUI does."""

    def __init__(self, api, *, method="GET", query=b"", gate=None):
        self.received = asyncio.Queue()
        self.received.put_nowait({"type": "http.request", "body": b""})
        self.messages = asyncio.Queue()
        self.parser = EventStreamParser()
        self.items = []
        self.gate = gate
        self.task = asyncio.create_task(
            api(
                {
                    "type": "http",
                    "path": "/operator/events",
                    "method": method,
                    "query_string": query,
                    "headers": [],
                },
                self.received.get,
                self.send,
            )
        )

    async def send(self, message):
        if self.gate is not None and message.get("more_body"):
            await self.gate.wait()
        await self.messages.put(message)

    async def start(self):
        message = await asyncio.wait_for(self.messages.get(), 2)
        assert message["type"] == "http.response.start"
        return message

    async def json_refusal(self):
        start = await self.start()
        body = await asyncio.wait_for(self.messages.get(), 2)
        await asyncio.wait_for(self.task, 2)
        return start["status"], json.loads(body["body"])

    async def next(self):
        """The next data event (dict) or heartbeat comment (PING); None at stream end."""
        async with asyncio.timeout(2):
            while not self.items:
                message = await self.messages.get()
                if message["type"] == "http.response.start":
                    continue
                self.items.extend(self.parser.feed(message.get("body", b"")))
                if not message.get("more_body", False) and not self.items:
                    return None
        item = self.items.pop(0)
        return PING if item is None else json.loads(item)

    def pending(self):
        return not self.messages.empty() or bool(self.items)

    async def disconnect(self):
        self.received.put_nowait({"type": "http.disconnect"})
        await asyncio.wait_for(self.task, 2)


async def opened(api, *, gate=None):
    stream = Stream(api, gate=gate)
    start = await stream.start()
    assert start["status"] == 200
    headers = dict(start["headers"])
    assert headers[b"content-type"] == b"text/event-stream; charset=utf-8"
    assert headers[b"cache-control"] == b"no-store"
    return stream, await stream.next()


async def settled(api, count):
    async with asyncio.timeout(2):
        while api.live.streams != count:
            await asyncio.sleep(0)


class Reader:
    """Parses one real HTTP event stream; a heartbeat comment is returned as PING."""

    def __init__(self, response):
        self.chunks = response.aiter_bytes()
        self.parser = EventStreamParser()
        self.items = []

    async def next(self):
        async with asyncio.timeout(2):
            while not self.items:
                self.items.extend(self.parser.feed(await anext(self.chunks)))
        item = self.items.pop(0)
        return PING if item is None else json.loads(item)

    async def rest(self):
        remaining = b""
        async with asyncio.timeout(3):
            async for chunk in self.chunks:
                remaining += chunk
        return remaining


async def test_guarded_localhost_stream_snapshot_commit_log_wakeup_and_auth(authenticated_mock):
    url, broker = authenticated_mock
    async with httpx2.AsyncClient(
        base_url=url.removesuffix("/mcp"),
        trust_env=False,
        headers={"Authorization": "Bearer " + LOCAL},
        timeout=httpx2.Timeout(5.0),
    ) as http:
        async with http.stream("GET", "/operator/events") as response:
            reader = Reader(response)
            assert response.status_code == 200
            assert response.headers["content-type"].startswith("text/event-stream")
            assert response.headers["cache-control"] == "no-store"
            snapshot = await reader.next()
            assert snapshot["ok"] and snapshot["kind"] == "snapshot"
            assert snapshot["schema_version"] == 1
            model = begin(LiveState())
            model = apply(model, snapshot, model.generation, asyncio.get_running_loop().time())
            assert model.connection == "live" and model.cursor == snapshot["cursor"]
            assert model.snapshot is not None and model.timeline is not None
            assert snapshot["state"]["broker_epoch"] == snapshot["timeline"]["broker_epoch"]
            broker._store().add_job("committed", "c" * 64)
            delta = await reader.next()
            assert delta["kind"] == "delta" and delta["base_cursor"] == snapshot["cursor"]
            assert delta["upsert"][0]["job_id"] == "committed" and delta["order"] == ["committed"]
            model = apply(model, delta, model.generation, asyncio.get_running_loop().time())
            assert model.connection == "live" and model.card("committed") is not None
            assert model.cursor == delta["cursor"]
            log = BuildLogs("ephemeral:local_cli", on_change=broker.operator_changes.notify_logs)
            log.append("warning: retained output")
            logs = await reader.next()
            assert logs["kind"] == "delta" and logs["logs_changed"]
            assert logs["upsert"] == [] and "retained output" not in json.dumps(logs)
            model = apply(model, logs, model.generation, asyncio.get_running_loop().time())
            assert model.connection == "live" and model.cursor == logs["cursor"]
        for headers, status in (
            ({"Authorization": "Bearer " + REMOTE}, 403),
            ({"Authorization": "Bearer invalid"}, 401),
            ({"Origin": "https://example.invalid"}, 403),
        ):
            denied = await http.get("/operator/events", headers=headers)
            assert denied.status_code == status
            assert denied.headers["content-type"] == "application/json"
        # The long-poll route is gone; nothing else serves it.
        assert (await http.post("/operator/live", json={"cursor": None})).status_code == 404
    assert broker.backend.calls == 0


@pytest.mark.parametrize("change", [None, "read_only", "remote", "another_owner"])
async def test_current_grant_rechecked_on_every_wake(operator, change):
    api, broker, current = operator
    stream, initial = await opened(api)
    assert initial["kind"] == "snapshot"
    current[0] = (
        None if change is None else actor("other" if change == "another_owner" else "owner")
    )
    if change == "read_only":
        current[0].scopes = frozenset({"read"})
    if change == "remote":
        current[0].origin = Origin.REMOTE
    broker.operator_changes.notify()
    following = await stream.next()
    if change == "another_owner":
        assert following["kind"] == "reset" and "timeline" in following
        assert following["cursor"]["sequence"] > initial["cursor"]["sequence"]
        await stream.disconnect()
    else:
        assert following == {
            "ok": False,
            "error": "authentication_required" if change is None else "local_operator_required",
        }
        assert await stream.next() is None
        await asyncio.wait_for(stream.task, 2)
    assert api.live.streams == 0 and not broker.operator_changes._readers


async def test_stream_limit_disconnect_cancellation_and_close_release_accounting(operator):
    api, broker, _ = operator
    streams = [(await opened(api))[0] for _ in range(operator_live.MAX_STREAMS)]
    assert api.live.streams == operator_live.MAX_STREAMS
    status, denied = await Stream(api).json_refusal()
    assert (status, denied) == (200, {"ok": False, "error": "unavailable"})
    await streams[0].disconnect()
    streams[1].task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await streams[1].task
    await settled(api, operator_live.MAX_STREAMS - 2)
    broker._store().add_job("unblocked", "c" * 64)
    for stream in streams[2:]:
        assert (await stream.next())["upsert"][0]["job_id"] == "unblocked"
    api.live.close()
    for stream in streams[2:]:
        assert await stream.next() is None
        await asyncio.wait_for(stream.task, 2)
    assert api.live.streams == 0 and broker._store().job("unblocked")["state"] == "queued"
    assert not broker.operator_changes._readers and not api.live._wakes
    # Shutdown has begun: a new stream is refused before any response starts.
    status, refused = await Stream(api).json_refusal()
    assert (status, refused) == (200, {"ok": False, "error": "unavailable"})


async def test_broker_close_ends_open_stream(tmp_path):
    broker = Broker(Settings(state_dir=tmp_path / "state"), lock_path=tmp_path / "device.lock")
    await broker.start()
    api = OperatorAPI(None, broker, lambda header: actor())
    try:
        stream, _ = await opened(api)
    finally:
        await asyncio.wait_for(broker.close(), 5)
    assert await stream.next() is None
    await asyncio.wait_for(stream.task, 2)
    assert api.live.streams == 0


async def test_burst_is_coalesced_and_slow_consumer_holds_at_most_one_capture(operator):
    api, broker, _ = operator
    stream, first = await opened(api)
    for index in range(20):
        broker._store().add_job(f"burst-{index}", "c" * 64)
    delta = await stream.next()
    assert delta["kind"] == "delta" and len(delta["upsert"]) == 20
    await asyncio.sleep(0.05)
    assert not stream.pending()
    await stream.disconnect()

    gate = asyncio.Event()
    gated = Stream(api, gate=gate)
    await gated.start()
    captures = []
    original = api.live._capture

    def counted(principal):
        captures.append(principal)
        return original(principal)

    api.live._capture = counted
    for index in range(50):
        broker._store().add_job(f"slow-{index}", "c" * 64)
        await asyncio.sleep(0)
    await asyncio.sleep(0.05)
    # The snapshot send is still blocked: nothing was captured or queued behind it.
    assert captures == [] and gated.messages.empty()
    gate.set()
    assert (await gated.next())["kind"] == "snapshot"
    following = await gated.next()
    assert following["kind"] == "delta" and len(captures) == 1
    await asyncio.sleep(0.05)
    assert not gated.pending() and len(captures) == 1
    await gated.disconnect()
    assert api.live.streams == 0


async def test_expiry_recaptures_without_a_notification(operator, monkeypatch):
    api, broker, _ = operator
    deadlines = [broker.clock() + 0.05]
    monkeypatch.setattr(
        OperatorLive, "_next_expiry", lambda self: deadlines.pop() if deadlines else None
    )
    stream, first = await opened(api)
    revision = broker.operator_changes.revision()
    expired = await stream.next()
    assert expired["kind"] == "delta" and expired["logs_changed"] is True
    assert expired["base_cursor"] == first["cursor"]
    assert broker.operator_changes.revision()[1] == revision[1] + 1
    await asyncio.sleep(0.05)
    assert not stream.pending()
    await stream.disconnect()


async def test_idle_stream_sends_comment_heartbeats(operator, monkeypatch):
    api, broker, _ = operator
    monkeypatch.setattr(operator_live, "HEARTBEAT_SECONDS", 0.05)
    stream, _ = await opened(api)
    revision = broker.operator_changes.revision()
    assert await stream.next() is PING
    assert await stream.next() is PING
    assert broker.operator_changes.revision() == revision
    await stream.disconnect()
    assert api.live.streams == 0 and not broker.operator_changes._readers


@pytest.mark.parametrize(
    "method,query,status",
    [("POST", b"", 405), ("GET", b"cursor=1", 400)],
)
async def test_events_route_rejects_other_methods_and_queries(operator, method, query, status):
    api, _, _ = operator
    stream = Stream(api, method=method, query=query)
    assert (await stream.json_refusal())[0] == status
    assert api.live.streams == 0


@pytest.mark.parametrize("route", ["live", "events-missing"])
async def test_long_poll_route_is_removed(operator, route):
    api, _, _ = operator
    assert (await answer(api, {"cursor": None}, route))[0] == 404


async def test_uvicorn_shutdown_ends_open_streams_before_waiting(tmp_path):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    settings = Settings(state_dir=tmp_path / "state", port=port)
    resident = Broker(settings, lock_path=tmp_path / "device.lockfile")
    app = create_app(
        settings,
        resident,
        require_auth=True,
        auth=AuthState({Origin.LOCAL: LOCAL, Origin.REMOTE: REMOTE}),
    )
    # No graceful-shutdown timeout: without the hook an open stream holds forever.
    server = cli._Server(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=port,
            log_level=LOG_LEVEL,
            access_log=False,
            proxy_headers=False,
        )
    )
    task = asyncio.create_task(server.serve())
    try:
        await wait_until_started(server, task, "Operator stream server")
        async with httpx2.AsyncClient(
            base_url=f"http://127.0.0.1:{port}",
            trust_env=False,
            headers={"Authorization": "Bearer " + LOCAL},
            timeout=httpx2.Timeout(5.0),
        ) as http:
            async with http.stream("GET", "/operator/events") as response:
                reader = Reader(response)
                assert (await reader.next())["kind"] == "snapshot"
                server.should_exit = True
                assert b"data:" not in await reader.rest()
        await asyncio.wait_for(task, 3)
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 5)
    assert resident.store is None and not server.server_state.connections


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
    # The local operator reads any job's redacted output, not only its own.
    current[0] = actor("stranger")
    _, other = await answer(api, payload, "job-logs")
    assert other["status"] == "available" and "fixture-secret" not in json.dumps(other)
    current[0] = actor(scopes=frozenset({"control"}))
    assert (await answer(api, payload, "job-logs"))[1]["error"] == "not_authorized"
    current[0] = actor()
    logs.deadline = 0
    _, expired = await answer(api, payload, "job-logs")
    assert expired["status"] == "expired" and expired["entries"] == []
    service.logs.clear()
    assert (await answer(api, payload, "job-logs"))[1]["error"] == "unavailable"

    def failure(job):
        raise ValueError("private diagnostic")

    monkeypatch.setattr(broker._store(), "job", failure)
    status, body = await answer(api, {"job_id": "logs"}, "job-logs")
    assert status == 200 and body == {"ok": False, "error": "unavailable"}
    assert broker.backend.calls == 0


@pytest.mark.parametrize("reason", ["revoked", "expired"])
async def test_report_retention_wakes_stream_and_removes_metadata(tmp_path, reason, monkeypatch):
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
        stream, first = await opened(api)
        assert first["timeline"]["cards"][0]["device_observation"]
        if reason == "revoked":
            broker.principal_valid = lambda principal_id, *_: principal_id != "synthetic-owner"
        else:
            clock.now += 201
        removed = await stream.next()
        while removed is PING:
            removed = await stream.next()
        assert removed["kind"] == "delta"
        assert "device_observation" not in removed["upsert"][0]
        assert "one" not in broker.device_observations
        await stream.disconnect()
        again, reset = await opened(api)
        assert "device_observation" not in reset["timeline"]["cards"][0]
        await again.disconnect()
        assert backend.calls == 0 and not backend.hardware_connected


@pytest.mark.parametrize(
    ("test", "field", "stream"),
    [
        ("firmware_flash", "flash_principal", "flash"),
        ("firmware_build", "build_principal", "build"),
        ("passive_observe", None, "device"),
    ],
)
async def test_local_operator_reads_durable_logs_of_any_principal(operator, test, field, stream):
    api, broker, current = operator
    plan = {"test": test, **({field: "remote-owner"} if field else {})}
    digest = digest_plan(plan)
    broker._store().put_plan(digest, plan)
    broker._store().add_job("durable", digest)
    log = broker.job_logs.begin("durable", "remote-owner", test, keep=set())
    log.append(stream, "warning: retained line token=fixture-secret", "warning")
    log.close()
    current[0] = actor("local-operator")
    payload = {"job_id": "durable", "levels": ["warning"], "context": 0, "limit": 100}
    _, result = await answer(api, payload, "job-logs")
    assert result["status"] == "available" and result["source"] == "durable_job_log"
    assert [entry["text"].startswith("warning: retained line") for entry in result["entries"]] == [
        True
    ]
    assert "fixture-secret" not in json.dumps(result)
    current[0] = SimpleNamespace(
        principal_id="remote-owner", origin=Origin.REMOTE, scopes=frozenset({"read", "control"})
    )
    # The operator API stays local-only; remote clients keep owner-only MCP reads.
    assert (await answer(api, payload, "job-logs"))[1]["error"] == "local_operator_required"
