"""Deterministic synthetic Tunnel lifecycles; no client, account, or USB use."""

import asyncio
import json
import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from filelock import Timeout

from cardputer_mcp import tunnel_manager as tm
from cardputer_mcp.client import SURFACE_TOOLS
from cardputer_mcp.tunnel import TunnelError, TunnelSettings
from cardputer_mcp.windows_process import WindowsProcessError

KEY = "SYNTHETIC_RUNTIME_KEY_NEVER_REAL"
BEARER = "Bearer " + "a" * 64
PRIVATE = "SYNTHETIC_SECRET_RAW_NATIVE_OUTPUT"
URL = "http://127.0.0.1:18765/mcp"
NOW = 1790947500.0


def stamp(value=NOW):
    return datetime.fromtimestamp(value, UTC).isoformat().replace("+00:00", "Z")


def healthy(**updates):
    result = {
        "schema_version": 1,
        "snapshot_at": stamp(),
        "component": "control-plane",
        "status": "ok",
        "state": "polling",
        "limited": False,
        "details": {"last_success": stamp(), "consecutive_failures": 0},
    }
    result.update(updates)
    return {"ready_status": 200, "control_status": 200, "control_plane": result}


class FakeLock:
    def __init__(self):
        self.held = False
        self.blocked = False
        self.releases = 0

    def acquire(self, *, timeout):
        assert timeout == 0
        if self.blocked:
            raise Timeout(PRIVATE)
        self.held = True

    def release(self):
        self.held = False
        self.releases += 1


class FakeProcess:
    def __init__(self):
        self.exit_code = None
        self.children = 1
        self.confirm_stop = True
        self.stop_calls = 0
        self.close_calls = 0
        self.fail_close = False

    def poll(self):
        return self.exit_code

    def active_count(self):
        return self.children

    def stop(self, timeout):
        assert timeout == tm.STOP_TIMEOUT_SECONDS
        self.stop_calls += 1
        if self.confirm_stop:
            self.children = 0
            if self.exit_code is None:
                self.exit_code = 1
            return True
        return False

    def close(self):
        assert self.children == 0
        if self.fail_close:
            raise RuntimeError(PRIVATE)
        self.close_calls += 1


@pytest.fixture
def configured(tmp_path):
    config = tmp_path / "broker.toml"
    config.write_text('backend = "mock"\nport = 18765\n')
    settings = TunnelSettings(
        client_exe=tmp_path / "tunnel-client.exe",
        client_archive=tmp_path / "client.zip",
        broker_config=config,
        tunnel_id="tunnel_" + "1" * 32,
        health_port=18766,
    )
    process, lock = FakeProcess(), FakeLock()
    health = healthy()
    identity = {
        "ok": True,
        "authenticated": True,
        "origin": "remote_mcp",
        "scopes": ["read"],
        "tunnel_ingress_enabled": False,
        "backend": "mock",
        "status": {
            "ok": True,
            "active_job": "active-job-is-allowed",
            "owner": "active-owner-is-allowed",
            "hardware_connected": True,
        },
    }
    capture = {"starts": 0, "broker_calls": 0, "verifies": 0, "events": []}

    def factory(args, env, cwd):
        capture["starts"] += 1
        capture["args"] = args
        capture["env_reference"] = env
        capture["env_at_start"] = dict(env)
        capture["cwd"] = cwd
        return process

    def probe(url, bearer):
        assert url == URL and bearer == BEARER
        capture["broker_calls"] += 1
        return identity

    def verify(settings):
        capture["verifies"] += 1

    manager = tm.TunnelManager(
        process_factory=factory,
        broker_probe=probe,
        health_probe=lambda port: health,
        verify_client=verify,
        listener_check=lambda port: None,
        lock_factory=lambda: lock,
        clock=lambda: NOW,
        monotonic=lambda: NOW,
        on_event=capture["events"].append,
    )
    assert manager.configure(settings, URL)["status_code"] == "tunnel_configured"
    return SimpleNamespace(
        manager=manager,
        settings=settings,
        process=process,
        lock=lock,
        health=health,
        identity=identity,
        capture=capture,
    )


