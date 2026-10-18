"""Read-only worker transport boundaries, delivery gaps, and cancellation."""

import asyncio
import json
import threading
import time
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import httpx2
import pytest

from cardputer_mcp import gui_live_transport
from cardputer_mcp.gui_live_transport import (
    AUTH_FAILED,
    DELIVERY_GAP,
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
    runner = LiveRunner(OperatorClient(f"http://127.0.0.1:{server.server_port}", TOKEN))
    try:
        runner.start(1)
        events = wait_until(runner.drain)
        assert events[0].payload == {"error": REJECTED}
        assert requests == [("/operator/live", f"Bearer {TOKEN}", {"cursor": None})]
        assert sink_requests == []
    finally:
        runner.close()
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
