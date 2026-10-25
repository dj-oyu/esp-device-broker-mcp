"""Read-only worker transport boundaries, event streams, delivery gaps, and cancellation."""

import asyncio
import json
import threading
import time
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import httpx2
import pytest

from cardputer_mcp import gui_live_transport, operator_live
from cardputer_mcp.gui_live_retry import LiveRetry
from cardputer_mcp.gui_live_transport import (
    AUTH_FAILED,
    INVALID_RESPONSE,
    MAX_RESPONSE_BYTES,
    REJECTED,
    STARTUP_FAILED,
    UNREACHABLE,
    EventStreamParser,
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


def sse(*payloads):
    return b"".join(b"data: " + json.dumps(payload).encode() + b"\n\n" for payload in payloads)


class Events(httpx2.AsyncByteStream):
    """A mock event stream: its chunks, then held open (or ended) until cancelled."""

    def __init__(self, *chunks, held=None, cancelled=None, hold=True):
        self.chunks, self.held, self.cancelled, self.hold = chunks, held, cancelled, hold

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk
        if not self.hold:
            return
        if self.held is not None:
            self.held.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            if self.cancelled is not None:
                self.cancelled.set()
            raise


def stream(*chunks, **options):
    return httpx2.Response(
        200, headers={"content-type": "text/event-stream"}, stream=Events(*chunks, **options)
    )


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
        if request.url.path == "/operator/events":
            requests.append((request.url.path, None))
            return stream(sse(envelope()), held=live_held)
        payload = json.loads(request.content)
        requests.append((request.url.path, payload))
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
        def do_GET(self):
            sink_requests.append(self.headers.get("Authorization"))
            self.send_error(500)

        do_POST = do_GET

        def log_message(self, *args):
            pass

    sink = ThreadingHTTPServer(("127.0.0.1", 0), Sink)

    class Redirect(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append((self.path, self.headers.get("Authorization"), self.command))
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
        assert requests == [("/operator/events", f"Bearer {TOKEN}", "GET")]
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
        if request.url.path == "/operator/events":
            requests.append((request.url.path, None))
            snapshots += 1
            if snapshots == 2:
                resumed.set()
            return stream(
                sse(envelope(sequence=snapshots)), held=live_held, cancelled=live_cancelled
            )
        payload = json.loads(request.content)
        requests.append((request.url.path, payload))
        logs_held.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            logs_cancelled.set()
            return httpx2.Response(200, json={"ok": True, "entries": ["late output"]})

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
    assert len(requests) == 2
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
        if request.url.path == "/operator/events":
            return stream(sse(envelope()), held=held)
        if request.url.path == "/operator/image":
            return httpx2.Response(200, json={"ok": True, "retained": "preview JSON"})
        return httpx2.Response(200, json={"ok": True, "entries": []})

    runner = make(handler)
    runner.start(1)
    assert held.wait(1)
    assert [(request.method, request.url.path) for request in requests] == [
        ("GET", "/operator/events")
    ]
    assert requests[0].headers["accept"] == "text/event-stream"
    assert requests[0].headers["authorization"] == f"Bearer {TOKEN}"
    assert requests[0].content == b"" and TOKEN not in str(requests[0].url)
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
        return stream(sse(envelope()), held=live_held)

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
        return stream(sse(envelope()), held=held)

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

        def do_GET(self):
            requests.append(("live", self.headers.get("Authorization"), None))
            try:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(sse(envelope()))
                self.wfile.flush()
            except OSError:
                return
            held["live"].set()
            releases["live"].wait(timeout=3)

        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            path = self.path.removeprefix("/operator/")
            requests.append((path, self.headers.get("Authorization"), payload))
            held[path].set()
            if not releases[path].wait(timeout=3):
                return
            response = {"ok": True, "retained": path}
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
        # The server thread writes the snapshot before it holds; wait for its delivery.
        assert [event.kind for event in wait_until(runner.drain)] == ["message"]
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


def test_event_stream_parser_handles_split_chunks_comments_and_multiline_data():
    payload = (
        b"\xef\xbb\xbf: ping\r\n\r\n"
        b"id: " + EPOCH.encode() + b":1\r\n"
        b'data: {"a":\r\n'
        b"data:1}\r\n\r\n"
        b"event: other\ndata: ignored\n\n"
        b"retry: 10\rdata: caf\xc3\xa9\r\r"
        b"data\n\n"
        b": ping\n\n"
    )
    expected = [None, '{"a":\n1}', "café", "", None]
    whole = EventStreamParser().feed(payload)
    assert whole == expected
    for size in (1, 2, 3, 7):
        parser = EventStreamParser()
        items = []
        for index in range(0, len(payload), size):
            items.extend(parser.feed(payload[index : index + size]))
        assert items == expected
    # A CRLF split across chunks is one line end, not an extra blank line.
    parser = EventStreamParser()
    assert parser.feed(b"data: x\r") == []
    assert parser.feed(b"\ndata: y\r") == []
    assert parser.feed(b"\n\r\n") == ["x\ny"]


@pytest.mark.parametrize(
    "chunks",
    [[b"data: " + b"x" * 64], [b"data: " + b"x" * 30 + b"\n", b"data: " + b"x" * 40 + b"\n"]],
    ids=["line", "event"],
)
def test_event_stream_parser_bounds_lines_and_events(chunks):
    parser = EventStreamParser(limit=64)
    with pytest.raises(ValueError):
        for chunk in chunks:
            parser.feed(chunk)


def test_event_stream_parser_rejects_invalid_utf8():
    with pytest.raises(ValueError):
        EventStreamParser().feed(b"data: \xff\n\n")


def messages(runner, count):
    events = []

    def collected():
        events.extend(runner.drain())
        return len(events) >= count

    wait_until(collected)
    return events


def test_stream_delivers_snapshot_delta_and_comment_heartbeat_in_order(runner_factory):
    make, options = runner_factory
    first = envelope()
    delta = envelope(
        "delta",
        2,
        base_cursor=first["cursor"],
        upsert=[],
        remove=[],
        order=[],
        has_more=False,
        logs_changed=True,
    )
    body = sse(first) + b": ping\n\n" + sse(delta)
    # Arbitrary chunk boundaries inside events, comments and CRLF pairs.
    chunks = [body[index : index + 5] for index in range(0, len(body), 5)]

    async def handler(request):
        return stream(*chunks)

    runner = make(handler)
    runner.start(1)
    events = messages(runner, 3)
    assert [event.kind for event in events] == ["message"] * 3
    assert events[0].payload == first
    assert events[1].payload == envelope("heartbeat", 1)
    assert events[2].payload == delta
    assert all(event.generation == 1 for event in events)
    # The read timeout outlasts the broker heartbeat, so only a dead stream times out.
    timeout = options[0]["timeout"]
    assert timeout.read > operator_live.HEARTBEAT_SECONDS
    assert timeout.connect == 2.0


@pytest.mark.parametrize(
    "body,error,reason,code,retryable",
    [
        (
            sse({"ok": False, "error": "unavailable"}),
            REJECTED,
            "broker_rejected",
            "unavailable",
            True,
        ),
        (
            sse({"ok": False, "error": "authentication_required"}),
            AUTH_FAILED,
            "authentication_failed",
            "authentication_required",
            False,
        ),
        (
            sse({"ok": False, "error": "local_operator_required"}),
            AUTH_FAILED,
            "authentication_failed",
            "local_operator_required",
            False,
        ),
        (sse({"ok": False, "error": TOKEN}), REJECTED, "broker_rejected", None, False),
        (
            b"data: not json " + TOKEN.encode() + b"\n\n",
            INVALID_RESPONSE,
            "invalid_response",
            None,
            False,
        ),
        (sse(envelope("delta", 2)), INVALID_RESPONSE, "invalid_response", None, False),
        (b"", UNREACHABLE, "peer_disconnected", None, True),
    ],
    ids=["unavailable", "unauthenticated", "forbidden", "unknown", "json", "first", "eof"],
)
def test_stream_refusals_and_drops_are_fixed_diagnostics(
    runner_factory, body, error, reason, code, retryable
):
    make, _ = runner_factory

    async def handler(request):
        return stream(body, hold=False)

    runner = make(handler)
    runner.start(1)
    event = wait_until(runner.drain)[-1]
    assert event.kind == "disconnected" and event.payload == {"error": error}
    assert event.diagnostic["reason"] == reason
    assert event.diagnostic["code"] == code and event.diagnostic["retryable"] is retryable
    assert TOKEN not in repr(event)


def test_mid_stream_refusal_follows_delivered_events(runner_factory):
    make, _ = runner_factory

    async def handler(request):
        return stream(sse(envelope(), {"ok": False, "error": "unavailable"}), hold=False)

    runner = make(handler)
    runner.start(1)
    events = messages(runner, 2)
    assert [event.kind for event in events] == ["message", "disconnected"]
    assert events[1].diagnostic["code"] == "unavailable" and events[1].diagnostic["retryable"]


@pytest.mark.parametrize(
    "content_type,body,expected,code,retryable",
    [
        ("application/json", {"ok": False, "error": "unavailable"}, REJECTED, "unavailable", True),
        ("application/json", {"ok": True}, INVALID_RESPONSE, None, False),
        ("text/plain", {"ok": True}, INVALID_RESPONSE, None, False),
    ],
)
def test_json_refusal_before_stream_start_keeps_error_boundary(
    runner_factory, content_type, body, expected, code, retryable
):
    make, _ = runner_factory

    async def handler(request):
        return httpx2.Response(
            200, headers={"content-type": content_type}, content=json.dumps(body).encode()
        )

    runner = make(handler)
    runner.start(1)
    event = wait_until(runner.drain)[0]
    assert event.payload == {"error": expected}
    assert event.diagnostic["code"] == code and event.diagnostic["retryable"] is retryable


def test_dropped_stream_is_retryable_and_reconnect_receives_a_fresh_snapshot(runner_factory):
    make, _ = runner_factory
    requests = []

    async def handler(request):
        requests.append((request.method, request.url.path, request.headers.get("last-event-id")))
        if len(requests) == 1:
            return stream(sse(envelope()), hold=False)
        return stream(sse(envelope(sequence=7)))

    runner = make(handler)
    runner.start(1)
    events = messages(runner, 2)
    assert events[0].payload["kind"] == "snapshot"
    dropped = events[1]
    assert dropped.kind == "disconnected" and dropped.payload == {"error": UNREACHABLE}
    retry = LiveRetry()
    retry.failed(dropped.diagnostic, 100.0, jitter=1.0)
    assert not retry.stopped and retry.deadline == 101.0
    runner.reconnect(2)
    event = wait_until(runner.drain)[0]
    assert event.generation == 2 and event.payload == envelope(sequence=7)
    # Every (re)connect is a full snapshot; no Last-Event-ID is sent.
    assert requests == [("GET", "/operator/events", None)] * 2