def test_connect_read_only_active_broker_and_separate_evidence(configured, monkeypatch):
    c = configured
    monkeypatch.setenv("CARDPUTER_LOCAL_TOKEN", PRIVATE)
    monkeypatch.setenv("HTTP_PROXY", "https://" + PRIVATE)
    monkeypatch.setenv("OPENAI_ADMIN_KEY", PRIVATE)
    state = c.manager.connect(KEY, BEARER)
    assert state["state"] == "running"
    assert state["owned"] and state["process_alive"] and not state["busy"]
    assert not state["local_readiness"] and not state["cloud_poll_observed"]
    assert c.lock.held and c.capture["verifies"] == 1
    assert c.capture["env_reference"] == {}
    assert c.capture["env_at_start"]["CARDPUTER_TUNNEL_API_KEY"] == KEY
    assert c.capture["env_at_start"]["CARDPUTER_TUNNEL_BEARER"] == BEARER
    assert PRIVATE not in repr(c.capture["env_at_start"])
    assert BEARER not in repr(c.capture["args"]) and KEY not in repr(c.capture["args"])
    state = c.manager.poll()
    assert state["local_readiness"] and state["cloud_poll_observed"]
    assert state["app_verified"] is False
    assert c.capture["broker_calls"] == 1
    for secret in (PRIVATE, KEY, BEARER, c.settings.tunnel_id, str(c.settings.client_exe)):
        assert secret not in repr(state) + repr(c.manager) + repr(c.capture["events"])
    assert c.manager.close() is True
    assert c.process.stop_calls == 1 and c.process.close_calls == 1
    assert not c.lock.held
    assert c.capture["broker_calls"] == 1


def test_connect_idempotent_no_spawn_or_broker_reprobe(configured):
    c = configured
    c.manager.connect(KEY, BEARER)
    c.manager.connect(KEY, BEARER)
    assert c.capture["starts"] == c.capture["broker_calls"] == 1
    assert c.manager.configure(c.settings, URL)["status_code"] == "tunnel_configuration_locked"
    assert c.manager.disconnect()["state"] == "disconnected"
    assert c.manager.disconnect()["state"] == "disconnected"
    assert c.process.stop_calls == 1


@pytest.mark.parametrize(
    "change,code",
    [
        ({"authenticated": False}, "tunnel_broker_auth_failed"),
        ({"origin": "local_cli"}, "tunnel_broker_origin_rejected"),
        ({"scopes": ["control"]}, "tunnel_broker_scope_rejected"),
        ({"backend": "serial"}, "tunnel_broker_backend_mismatch"),
        ({"status": {"ok": False, "error": PRIVATE}}, "tunnel_broker_status_invalid"),
    ],
)
def test_broker_validation_fail_closed(configured, change, code):
    c = configured
    c.identity.update(change)
    state = c.manager.connect(KEY, BEARER)
    assert state["status_code"] == code and state["state"] == "disconnected"
    assert c.capture["starts"] == 0 and not c.lock.held
    assert PRIVATE not in repr(state)


@pytest.mark.parametrize(
    "url", ["http://localhost:18765/mcp", URL + "/", "http://127.0.0.1:18767/mcp"]
)
def test_exact_current_broker_url_required(configured, url):
    c = configured
    state = c.manager.configure(c.settings, url)
    assert state["status_code"] == "tunnel_broker_url_mismatch"
    assert not state["configured"]
    assert c.manager.connect(KEY, BEARER)["status_code"] == "tunnel_not_configured"


def test_configuration_rechecked_at_connect(configured):
    c = configured
    c.settings.broker_config.write_text("port = 18764\n")
    assert c.manager.connect(KEY, BEARER)["status_code"] == "tunnel_configuration_changed"
    assert c.capture["starts"] == 0


def test_placeholder_rejected(configured):
    c = configured
    settings = c.settings.model_copy(update={"tunnel_id": "tunnel_" + "0" * 32})
    assert c.manager.configure(settings, URL)["status_code"] == "tunnel_id_placeholder"


def test_failed_release_verification_does_not_lock_probe_or_spawn(configured):
    c = configured

    def reject(settings):
        raise TunnelError("client_release_verification_failed")

    c.manager._verify = reject
    state = c.manager.connect(KEY, BEARER)
    assert state["status_code"] == "client_release_verification_failed"
    assert not c.lock.held and c.capture["broker_calls"] == c.capture["starts"] == 0


@pytest.mark.parametrize("failure", ["lock", "listener"])
def test_occupied_lock_or_listener_never_attached(configured, failure):
    c = configured
    if failure == "lock":
        c.lock.blocked = True
        code = "tunnel_launcher_already_running"
    else:

        def occupied(port):
            raise TunnelError("tunnel_health_listener_exists")

        c.manager._listener_check = occupied
        code = "tunnel_health_listener_exists"
    assert c.manager.connect(KEY, BEARER)["status_code"] == code
    assert c.capture["starts"] == 0 and c.capture["broker_calls"] == 0
    assert c.process.stop_calls == 0


