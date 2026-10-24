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
    INVALID_RESPONSE,
    MAX_RESPONSE_BYTES,
    REJECTED,
    STARTUP_FAILED,
    LiveRunner,
)
from cardputer_mcp.operator_client import OperatorClient, OperatorClientError

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
    if status in (401, 403):
        assert events[0].payload == {
            "job_id": "job_A",
            "request_id": request_id,
            "response": {
                "ok": False,
                "error": "authentication_required" if status == 401 else "local_operator_required",
            },
        }
    else:
        assert events[0].payload == {"job_id": "job_A", "request_id": request_id, "error": expected}
    assert "private body" not in repr(events) and TOKEN not in repr(events)
    assert not runner._live_task.done()


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
    "status,retryable",
    [
        (302, False),
        (401, False),
        (408, True),
        (429, True),
        (503, True),
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
