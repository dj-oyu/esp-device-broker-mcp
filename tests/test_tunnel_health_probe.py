"""The owned-client health probe against real loopback sockets; no Tunnel client.

The probe is synchronous on purpose: GUI status polls run every two seconds, and
creating a fresh event loop per poll is exactly what must not happen (#126).
"""

import asyncio
import json
import socket
import threading
import time
from collections.abc import Callable, Iterator

import pytest

from cardputer_mcp import tunnel_manager as tm
from cardputer_mcp.tunnel import TunnelError

PRIVATE = "SYNTHETIC_HEALTH_PRIVATE_BODY"
CONTROL = {"schema_version": 1, "component": "control-plane", "status": "ok"}
# Generous ceiling over the configured budget: proves boundedness, not speed.
SLACK_SECONDS = 1.5

Handler = Callable[[socket.socket, str, dict[str, str]], None]


class LoopbackServer:
    """One thread per connection; each request is handled then the socket closes."""

    def __init__(self, handler: Handler) -> None:
        self.handler = handler
        self.requests: list[tuple[str, dict[str, str]]] = []
        self.stopping = threading.Event()
        self.listener = socket.create_server(("127.0.0.1", 0))
        self.listener.settimeout(0.05)
        self.port = self.listener.getsockname()[1]
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self) -> None:
        while not self.stopping.is_set():
            try:
                conn, _ = self.listener.accept()
            except (TimeoutError, OSError):
                continue
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn: socket.socket) -> None:
        with conn:
            conn.settimeout(2)
            data = b""
            try:
                while b"\r\n\r\n" not in data:
                    chunk = conn.recv(4096)
                    if not chunk:
                        return
                    data += chunk
                head = data.split(b"\r\n\r\n", 1)[0].decode("latin-1").split("\r\n")
                path = head[0].split(" ")[1]
                headers = {
                    name.strip().lower(): value.strip()
                    for name, _, value in (line.partition(":") for line in head[1:])
                }
                self.requests.append((path, headers))
                self.handler(conn, path, headers)
            except OSError:
                pass

    def close(self) -> None:
        self.stopping.set()
        self.listener.close()
        self.thread.join(2)


@pytest.fixture
def serve() -> Iterator[Callable[[Handler], LoopbackServer]]:
    servers: list[LoopbackServer] = []

    def start(handler: Handler) -> LoopbackServer:
        servers.append(LoopbackServer(handler))
        return servers[-1]

    yield start
    for server in servers:
        server.close()


def respond(conn: socket.socket, status: int, body: bytes, *extra: str) -> None:
    head = [f"HTTP/1.1 {status} X", f"Content-Length: {len(body)}", "Connection: close", *extra]
    conn.sendall(("\r\n".join(head) + "\r\n\r\n").encode() + body)


def healthy(conn: socket.socket, path: str, headers: dict[str, str]) -> None:
    if path == "/readyz":
        respond(conn, 200, b"ready")
    else:
        respond(conn, 200, json.dumps(CONTROL).encode())


def timed(port: int) -> tuple[dict, float]:
    started = time.monotonic()
    result = tm.health_probe(port)
    return result, time.monotonic() - started


def test_reads_both_components_without_a_proxy_or_any_event_loop(serve, monkeypatch):
    server = serve(healthy)
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "all_proxy"):
        monkeypatch.setenv(name, "http://127.0.0.1:9")

    def no_loop(*args, **kwargs):
        raise AssertionError("health probe created an event loop")

    monkeypatch.setattr(asyncio, "run", no_loop)
    monkeypatch.setattr(asyncio, "new_event_loop", no_loop)
    monkeypatch.setattr(asyncio, "Runner", no_loop)
    for _ in range(3):  # Repeated polls never need a loop either.
        result = tm.health_probe(server.port)
        assert result == {"ready_status": 200, "control_status": 200, "control_plane": CONTROL}
    paths = [path for path, _ in server.requests]
    assert paths == ["/readyz", "/health/control-plane"] * 3
    for _, headers in server.requests:
        assert headers["accept-encoding"] == "identity"
        assert headers["host"] == f"127.0.0.1:{server.port}"
        assert "authorization" not in headers and "proxy-authorization" not in headers


