"""Deterministic synthetic Tunnel lifecycles; no client, account, or USB use."""

import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from filelock import Timeout

from cardputer_mcp import tunnel_manager as tm
from cardputer_mcp.client import SURFACE_TOOLS
from cardputer_mcp.tunnel import TunnelError, TunnelSettings

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
    c.settings.broker_config.write_text(c.settings.broker_config.read_text() + "# external edit\n")
    state = c.manager.poll()
    assert state["state"] == "running" and state["owned"] and c.process.stop_calls == 0
    assert c.manager.configure(c.settings, URL)["status_code"] == "tunnel_configuration_locked"
    assert c.manager.disconnect()["state"] == "disconnected"
    assert c.manager.disconnect()["state"] == "disconnected"
    assert c.process.stop_calls == 1


@pytest.mark.parametrize(
    "change,code",
    [
        ({"authenticated": False}, "tunnel_broker_auth_failed"),
        ({"origin": "local_cli"}, "tunnel_broker_origin_rejected"),
        ({"backend": "serial"}, "tunnel_broker_backend_mismatch"),
    ],
)
def test_broker_validation_fail_closed(configured, change, code):
    c = configured
    c.identity.update(change)
    state = c.manager.connect(KEY, BEARER)
    assert state["status_code"] == code and state["state"] == "disconnected"
    assert c.capture["starts"] == 0 and not c.lock.held
    assert PRIVATE not in repr(state)


@pytest.mark.parametrize("url", ["http://localhost:18765/mcp", "http://127.0.0.1:18767/mcp"])
def test_exact_current_broker_url_required(configured, url):
    c = configured
    state = c.manager.configure(c.settings, url)
    assert state["status_code"] == "tunnel_broker_url_mismatch"
    assert not state["configured"]
    assert c.manager.connect(KEY, BEARER)["status_code"] == "tunnel_not_configured"


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
    "updates,code,age",
    [
        ({"schema_version": 2}, "tunnel_health_schema_unsupported", 500),
        ({"snapshot_at": stamp(NOW - 20)}, "tunnel_health_stale", 500),
        ({"details": {"consecutive_failures": 0}}, "tunnel_cloud_poll_unobserved", 500),
        (
            {"details": {"consecutive_failures": 0, "last_success": stamp(NOW - 300)}},
            "tunnel_cloud_poll_stale",
            500,
        ),
        (
            {"details": {"consecutive_failures": 0, "last_success": stamp(NOW - 20)}},
            "tunnel_cloud_poll_stale",
            0,  # success from a previous child is not evidence
        ),
        (
            {"details": {"consecutive_failures": 1}, "status": "degraded"},
            "tunnel_cloud_poll_degraded",
            500,
        ),
    ],
)
def test_control_plane_requires_fresh_supported_success(updates, code, age):
    payload = healthy(**updates)["control_plane"]
    assert tm.classify_control_plane(payload, now=NOW, started_at=NOW - age) == (False, code)


def test_only_readyz_does_not_verify_cloud_or_app(configured):
    c = configured
    c.health["control_status"] = 404
    c.manager.connect(KEY, BEARER)
    state = c.manager.poll()
    assert state["local_readiness"] and not state["cloud_poll_observed"]
    assert state["app_verified"] is False and state["status_code"] == "tunnel_local_ready"


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


@pytest.mark.parametrize("change", ["tunnel_id", "port", "deleted", "during_probe"])
def test_canonical_config_edit_requires_explicit_reload_before_connect(configured, change):
    c = configured
    path = c.settings.broker_config
    if change == "tunnel_id":
        path.write_text(path.read_text() + '[tunnel]\ntunnel_id = "tunnel_' + "2" * 32 + '"\n')
    elif change == "port":
        path.write_text("port = 18764\n")
    elif change == "during_probe":

        def probe(url, bearer):
            path.write_text(path.read_text() + "# modified during connection\n")
            return c.identity

        c.manager._broker_probe = probe
    else:
        path.unlink()
    state = c.manager.connect(KEY, BEARER)
    assert state["status_code"] == "tunnel_configuration_changed"
    assert state["state"] == "disconnected" and not state["owned"]
    assert c.capture["starts"] == c.capture["broker_calls"] == 0 and not c.lock.held
    assert c.capture["verifies"] == (change == "during_probe")