def test_unexpected_exit_stops_descendants_before_unlock(configured):
    c = configured
    c.manager.connect(KEY, BEARER)
    c.process.exit_code = 17
    c.process.children = 2
    state = c.manager.poll()
    assert state["status_code"] == "tunnel_client_exited" and state["exit_code"] == 17
    assert not state["owned"] and not state["process_alive"]
    assert c.process.stop_calls == 1 and c.process.children == 0 and not c.lock.held
    for _ in range(5):
        c.manager.poll()
    assert c.capture["starts"] == 1


@pytest.mark.parametrize("failure", ["stop", "close"])
def test_unconfirmed_shutdown_retains_ownership_and_window(configured, failure):
    c = configured
    c.manager.connect(KEY, BEARER)
    c.process.confirm_stop = failure != "stop"
    c.process.fail_close = failure == "close"
    assert c.manager.close() is False
    state = c.manager.snapshot()
    assert state["state"] == "stop_uncertain" and state["ownership_uncertain"]
    assert state["owned"] and c.lock.held
    c.manager.connect(KEY, BEARER)
    assert c.capture["starts"] == 1
    c.process.confirm_stop = True
    c.process.fail_close = False
    assert c.manager.close() is True
    assert not c.lock.held and c.manager.snapshot()["state"] == "closed"


def test_partial_launch_keeps_exception_owned_job(configured):
    c = configured
    c.process.confirm_stop = False

    def fail(args, env, cwd):
        raise WindowsProcessError("owned_process_start_failed", owned=c.process)

    c.manager._factory = fail
    state = c.manager.connect(KEY, BEARER)
    assert state["status_code"] == "tunnel_stop_unconfirmed"
    assert state["owned"] and state["ownership_uncertain"] and c.lock.held
    c.process.confirm_stop = True
    assert c.manager.close() is True


@pytest.mark.parametrize("status", [401, 403])
def test_native_cloud_auth_failure_stops_only_owned_job_and_no_retry(configured, status):
    c = configured
    c.manager.connect(KEY, BEARER)
    c.health.update(
        healthy(
            status="degraded",
            state="backoff",
            details={
                "last_success": stamp(),
                "consecutive_failures": 1,
                "http_status": status,
                "failure_category": "http_error",
                "error": PRIVATE,
            },
        )
    )
    state = c.manager.poll()
    assert state["status_code"] == "tunnel_cloud_auth_failed"
    assert state["state"] == "disconnected" and not c.lock.held
    c.manager.poll()
    assert c.capture["starts"] == c.capture["broker_calls"] == 1
    assert PRIVATE not in repr(state)


@pytest.mark.parametrize(
    "updates,code",
    [
        ({"schema_version": 2}, "tunnel_health_schema_unsupported"),
        ({"schema_version": True}, "tunnel_health_schema_unsupported"),
        ({"limited": True}, "tunnel_health_schema_unsupported"),
        ({"snapshot_at": stamp(NOW - 20)}, "tunnel_health_stale"),
        ({"snapshot_at": stamp(NOW + 20)}, "tunnel_health_stale"),
        ({"details": {"consecutive_failures": 0}}, "tunnel_cloud_poll_unobserved"),
        (
            {"details": {"consecutive_failures": 0, "last_success": stamp(NOW - 300)}},
            "tunnel_cloud_poll_stale",
        ),
        (
            {"details": {"consecutive_failures": 0, "last_success": stamp(NOW + 20)}},
            "tunnel_cloud_poll_stale",
        ),
        (
            {"details": {"consecutive_failures": 1}, "status": "degraded"},
            "tunnel_cloud_poll_degraded",
        ),
    ],
)
def test_control_plane_requires_fresh_supported_success(updates, code):
    payload = healthy(**updates)["control_plane"]
    assert tm.classify_control_plane(payload, now=NOW, started_at=NOW - 500) == (False, code)


def test_recent_success_from_previous_child_is_not_evidence():
    payload = healthy(details={"consecutive_failures": 0, "last_success": stamp(NOW - 20)})
    assert tm.classify_control_plane(payload["control_plane"], now=NOW, started_at=NOW)[0] is False


def test_only_readyz_does_not_verify_cloud_or_app(configured):
    c = configured
    c.health["control_status"] = 404
    c.manager.connect(KEY, BEARER)
    state = c.manager.poll()
    assert state["local_readiness"] and not state["cloud_poll_observed"]
    assert state["app_verified"] is False and state["status_code"] == "tunnel_local_ready"