def test_redirects_are_reported_not_followed(serve):
    target = serve(healthy)

    def redirect(conn, path, headers):
        respond(conn, 302, b"", f"Location: http://127.0.0.1:{target.port}{path}")

    server = serve(redirect)
    result = tm.health_probe(server.port)
    assert result == {"ready_status": 302, "control_status": 302, "control_plane": None}
    assert target.requests == []


def test_oversized_and_compressed_bodies_are_rejected_without_echo(serve):
    def oversized(conn, path, headers):
        if path == "/readyz":
            respond(conn, 200, PRIVATE.encode() * 40)  # > 512 B
        else:
            respond(conn, 200, PRIVATE.encode() * tm.HEALTH_MAX_BYTES)

    result = tm.health_probe(serve(oversized).port)
    assert result == {"ready_status": 0, "control_status": 0, "control_plane": None}
    assert PRIVATE not in repr(result)

    def compressed(conn, path, headers):
        body = json.dumps({**CONTROL, "note": PRIVATE}).encode()
        respond(conn, 200, body, "Content-Encoding: gzip")

    result = tm.health_probe(serve(compressed).port)
    assert result == {"ready_status": 0, "control_status": 0, "control_plane": None}
    assert PRIVATE not in repr(result)


def test_invalid_json_and_connection_refused_are_quietly_unknown(serve):
    def broken(conn, path, headers):
        respond(conn, 200, b"ready" if path == "/readyz" else b"{" + PRIVATE.encode())

    result = tm.health_probe(serve(broken).port)
    assert result == {"ready_status": 200, "control_status": 0, "control_plane": None}
    with socket.create_server(("127.0.0.1", 0)) as probe:
        closed_port = probe.getsockname()[1]
    result, elapsed = timed(closed_port)
    assert result == {"ready_status": 0, "control_status": 0, "control_plane": None}
    assert elapsed < tm.HEALTH_TOTAL_SECONDS + SLACK_SECONDS


def drip(prefix: bytes) -> Handler:
    def handler(conn, path, headers):
        conn.sendall(prefix)
        for _ in range(400):  # ~20 s of 1-byte writes if never cut off.
            time.sleep(0.05)
            conn.sendall(b"x")

    return handler


@pytest.mark.parametrize(
    "prefix",
    [
        # Body bytes keep arriving inside the per-read timeout.
        b"HTTP/1.1 200 OK\r\nContent-Length: 100000\r\n\r\n",
        # Header bytes keep arriving inside the per-read timeout.
        b"HTTP/1.1 200 OK\r\nX-Slow: ",
    ],
    ids=["body", "headers"],
)
def test_a_dripping_peer_is_cut_off_at_the_overall_deadline(serve, monkeypatch, prefix):
    monkeypatch.setattr(tm, "HEALTH_TOTAL_SECONDS", 0.4)
    server = serve(drip(prefix))
    result, elapsed = timed(server.port)
    assert result == {"ready_status": 0, "control_status": 0, "control_plane": None}
    assert elapsed < 0.4 + SLACK_SECONDS
    # The budget is shared: the second component is never even requested.
    assert [path for path, _ in server.requests] == ["/readyz"]


def test_a_silent_peer_is_bounded_by_the_per_read_and_total_budget(serve, monkeypatch):
    monkeypatch.setattr(tm, "HEALTH_TOTAL_SECONDS", 0.3)
    stall = threading.Event()
    server = serve(lambda conn, path, headers: stall.wait(5))
    try:
        result, elapsed = timed(server.port)
    finally:
        stall.set()
    assert result == {"ready_status": 0, "control_status": 0, "control_plane": None}
    assert elapsed < 0.3 + SLACK_SECONDS


async def test_calling_on_a_running_loop_is_refused():
    with pytest.raises(TunnelError, match="^tunnel_worker_required$"):
        tm.health_probe(1)
