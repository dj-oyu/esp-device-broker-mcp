"""Read-only worker transport boundaries, delivery gaps, and cancellation."""

import asyncio
import json
import threading
import time
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from typing import Any

import httpx2
import pytest

from cardputer_mcp import gui_live_transport
from cardputer_mcp.gui_live_transport import (
    AUTH_FAILED,
    DELIVERY_GAP,
    IMAGE_EXPIRED,
    INVALID_RESPONSE,
    MAX_DELIVERIES,
    MAX_RESPONSE_BYTES,
    REJECTED,
    STARTUP_FAILED,
    LiveRunner,
)
from cardputer_mcp.operator_client import OperatorClient, OperatorClientError
from cardputer_mcp.operator_live import HEARTBEAT_SECONDS

EPOCH = "a" * 32
TOKEN = "test-private-token"


def envelope(kind="snapshot", sequence=1, **fields):
    return {
        "ok": True,
        "schema_version": 1,
        "kind": kind,
        "cursor": {"epoch": EPOCH, "sequence": sequence},
        **fields,
    }


def wait_until(predicate: Callable[[], Any], timeout=2):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(0.005)
    raise AssertionError("worker condition did not become ready")


@pytest.fixture
def runner_factory(monkeypatch):
    original = httpx2.AsyncClient
    runners = []
    options = []

    def make(handler):
        def client(**kwargs):
            options.append(kwargs)
            return original(**kwargs, transport=httpx2.MockTransport(handler))

        monkeypatch.setattr(httpx2, "AsyncClient", client)
        runner = LiveRunner(OperatorClient("http://127.0.0.1:8765/mcp", TOKEN))
        runners.append(runner)
        return runner

    yield make, options
    for runner in runners:
        runner.close()
        assert not runner._thread.is_alive()