def test_invalid_runtime_inputs_not_echoed_or_stored(configured):
    c = configured
    state = c.manager.connect(PRIVATE + "\n", BEARER)
    assert state["status_code"] == "invalid_runtime_key"
    assert PRIVATE not in repr(state) + repr(c.manager)
    assert c.capture["starts"] == c.capture["broker_calls"] == 0


@pytest.mark.asyncio
async def test_sync_default_probe_requires_worker_not_nested_asyncio():
    with pytest.raises(TunnelError, match="^tunnel_worker_required$"):
        tm.broker_probe(URL, BEARER)


def test_default_broker_probe_only_read_only_tools(monkeypatch):
    calls = []

    class Client:
        async def list_tools(self):
            return SimpleNamespace(tools=[SimpleNamespace(name=name) for name in SURFACE_TOOLS])

    @asynccontextmanager
    async def connection(url, token):
        assert url == URL and token == BEARER.removeprefix("Bearer ")
        yield Client()

    async def call(client, tool, request):
        # Only the read-only status tool is ever used by the default probe.
        assert tool == "status"
        calls.append(request["view"])
        if request["view"] == "identity":
            return {"ok": True, "authenticated": True, "origin": "remote_mcp", "scopes": ["read"]}
        assert request == {"view": "device"}
        return {"ok": True, "backend": "mock", "active_job": PRIVATE, "owner": PRIVATE}

    monkeypatch.setattr(tm, "connect_mcp", connection)
    monkeypatch.setattr(tm, "call", call)
    result = tm.broker_probe(URL, BEARER)
    assert result["authenticated"] and result["backend"] == "mock"
    assert calls == ["identity", "device"]


def test_health_http_bounds_proxy_redirects_and_untrusted_body(monkeypatch):
    requested = []

    class Response:
        status_code = 200
        headers = {}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def aiter_raw(self):
            if requested[-1].endswith("readyz"):
                yield b"ready"
            else:
                yield json.dumps(healthy()["control_plane"]).encode()

    class HTTP:
        def __init__(self, **options):
            assert options["trust_env"] is False
            assert options["follow_redirects"] is False
            assert options["timeout"] <= 1
            assert options["headers"] == {"Accept-Encoding": "identity"}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        def stream(self, method, url):
            assert method == "GET"
            requested.append(url)
            return Response()

    monkeypatch.setattr(tm.httpx2, "AsyncClient", HTTP)
    assert tm.health_probe(18766)["control_plane"]["schema_version"] == 1
    assert requested == [
        "http://127.0.0.1:18766/readyz",
        "http://127.0.0.1:18766/health/control-plane",
    ]

    async def overflow(self):
        yield PRIVATE.encode() * tm.HEALTH_MAX_BYTES

    monkeypatch.setattr(Response, "aiter_raw", overflow)
    result = tm.health_probe(18766)
    assert result == {"ready_status": 0, "control_status": 0, "control_plane": None}
    assert PRIVATE not in repr(result)


@pytest.mark.asyncio
async def test_health_total_timeout_covers_slow_drip_headers(monkeypatch):
    monkeypatch.setattr(tm, "HEALTH_TOTAL_SECONDS", 0.12)
    calls = []
    tasks = set()

    async def slow_headers(reader, writer):
        task = asyncio.current_task()
        tasks.add(task)
        calls.append(True)
        try:
            await reader.readuntil(b"\r\n\r\n")
            writer.write(b"HTTP/1.1 200 OK\r\nX-Slow: ")
            await writer.drain()
            while True:
                writer.write(b"x")
                await writer.drain()
                await asyncio.sleep(0.02)
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except ConnectionError:
                pass
            tasks.discard(task)

    server = await asyncio.start_server(slow_headers, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        started = time.monotonic()
        result = await asyncio.wait_for(tm._health_probe_async(port), timeout=1)
        assert time.monotonic() - started < 0.7
        assert calls == [True]
        assert result == {"ready_status": 0, "control_status": 0, "control_plane": None}
    finally:
        server.close()
        await server.wait_closed()
        pending = list(tasks)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)


@pytest.mark.asyncio
async def test_health_total_timeout_covers_dripping_body(monkeypatch):
    monkeypatch.setattr(tm, "HEALTH_TOTAL_SECONDS", 0.06)
    original = tm.httpx2.AsyncClient
    chunks = []

    class Body(tm.httpx2.AsyncByteStream):
        async def __aiter__(self):
            for _ in range(100):
                chunks.append(True)
                await asyncio.sleep(0.01)
                yield b"x"

    def client(**kwargs):
        transport = tm.httpx2.MockTransport(lambda request: tm.httpx2.Response(200, stream=Body()))
        return original(transport=transport, **kwargs)

    monkeypatch.setattr(tm.httpx2, "AsyncClient", client)
    result = await tm._health_probe_async(18766)
    assert len(chunks) < 10
    assert result == {"ready_status": 0, "control_status": 0, "control_plane": None}


def test_default_manager_fails_closed_off_windows(configured):
    if tm.os.name == "nt":
        pytest.skip("This assertion is specifically the non-Windows guard")
    c = configured
    manager = tm.TunnelManager()
    manager.configure(c.settings, URL)
    assert manager.connect(KEY, BEARER)["status_code"] == "tunnel_windows_required"
    assert not manager.snapshot()["owned"]
    assert manager.close()


def test_health_exception_not_echoed_and_owned_job_kept_visible(configured):
    c = configured
    c.manager.connect(KEY, BEARER)

    def fail(port):
        raise RuntimeError(PRIVATE)

    c.manager._health_probe = fail
    state = c.manager.poll()
    assert state["status_code"] == "tunnel_health_unavailable"
    assert state["process_alive"] and state["owned"] and c.lock.held
    assert PRIVATE not in repr(state) + repr(c.capture["events"])
    assert c.process.stop_calls == 0


def test_native_process_status_failure_retains_uncertain_ownership(configured):
    c = configured
    c.manager.connect(KEY, BEARER)

    def fail():
        raise RuntimeError(PRIVATE)

    c.process.poll = fail
    state = c.manager.poll()
    assert state["status_code"] == "tunnel_stop_unconfirmed"
    assert state["ownership_uncertain"] and state["owned"] and c.lock.held
    assert c.process.stop_calls == 1
    c.process.poll = lambda: 1
    assert c.manager.close()


def test_long_poll_fresh_evidence_does_not_imply_app_verification():
    payload = healthy(
        details={
            "consecutive_failures": 0,
            "last_success": stamp(NOW - 300),
            "deadline_seconds": 600,
        }
    )["control_plane"]
    assert tm.classify_control_plane(payload, now=NOW, started_at=NOW - 400) == (
        True,
        "tunnel_cloud_poll_observed",
    )
    payload["details"]["deadline_seconds"] = float("inf")
    assert tm.classify_control_plane(payload, now=NOW, started_at=NOW - 400)[0] is False


@pytest.mark.parametrize("change", ["comment", "tunnel_id", "port", "oversized", "deleted"])
def test_canonical_config_edit_requires_explicit_reload_before_connect(configured, change):
    c = configured
    path = c.settings.broker_config
    if change == "comment":
        path.write_text(path.read_text() + "# changed externally\n")
    elif change == "tunnel_id":
        path.write_text(path.read_text() + '[tunnel]\ntunnel_id = "tunnel_' + "2" * 32 + '"\n')
    elif change == "port":
        path.write_text("port = 18764\n")
    elif change == "oversized":
        path.write_bytes(b"#" * (tm.CONFIG_MAX_BYTES + 1))
    else:
        path.unlink()
    state = c.manager.connect(KEY, BEARER)
    assert state["status_code"] == "tunnel_configuration_changed"
    assert state["state"] == "disconnected" and not state["owned"]
    assert c.capture["starts"] == c.capture["broker_calls"] == c.capture["verifies"] == 0


def test_external_config_edit_does_not_touch_active_client(configured):
    c = configured
    c.manager.connect(KEY, BEARER)
    c.settings.broker_config.write_text(c.settings.broker_config.read_text() + "# external edit\n")
    state = c.manager.poll()
    assert state["state"] == "running" and state["cloud_poll_observed"]
    assert c.process.stop_calls == 0
    c.manager.disconnect()
    assert c.manager.connect(KEY, BEARER)["status_code"] == "tunnel_configuration_changed"
    assert c.capture["starts"] == 1
    assert c.manager.configure(c.settings, URL)["status_code"] == "tunnel_configured"
    # The fixture returns the same synthetic process, so reset its exit state.
    c.process.exit_code, c.process.children = None, 1
    assert c.manager.connect(KEY, BEARER)["state"] == "running"
    assert c.capture["starts"] == 2


def test_config_edit_during_broker_probe_cannot_spawn_old_configuration(configured):
    c = configured

    def probe(url, bearer):
        path = c.settings.broker_config
        path.write_text(path.read_text() + "# modified during connection\n")
        return c.identity

    c.manager._broker_probe = probe
    state = c.manager.connect(KEY, BEARER)
    assert state["status_code"] == "tunnel_configuration_changed"
    assert c.capture["starts"] == 0 and not c.lock.held