def test_held_reads_advance_cursor_and_heartbeat_without_state_refresh(runner_factory):
    make, options = runner_factory
    requests = []
    held = threading.Event()

    async def handler(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx2.Response(
                200, json=envelope(), headers={"set-cookie": "unused=cookie; Path=/"}
            )
        if len(requests) == 2:
            return httpx2.Response(200, json=envelope("heartbeat"))
        held.set()
        await asyncio.Future()

    runner = make(handler)
    runner.start(1)
    assert held.wait(1)
    events = runner.drain()
    assert [event.payload["kind"] for event in events] == ["snapshot", "heartbeat"]
    assert all(event.kind == "message" and event.generation == 1 for event in events)
    assert all(event.started_at > 0 for event in events)
    assert all(request.url.path == "/operator/live" for request in requests)
    assert json.loads(requests[0].content) == {"cursor": None}
    assert [json.loads(request.content)["cursor"] for request in requests[1:]] == [
        {"epoch": EPOCH, "sequence": 1},
        {"epoch": EPOCH, "sequence": 1},
    ]
    assert all(request.headers["authorization"] == f"Bearer {TOKEN}" for request in requests)
    assert all(TOKEN not in str(request.url) for request in requests)
    assert all("cookie" not in request.headers for request in requests)
    assert options[0]["trust_env"] is False
    assert options[0]["follow_redirects"] is False
    # The client's inactivity timeout must outlast an idle server heartbeat plus
    # scheduling/network overhead; equal deadlines caused normal idle disconnects.
    assert options[0]["timeout"].read >= HEARTBEAT_SECONDS + 5.0
    assert options[0]["timeout"].connect == 2.0
    assert options[0]["timeout"].write == 5.0
    assert options[0]["timeout"].pool == 2.0


@pytest.mark.parametrize(
    "status,expected", [(302, REJECTED), (401, AUTH_FAILED), (403, AUTH_FAILED)]
)
def test_redirect_and_auth_failures_are_fixed_and_never_retried(
    runner_factory, status, expected, monkeypatch
):
    make, _ = runner_factory
    monkeypatch.setenv("HTTP_PROXY", "http://invalid-proxy.example:1234")
    monkeypatch.setenv("ALL_PROXY", "http://invalid-proxy.example:1234")
    requests = []

    async def handler(request):
        requests.append(request)
        return httpx2.Response(
            status,
            headers={"location": "http://other-host.example/private"},
            text=f"server secret {TOKEN}",
        )

    runner = make(handler)
    runner.start(1)
    events = wait_until(runner.drain)
    assert len(requests) == 1
    assert len(events) == 1
    assert events[0].kind == "disconnected"
    assert events[0].payload == {"error": expected}
    assert TOKEN not in repr(events)
    assert "other-host" not in repr(events)


def test_credential_validation_precedes_worker_and_no_secret_repr():
    class ForeignAPI:
        base_url = "https://remote.example/mcp"
        _token = TOKEN

    with pytest.raises(OperatorClientError, match="127.0.0.1"):
        LiveRunner(ForeignAPI())


@pytest.mark.parametrize(
    "response",
    [
        b"not json",
        b"[]",
        b'{"ok":false}',
        json.dumps(envelope("heartbeat")).encode(),
        json.dumps(envelope(cursor={"epoch": "invalid", "sequence": 0})).encode(),
    ],
)
def test_invalid_initial_envelopes_disconnect_with_safe_error(runner_factory, response):
    make, _ = runner_factory

    async def handler(request):
        return httpx2.Response(200, content=response)

    runner = make(handler)
    runner.start(1)
    events = wait_until(runner.drain)
    assert len(events) == 1
    assert events[0].kind == "disconnected"
    assert events[0].payload["error"] in (INVALID_RESPONSE, REJECTED)


def test_response_limit_stops_stream_before_parsing_unbounded_body(runner_factory):
    make, _ = runner_factory
    yielded = 0
    closed = threading.Event()

    class LargeBody(httpx2.AsyncByteStream):
        async def __aiter__(self):
            nonlocal yielded
            for _ in range(1000):
                yielded += 8192
                yield b"x" * 8192

        async def aclose(self):
            closed.set()

    async def handler(request):
        return httpx2.Response(200, stream=LargeBody())

    runner = make(handler)
    runner.start(1)
    events = wait_until(runner.drain)
    assert events[0].payload == {"error": INVALID_RESPONSE}
    assert yielded == MAX_RESPONSE_BYTES + 8192
    assert closed.is_set()


def test_content_length_limit_rejects_before_reading(runner_factory):
    make, _ = runner_factory
    read = threading.Event()

    class Body(httpx2.AsyncByteStream):
        async def __aiter__(self):
            read.set()
            yield b"{}"

    async def handler(request):
        return httpx2.Response(
            200, headers={"content-length": str(MAX_RESPONSE_BYTES + 1)}, stream=Body()
        )

    runner = make(handler)
    runner.start(1)
    assert wait_until(runner.drain)[0].payload == {"error": INVALID_RESPONSE}
    assert not read.is_set()


def test_reconnect_cancels_held_read_and_rejects_late_generation(runner_factory):
    make, _ = runner_factory
    requests = []
    old_held = threading.Event()
    old_cancelled = threading.Event()
    new_held = threading.Event()
    closed_cancelled = threading.Event()

    async def handler(request):
        requests.append(json.loads(request.content))
        if len(requests) == 1:
            old_held.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                old_cancelled.set()
                # Simulate an uncancellable response already completing.
                return httpx2.Response(200, json=envelope(sequence=99))
        if len(requests) == 2:
            return httpx2.Response(200, json=envelope(sequence=2))
        new_held.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            closed_cancelled.set()
            return httpx2.Response(200, json=envelope("heartbeat", sequence=2))

    runner = make(handler)
    runner.start(1)
    assert old_held.wait(1)
    runner.reconnect(2)
    assert new_held.wait(1)
    assert old_cancelled.is_set()
    events = runner.drain()
    assert len(events) == 1
    assert events[0].generation == 2
    assert events[0].payload["cursor"]["sequence"] == 2
    assert requests[:2] == [{"cursor": None}, {"cursor": None}]
    start = time.monotonic()
    runner.close()
    assert time.monotonic() - start < 1
    assert closed_cancelled.is_set()
    assert not runner._thread.is_alive()
    assert runner.drain() == []
    runner.reconnect(3)
    runner.close()


def test_mismatched_delta_base_stops_subscription(runner_factory):
    make, _ = runner_factory
    requests = []

    async def handler(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx2.Response(200, json=envelope())
        return httpx2.Response(
            200,
            json=envelope("delta", sequence=3, base_cursor={"epoch": EPOCH, "sequence": 2}),
        )

    runner = make(handler)
    runner.start(1)
    wait_until(lambda: len(requests) == 2)
    events = wait_until(lambda: result if len(result := runner.drain()) == 2 else None)
    assert [event.kind for event in events] == ["message", "disconnected"]
    assert events[-1].payload == {"error": INVALID_RESPONSE}
    assert len(requests) == 2


def test_overflow_discards_delta_chain_and_waits_for_explicit_new_generation(runner_factory):
    make, _ = runner_factory
    requests = []
    recovered = threading.Event()

    async def handler(request):
        payload = json.loads(request.content)
        requests.append(payload)
        if len(requests) > MAX_DELIVERIES + 1:
            if payload["cursor"] is None:
                recovered.set()
                return httpx2.Response(200, json=envelope(sequence=100))
            await asyncio.Future()
        if payload["cursor"] is None:
            return httpx2.Response(200, json=envelope())
        sequence = payload["cursor"]["sequence"]
        return httpx2.Response(
            200, json=envelope("delta", sequence + 1, base_cursor=payload["cursor"])
        )

    runner = make(handler)
    runner.start(1)
    wait_until(lambda: runner._gap_generation == 1)
    assert len(requests) == MAX_DELIVERIES + 1
    assert runner.drain()[0].payload == {"error": DELIVERY_GAP}
    assert runner.drain() == []
    runner.reconnect(1)
    assert len(requests) == MAX_DELIVERIES + 1
    runner.reconnect(2)
    assert recovered.wait(1)
    events = wait_until(runner.drain)
    assert events[0].generation == 2
    assert events[0].payload["kind"] == "snapshot"


def test_logs_are_bounded_selected_reads_with_per_request_identity(runner_factory):
    make, _ = runner_factory
    live_held = threading.Event()
    first_held = threading.Event()
    first_cancelled = threading.Event()
    requests = []

    async def handler(request):
        payload = json.loads(request.content)
        requests.append((request.url.path, payload))
        if request.url.path == "/operator/live":
            if payload["cursor"] is None:
                return httpx2.Response(200, json=envelope())
            live_held.set()
            await asyncio.Future()
        if payload["job_id"] == "job_A":
            first_held.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                first_cancelled.set()
                return httpx2.Response(200, json={"ok": True, "entries": ["late A"]})
        return httpx2.Response(200, json={"ok": True, "entries": ["selected B"]})

    runner = make(handler)
    runner.start(1)
    assert live_held.wait(1)
    runner.drain()
    old_id = runner.read_logs(1, "job_A")
    assert first_held.wait(1)
    new_id = runner.read_logs(1, "job_B", {"epoch": EPOCH, "sequence": 4})
    events = wait_until(runner.drain)
    assert first_cancelled.is_set()
    assert new_id > old_id
    assert len(events) == 1
    assert events[0].kind == "logs"
    assert events[0].payload == {
        "job_id": "job_B",
        "request_id": new_id,
        "response": {"ok": True, "entries": ["selected B"]},
    }
    logs = [payload for path, payload in requests if path == "/operator/job-logs"]
    assert len(logs) == 2
    assert logs[-1] == {
        "job_id": "job_B",
        "include_source_cache": False,
        "cursor": {"epoch": EPOCH, "sequence": 4},
        "levels": ["error", "warning", "info", "debug", "unknown"],
        "context": 0,
        "limit": 100,
    }
    with pytest.raises(ValueError):
        runner.read_logs(1, "../not-a-job")
    assert len(logs) == 2


def test_log_refusal_remains_separate_from_live_subscription(runner_factory):
    make, _ = runner_factory
    held = threading.Event()

    async def handler(request):
        if request.url.path == "/operator/job-logs":
            return httpx2.Response(200, json={"ok": False, "error": "not_authorized"})
        if json.loads(request.content)["cursor"] is None:
            return httpx2.Response(200, json=envelope())
        held.set()
        await asyncio.Future()

    runner = make(handler)
    runner.start(1)
    assert held.wait(1)
    runner.drain()
    request_id = runner.read_logs(1, "job_A")
    events = wait_until(runner.drain)
    assert events[0].payload == {
        "job_id": "job_A",
        "request_id": request_id,
        "response": {"ok": False, "error": "not_authorized"},
    }
    assert not runner._live_task.done()


def test_real_localhost_auth_ignores_proxies_and_does_not_follow_redirect(monkeypatch):
    requests = []
    sink_requests = []

    class Sink(BaseHTTPRequestHandler):
        def do_POST(self):
            sink_requests.append(self.headers.get("Authorization"))
            self.send_error(500)

        def log_message(self, *args):
            pass

    sink = ThreadingHTTPServer(("127.0.0.1", 0), Sink)

    class Redirect(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            requests.append((self.path, self.headers.get("Authorization"), json.loads(body)))
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{sink.server_port}/capture")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Redirect)
    workers = [
        threading.Thread(target=listener.serve_forever, kwargs={"poll_interval": 0.01})
        for listener in (server, sink)
    ]
    for worker in workers:
        worker.start()
    for key in ("HTTP_PROXY", "ALL_PROXY", "http_proxy", "all_proxy"):
        monkeypatch.setenv(key, "http://127.0.0.1:1")
    monkeypatch.setenv("NO_PROXY", "")
    monkeypatch.setenv("no_proxy", "")
    runner = None
    try:
        runner = LiveRunner(OperatorClient(f"http://127.0.0.1:{server.server_port}", TOKEN))
        runner.start(1)
        events = wait_until(runner.drain)
        assert events[0].payload == {"error": REJECTED}
        assert requests == [("/operator/live", f"Bearer {TOKEN}", {"cursor": None})]
        assert sink_requests == []
    finally:
        try:
            if runner is not None:
                runner.close()
        finally:
            for listener in (server, sink):
                listener.shutdown()
                listener.server_close()
            for worker in workers:
                worker.join(timeout=1)
                assert not worker.is_alive()


def test_pause_cancels_both_reads_and_requires_explicit_new_generation(runner_factory):
    make, _ = runner_factory
    requests = []
    live_held = threading.Event()
    logs_held = threading.Event()
    live_cancelled = threading.Event()
    logs_cancelled = threading.Event()
    resumed = threading.Event()
    snapshots = 0

    async def handler(request):
        nonlocal snapshots
        payload = json.loads(request.content)
        requests.append((request.url.path, payload))
        if request.url.path == "/operator/job-logs":
            logs_held.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                logs_cancelled.set()
                return httpx2.Response(200, json={"ok": True, "entries": ["late output"]})
        if payload["cursor"] is None:
            snapshots += 1
            if snapshots == 2:
                resumed.set()
            return httpx2.Response(200, json=envelope(sequence=snapshots))
        live_held.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            live_cancelled.set()
            return httpx2.Response(200, json=envelope("heartbeat", sequence=snapshots))

    runner = make(handler)
    runner.start(1)
    assert live_held.wait(1)
    runner.drain()
    runner.read_logs(1, "job_A")
    assert logs_held.wait(1)
    runner.pause(0)
    assert runner._current(1)
    runner.pause(1)
    assert live_cancelled.wait(1)
    assert logs_cancelled.wait(1)
    assert runner.drain() == []
    assert not runner._current(1)
    runner.read_logs(1, "job_B")
    runner.reconnect(1)
    assert len(requests) == 3
    runner.reconnect(2)
    assert resumed.wait(1)
    events = wait_until(runner.drain)
    assert len(events) == 1
    assert events[0].generation == 2
    assert events[0].payload["kind"] == "snapshot"
    # A late pause issued for the replaced view cannot cancel its replacement.
    runner.pause(1)
    assert runner._current(2)
    assert [payload for path, payload in requests if path == "/operator/job-logs"] == [
        {
            "job_id": "job_A",
            "include_source_cache": False,
            "cursor": None,
            "levels": ["error", "warning", "info", "debug", "unknown"],
            "context": 0,
            "limit": 100,
        }
    ]


def test_client_startup_failure_is_bounded_and_never_prints_private_error(monkeypatch, capsys):
    workers = []

    def fail(self):
        workers.append(self._thread)
        raise RuntimeError(f"private transport URL http://private.example/{TOKEN}")

    monkeypatch.setattr(LiveRunner, "_make_client", fail)
    started = time.monotonic()
    with pytest.raises(OperatorClientError) as caught:
        LiveRunner(OperatorClient("http://127.0.0.1:8765", TOKEN))
    assert time.monotonic() - started < 1
    assert str(caught.value) == STARTUP_FAILED
    workers[0].join(timeout=1)
    assert not workers[0].is_alive()
    captured = capsys.readouterr()
    assert captured.out == captured.err == ""


def test_hung_client_startup_times_out_and_eventually_releases_owned_resources(monkeypatch, capsys):
    entered = threading.Event()
    release = threading.Event()
    closed = threading.Event()
    workers = []

    class DelayedClient:
        async def aclose(self):
            closed.set()

    def delayed(self):
        workers.append(self._thread)
        entered.set()
        release.wait(timeout=2)
        return DelayedClient()

    monkeypatch.setattr(gui_live_transport, "STARTUP_WAIT_SECONDS", 0.05)
    monkeypatch.setattr(LiveRunner, "_make_client", delayed)
    started = time.monotonic()
    try:
        with pytest.raises(OperatorClientError, match=STARTUP_FAILED):
            LiveRunner(OperatorClient("http://127.0.0.1:8765", TOKEN))
        assert entered.is_set()
        assert time.monotonic() - started < 0.5
    finally:
        release.set()
        for worker in workers:
            worker.join(timeout=1)
            assert not worker.is_alive()
    assert closed.is_set()
    captured = capsys.readouterr()
    assert captured.out == captured.err == ""


def test_explicit_image_read_is_once_bounded_authenticated_and_independent_of_logs(runner_factory):
    make, options = runner_factory
    held = threading.Event()
    requests = []

    async def handler(request):
        requests.append(request)
        if request.url.path == "/operator/live":
            if json.loads(request.content)["cursor"] is None:
                return httpx2.Response(200, json=envelope())
            held.set()
            await asyncio.Future()
        if request.url.path == "/operator/image":
            return httpx2.Response(200, json={"ok": True, "retained": "preview JSON"})
        return httpx2.Response(200, json={"ok": True, "entries": []})

    runner = make(handler)
    runner.start(1)
    assert held.wait(1)
    assert all(request.url.path == "/operator/live" for request in requests)
    runner.drain()
    image_id = runner.read_image(1, "capture_A", expires_at=time.monotonic() + 10)
    events = wait_until(runner.drain)
    assert len(events) == 1
    assert events[0].kind == "image" and events[0].generation == 1
    assert events[0].payload == {
        "job_id": "capture_A",
        "request_id": image_id,
        "response": {"ok": True, "retained": "preview JSON"},
    }
    request = requests[-1]
    assert request.url.path == "/operator/image"
    assert json.loads(request.content) == {"job_id": "capture_A"}
    assert request.headers["authorization"] == f"Bearer {TOKEN}"
    assert TOKEN not in str(request.url) and TOKEN.encode() not in request.content
    assert request.extensions["timeout"]["read"] == 5.0
    assert options[0]["limits"].max_connections == 3
    assert options[0]["limits"].max_keepalive_connections == 3
    assert not runner._live_task.done()
    log_id = runner.read_logs(1, "capture_A")
    assert wait_until(runner.drain)[0].payload["request_id"] == log_id
    assert len([request for request in requests if request.url.path == "/operator/image"]) == 1
    with pytest.raises(ValueError):
        runner.read_image(1, "../invalid")


def test_replacing_image_read_rejects_late_a_b_a_completion_without_cancelling_logs(runner_factory):
    make, _ = runner_factory
    live_held = threading.Event()
    logs_held = threading.Event()
    image_held = threading.Event()
    cancelled = threading.Event()
    image_jobs = []

    async def handler(request):
        payload = json.loads(request.content)
        if request.url.path == "/operator/live":
            if payload["cursor"] is None:
                return httpx2.Response(200, json=envelope())
            live_held.set()
            await asyncio.Future()
        if request.url.path == "/operator/job-logs":
            logs_held.set()
            await asyncio.Future()
        image_jobs.append(payload["job_id"])
        if len(image_jobs) == 1:
            image_held.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                cancelled.set()
                return httpx2.Response(200, json={"ok": True, "body": "STALE A"})
        return httpx2.Response(200, json={"ok": True, "body": "current " + payload["job_id"]})

    runner = make(handler)
    runner.start(1)
    assert live_held.wait(1)
    runner.drain()
    runner.read_logs(1, "job_A")
    assert logs_held.wait(1)
    old_id = runner.read_image(1, "job_A")
    assert image_held.wait(1)
    middle_id = runner.read_image(1, "job_B")
    assert wait_until(runner.drain)[0].payload["request_id"] == middle_id
    current_id = runner.read_image(1, "job_A")
    events = wait_until(runner.drain)
    assert old_id < middle_id < current_id
    assert cancelled.is_set()
    assert events[0].payload == {
        "job_id": "job_A",
        "request_id": current_id,
        "response": {"ok": True, "body": "current job_A"},
    }
    assert not runner._log_task.done() and not runner._live_task.done()
    assert "STALE" not in repr(events)


@pytest.mark.parametrize("action", ["pause", "reconnect", "close"])
def test_image_read_is_cancelled_and_late_result_fenced_by_view_lifecycle(runner_factory, action):
    make, _ = runner_factory
    live_held = threading.Event()
    image_held = threading.Event()
    cancelled = threading.Event()
    requests = []

    async def handler(request):
        requests.append(request)
        if request.url.path == "/operator/image":
            image_held.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                cancelled.set()
                return httpx2.Response(200, json={"ok": True, "body": "late preview"})
        if json.loads(request.content)["cursor"] is None:
            return httpx2.Response(200, json=envelope())
        live_held.set()
        await asyncio.Future()

    runner = make(handler)
    runner.start(1)
    assert live_held.wait(1)
    runner.drain()
    runner.read_image(1, "job_A")
    assert image_held.wait(1)
    if action == "close":
        runner.close()
    elif action == "pause":
        runner.pause(1)
    else:
        runner.reconnect(2)
    assert cancelled.wait(1)
    assert all(event.kind != "image" for event in runner.drain())
    runner.read_image(1, "job_B")
    assert len([request for request in requests if request.url.path == "/operator/image"]) == 1


@pytest.mark.parametrize(
    "status,body,expected",
    [
        (302, b"private body", REJECTED),
        (401, b"private body", AUTH_FAILED),
        (403, b"private body", AUTH_FAILED),
        (200, b"invalid JSON with private body", INVALID_RESPONSE),
        (200, b"x" * (MAX_RESPONSE_BYTES + 1), INVALID_RESPONSE),
    ],
    ids=["redirect", "unauthorized", "forbidden", "invalid-json", "oversized-body"],
)
def test_image_failures_are_safe_bounded_and_never_retry_or_disconnect_live(
    runner_factory, status, body, expected
):
    make, _ = runner_factory
    held = threading.Event()
    images = []

    async def handler(request):
        if request.url.path == "/operator/image":
            images.append(request)
            return httpx2.Response(
                status, content=body, headers={"location": "http://secret.example/" + TOKEN}
            )
        if json.loads(request.content)["cursor"] is None:
            return httpx2.Response(200, json=envelope())
        held.set()
        await asyncio.Future()

    runner = make(handler)
    runner.start(1)
    assert held.wait(1)
    runner.drain()
    request_id = runner.read_image(1, "job_A")
    events = wait_until(runner.drain)
    assert len(events) == len(images) == 1
    assert events[0].kind == "image"
    assert events[0].payload == {"job_id": "job_A", "request_id": request_id, "error": expected}
    assert "private body" not in repr(events) and TOKEN not in repr(events)
    assert not runner._live_task.done()


def test_delivery_overflow_cancels_image_read_and_drops_late_preview(runner_factory):
    make, _ = runner_factory
    held = threading.Event()
    image_held = threading.Event()
    image_cancelled = threading.Event()
    flood = []

    async def handler(request):
        if request.url.path == "/operator/image":
            image_held.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                image_cancelled.set()
                return httpx2.Response(200, json={"ok": True, "body": "late preview"})
        cursor = json.loads(request.content)["cursor"]
        if cursor is None:
            return httpx2.Response(200, json=envelope())
        if not flood:
            gate = asyncio.get_running_loop().create_future()
            flood.append(gate)
            held.set()
            await gate
        return httpx2.Response(
            200, json=envelope("delta", cursor["sequence"] + 1, base_cursor=cursor)
        )

    runner = make(handler)
    runner.start(1)
    assert held.wait(1)
    runner.drain()
    runner.read_image(1, "job_A")
    assert image_held.wait(1)
    runner._loop.call_soon_threadsafe(flood[0].set_result, None)
    assert image_cancelled.wait(1)
    events = runner.drain()
    assert len(events) == 1 and events[0].kind == "disconnected"
    assert events[0].payload == {"error": DELIVERY_GAP}
    assert runner._gap_generation == 1


def test_real_three_connection_budget_allows_held_live_log_and_image_without_cancellation():
    held = {path: threading.Event() for path in ("live", "job-logs", "image")}
    releases = {path: threading.Event() for path in held}
    requests = []

    class Server(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            path = self.path.removeprefix("/operator/")
            requests.append((path, self.headers.get("Authorization"), payload))
            if path == "live" and payload["cursor"] is None:
                response = envelope()
            else:
                held[path].set()
                if not releases[path].wait(timeout=3):
                    return
                response = (
                    envelope("heartbeat") if path == "live" else {"ok": True, "retained": path}
                )
            body = json.dumps(response).encode()
            try:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except OSError:
                pass  # The runner cancels its owned held live connection on close.

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Server)
    worker = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
    worker.start()
    runner = None
    try:
        runner = LiveRunner(OperatorClient(f"http://127.0.0.1:{server.server_port}", TOKEN))
        runner.start(1)
        assert held["live"].wait(1)
        runner.drain()
        log_id = runner.read_logs(1, "job_A")
        assert held["job-logs"].wait(1)
        image_id = runner.read_image(1, "job_B")
        assert held["image"].wait(1)
        assert not runner._live_task.done() and not runner._log_task.done()
        releases["job-logs"].set()
        releases["image"].set()
        events = []

        def completed():
            events.extend(runner.drain())
            return len(events) == 2

        wait_until(completed)
        assert {event.kind for event in events} == {"logs", "image"}
        assert {event.kind: event.payload["request_id"] for event in events} == {
            "logs": log_id,
            "image": image_id,
        }
        assert all(auth == f"Bearer {TOKEN}" for _, auth, _ in requests)
        assert [path for path, _, _ in requests].count("image") == 1
        assert not runner._live_task.done()
    finally:
        try:
            if runner is not None:
                runner.close()
        finally:
            for release in releases.values():
                release.set()
            server.shutdown()
            server.server_close()
            worker.join(timeout=1)
            assert not worker.is_alive()


@pytest.mark.parametrize(
    "deadline", [0.0, -1.0, float("nan"), float("inf"), float("-inf"), True, TOKEN, 10**500]
)
def test_invalid_or_expired_image_deadline_returns_fixed_error_without_http(
    runner_factory, deadline
):
    make, _ = runner_factory
    held = threading.Event()
    requests = []

    async def handler(request):
        requests.append(request)
        if json.loads(request.content).get("cursor", "not live") is None:
            return httpx2.Response(200, json=envelope())
        held.set()
        await asyncio.Future()

    runner = make(handler)
    runner.start(1)
    assert held.wait(1)
    runner.drain()
    request_id = runner.read_image(1, "capture_A", expires_at=deadline)
    events = wait_until(runner.drain)
    assert len(events) == 1 and events[0].kind == "image"
    assert events[0].payload == {
        "job_id": "capture_A",
        "request_id": request_id,
        "error": IMAGE_EXPIRED,
    }
    assert all(request.url.path == "/operator/live" for request in requests)
    assert TOKEN not in repr(events)
    assert runner._image_task is None


def test_image_deadline_is_rechecked_after_worker_queue_delay(runner_factory, monkeypatch):
    make, _ = runner_factory
    held = threading.Event()
    blocked = threading.Event()
    release = threading.Event()
    requests = []
    clock = [1000.0]
    monkeypatch.setattr(gui_live_transport, "time", SimpleNamespace(monotonic=lambda: clock[0]))

    async def handler(request):
        requests.append(request)
        if json.loads(request.content).get("cursor", "not live") is None:
            return httpx2.Response(200, json=envelope())
        held.set()
        await asyncio.Future()

    def busy_worker():
        blocked.set()
        release.wait(timeout=1)

    runner = make(handler)
    runner.start(1)
    assert held.wait(1)
    runner.drain()
    runner._loop.call_soon_threadsafe(busy_worker)
    assert blocked.wait(1)
    try:
        request_id = runner.read_image(1, "capture_A", expires_at=1001.0)
        clock[0] = 1002.0
    finally:
        release.set()
    events = wait_until(runner.drain)
    assert events[0].payload == {
        "job_id": "capture_A",
        "request_id": request_id,
        "error": IMAGE_EXPIRED,
    }
    assert all(request.url.path == "/operator/live" for request in requests)


def test_image_deadline_guard_is_immediately_before_http_send(runner_factory, monkeypatch):
    make, _ = runner_factory
    held = threading.Event()
    requests = []
    clock = [1000.0]
    monkeypatch.setattr(gui_live_transport, "time", SimpleNamespace(monotonic=lambda: clock[0]))

    async def handler(request):
        requests.append(request)
        if json.loads(request.content).get("cursor", "not live") is None:
            return httpx2.Response(200, json=envelope())
        held.set()
        await asyncio.Future()

    runner = make(handler)
    runner.start(1)
    assert held.wait(1)
    runner.drain()
    build = runner._client.build_request

    def build_request(*args, **kwargs):
        request = build(*args, **kwargs)
        if request.url.path == "/operator/image":
            clock[0] = 1002.0
        return request

    monkeypatch.setattr(runner._client, "build_request", build_request)
    runner.read_image(1, "capture_A", expires_at=1001.0)
    events = wait_until(runner.drain)
    assert events[0].payload["error"] == IMAGE_EXPIRED
    assert all(request.url.path == "/operator/live" for request in requests)


def test_old_generation_image_request_cannot_invalidate_current_pending_preview(runner_factory):
    make, _ = runner_factory
    live_held = threading.Event()
    image_held = threading.Event()
    release = []

    async def handler(request):
        if request.url.path == "/operator/image":
            gate = asyncio.get_running_loop().create_future()
            release.append(gate)
            image_held.set()
            await gate
            return httpx2.Response(200, json={"ok": True, "body": "current preview"})
        if json.loads(request.content)["cursor"] is None:
            return httpx2.Response(200, json=envelope())
        live_held.set()
        await asyncio.Future()

    runner = make(handler)
    runner.start(2)
    assert live_held.wait(1)
    runner.drain()
    current_id = runner.read_image(2, "capture_A")
    assert image_held.wait(1)
    assert runner.read_image(1, "capture_B") == 0
    runner._loop.call_soon_threadsafe(release[0].set_result, None)
    events = wait_until(runner.drain)
    assert len(events) == 1
    assert events[0].generation == 2 and events[0].payload["request_id"] == current_id
    assert events[0].payload["response"]["body"] == "current preview"


@pytest.mark.parametrize(
    "status,retryable",
    [
        (302, False), (400, False), (401, False), (403, False), (404, False),
        (408, True), (429, True), (500, True), (502, True), (503, True), (504, True),
    ],
)
def test_live_failure_diagnostics_classify_status_without_response_text(
    runner_factory, status, retryable
):
    make, _ = runner_factory

    async def handler(request):
        return httpx2.Response(status, text=f"PRIVATE {TOKEN} http://private.example")

    runner = make(handler)
    runner.start(1)
    event = wait_until(runner.drain)[0]
    details = event.diagnostic
    assert details["http_status"] == status
    assert details["retryable"] is retryable
    assert details["generation"] == 1 and details["elapsed_ms"] >= 0
    assert details["code"] is None and details["utc"].endswith("+00:00")
    assert TOKEN not in repr(event) and "PRIVATE" not in repr(event)


@pytest.mark.parametrize(
    "code,retryable",
    [
        ("unavailable", True), ("operator_timeline_unavailable", True),
        ("operator_request_failed", False), ("PRIVATE", False),
    ],
)
def test_live_ok_false_classification_is_allowlisted(runner_factory, code, retryable):
    make, _ = runner_factory

    async def handler(request):
        return httpx2.Response(200, json={"ok": False, "error": code, "secret": TOKEN})

    runner = make(handler)
    runner.start(1)
    event = wait_until(runner.drain)[0]
    assert event.diagnostic["http_status"] == 200
    assert event.diagnostic["retryable"] is retryable
    assert event.diagnostic["code"] == (None if code == "PRIVATE" else code)
    assert "PRIVATE" not in repr(event) and TOKEN not in repr(event)


def test_live_timeout_diagnostic_drops_exception_text(runner_factory):
    make, _ = runner_factory

    async def handler(request):
        raise httpx2.ReadTimeout(f"PRIVATE {TOKEN}", request=request)

    runner = make(handler)
    runner.start(1)
    event = wait_until(runner.drain)[0]
    assert event.diagnostic["reason"] == "read_timeout"
    assert event.diagnostic["retryable"] is True
    assert "PRIVATE" not in repr(event) and TOKEN not in repr(event)
