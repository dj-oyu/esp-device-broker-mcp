"""Pinned precedence of the console and GUI Tunnel checks; synthetic, offline only.

The console (`tunnel.preflight`/`tunnel.check_broker`) and the GUI manager
(`TunnelManager._validate_configuration`/`broker_probe` + `_check_broker`) apply
deliberately different policies. These matrices pin every refusal code, its
precedence and the MCP calls made, so a shared core cannot silently merge them.
"""

import itertools
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from filelock import Timeout

from cardputer_mcp import tunnel
from cardputer_mcp import tunnel_manager as tm
from cardputer_mcp.client import SURFACE_TOOLS

URL = "http://127.0.0.1:18765/mcp"
BEARER = "Bearer " + "a" * 64
PLACEHOLDER = "tunnel_" + "0" * 32
REAL_ID = "tunnel_" + "1" * 32

# ---------------------------------------------------------------- configuration

CONFIG_CONDITIONS = ("placeholder", "ingress", "serial", "health_port", "url")


def _settings(tmp_path, failing):
    return tunnel.TunnelSettings(
        client_exe=tmp_path / "tunnel-client.exe",
        client_archive=tmp_path / "client.zip",
        broker_config=tmp_path / "broker.toml",
        tunnel_id=PLACEHOLDER if "placeholder" in failing else REAL_ID,
        health_port=18765 if "health_port" in failing else 18766,
        ingress_enabled="ingress" in failing,
    )


def _fake_broker(failing):
    return SimpleNamespace(
        backend="serial" if "serial" in failing else "mock",
        port=18765,
        tunnel=None,
    )


def _subsets(names):
    for size in range(len(names) + 1):
        yield from (set(combo) for combo in itertools.combinations(names, size))


def _console_config_expected(failing):
    # The console never receives a URL argument, so "url" is irrelevant to it.
    for name, code in (
        ("placeholder", "tunnel_id_placeholder"),
        ("ingress", "tunnel_config_conflict"),
        ("serial", "serial_tunnel_requires_explicit_enable_serial"),
        ("health_port", "health_port_conflicts_with_broker"),
    ):
        if name in failing:
            return code
    return None


def _gui_config_expected(failing):
    for name, code in (
        ("placeholder", "tunnel_id_placeholder"),
        ("ingress", "tunnel_config_conflict"),
        ("url", "tunnel_broker_url_mismatch"),
        ("health_port", "health_port_conflicts_with_broker"),
        ("serial", "serial_tunnel_requires_explicit_enable_serial"),
    ):
        if name in failing:
            return code
    return None


def _console_fakes(monkeypatch, events, broker):
    def load(path):
        events.append("load")
        return broker

    monkeypatch.setattr(tunnel, "load_settings", load)
    monkeypatch.setattr(tunnel, "verify_release", lambda selected: events.append("verify"))
    monkeypatch.setattr(tunnel, "check_listener", lambda port: events.append("listener"))

    class Lock:
        def __init__(self, path, timeout):
            assert timeout == 0

        def __enter__(self):
            events.append("lock")
            return self

        def __exit__(self, *args):
            pass

    monkeypatch.setattr(tunnel, "FileLock", Lock)


def test_console_preflight_precedence_matrix(tmp_path, monkeypatch):
    for failing in _subsets(CONFIG_CONDITIONS):
        for enable_serial in (False, True):
            events: list[str] = []
            _console_fakes(monkeypatch, events, _fake_broker(failing))
            settings = _settings(tmp_path, failing)
            effective = failing - ({"serial"} if enable_serial else set())
            expected = _console_config_expected(effective)
            if expected is None:
                result = tunnel.preflight(settings, enable_serial=enable_serial)
                assert result == {
                    "ok": True,
                    "broker_config": str(settings.broker_config),
                    "backend": "serial" if "serial" in failing else "mock",
                    "url": "http://127.0.0.1:18765/mcp",
                }
                assert events == ["load", "verify", "listener", "lock"]
            else:
                with pytest.raises(tunnel.TunnelError, match=f"^{expected}$"):
                    tunnel.preflight(settings, enable_serial=enable_serial)
                assert events == ([] if expected == "tunnel_id_placeholder" else ["load"]), (
                    failing,
                    enable_serial,
                )


@pytest.mark.parametrize(
    "step,code",
    [
        ("verify", "client_release_verification_failed"),
        ("listener", "tunnel_health_listener_exists"),
        ("lock", "tunnel_launcher_already_running"),
    ],
)
def test_console_preflight_post_configuration_order(tmp_path, monkeypatch, step, code):
    events: list[str] = []
    _console_fakes(monkeypatch, events, _fake_broker(set()))

    def refuse(name):
        def run(*args):
            events.append(name)
            raise tunnel.TunnelError(code)

        return run

    if step == "verify":
        monkeypatch.setattr(tunnel, "verify_release", refuse("verify"))
    elif step == "listener":
        monkeypatch.setattr(tunnel, "check_listener", refuse("listener"))
    else:

        class Locked:
            def __init__(self, path, timeout):
                pass

            def __enter__(self):
                events.append("lock")
                raise Timeout("held")

            def __exit__(self, *args):
                pass

        monkeypatch.setattr(tunnel, "FileLock", Locked)
    with pytest.raises(tunnel.TunnelError, match=f"^{code}$"):
        tunnel.preflight(_settings(tmp_path, set()))
    reached = {
        "verify": ["load", "verify"],
        "listener": ["load", "verify", "listener"],
        "lock": ["load", "verify", "listener", "lock"],
    }
    assert events == reached[step]


def test_gui_validate_configuration_precedence_matrix(tmp_path, monkeypatch):
    for failing in _subsets(CONFIG_CONDITIONS):
        for enable_serial in (False, True):
            events: list[str] = []
            broker = _fake_broker(failing)

            def load(path, broker=broker, events=events):
                events.append("load")
                return broker

            monkeypatch.setattr(tm, "load_settings", load)
            manager = tm.TunnelManager()
            manager._enable_serial = enable_serial
            url = "http://127.0.0.1:18799/mcp" if "url" in failing else URL
            effective = failing - ({"serial"} if enable_serial else set())
            expected = _gui_config_expected(effective)
            settings = _settings(tmp_path, failing)
            if expected is None:
                assert manager._validate_configuration(settings, url) == broker.backend
                assert events == ["load"]
            else:
                with pytest.raises(tunnel.TunnelError, match=f"^{expected}$"):
                    manager._validate_configuration(settings, url)
                assert events == ([] if expected == "tunnel_id_placeholder" else ["load"])


@pytest.mark.parametrize(
    "url",
    ["http://localhost:18765/mcp", "http://127.0.0.1:18765/mcp/", "http://127.0.0.1:18765"],
)
def test_gui_url_must_equal_canonical_broker_url_exactly(tmp_path, monkeypatch, url):
    monkeypatch.setattr(tm, "load_settings", lambda path: _fake_broker(set()))
    with pytest.raises(tunnel.TunnelError, match="^tunnel_broker_url_mismatch$"):
        tm.TunnelManager()._validate_configuration(_settings(tmp_path, set()), url)


@pytest.mark.parametrize("runtime", [None, False, True])
@pytest.mark.parametrize("enabled", [False, True])
def test_ingress_conflict_uses_canonical_broker_mode(tmp_path, monkeypatch, runtime, enabled):
    events: list[str] = []
    broker = SimpleNamespace(
        backend="mock",
        port=18765,
        tunnel=None if runtime is None else SimpleNamespace(ingress_enabled=runtime),
    )
    _console_fakes(monkeypatch, events, broker)
    monkeypatch.setattr(tm, "load_settings", lambda path: broker)
    settings = _settings(tmp_path, {"ingress"} if enabled else set())
    conflict = enabled != bool(runtime)
    if conflict:
        with pytest.raises(tunnel.TunnelError, match="^tunnel_config_conflict$"):
            tunnel.preflight(settings)
        with pytest.raises(tunnel.TunnelError, match="^tunnel_config_conflict$"):
            tm.TunnelManager()._validate_configuration(settings, URL)
    else:
        assert tunnel.preflight(settings)["backend"] == "mock"
        assert tm.TunnelManager()._validate_configuration(settings, URL) == "mock"


# ---------------------------------------------------------------- broker probe

BROKER_CONDITIONS = (
    "identity_not_ok",
    "unauthenticated",
    "origin",
    "scope",
    "ingress",
    "status_not_ok",
    "active_job",
    "owner",
    "hardware",
    "uncertain",
    "backend",
    "discovery",
)


def _observations(failing):
    identity = {
        "ok": "identity_not_ok" not in failing,
        "authenticated": "unauthenticated" not in failing,
        "origin": "local_cli" if "origin" in failing else "remote_mcp",
        "scopes": ["control"] if "scope" in failing else ["read"],
        "tunnel_ingress_enabled": "ingress" in failing,
    }
    status = {"ok": "status_not_ok" not in failing, "backend": "mock"}
    if "active_job" in failing:
        status["active_job"] = "job"
    if "owner" in failing:
        status["owner"] = "owner"
    if "hardware" in failing:
        status["hardware_connected"] = True
    if "uncertain" in failing:
        status["ownership_uncertain"] = True
    if "backend" in failing:
        status["backend"] = "serial"
    names = list(SURFACE_TOOLS)
    if "discovery" in failing:
        names = names[1:]
    return identity, status, names


def _probe_fakes(failing, events):
    identity, status, names = _observations(failing)

    class Client:
        async def list_tools(self):
            events.append("list_tools")
            return SimpleNamespace(tools=[SimpleNamespace(name=name) for name in names])

    @asynccontextmanager
    async def connection(url, token):
        assert url == URL and token == BEARER.removeprefix("Bearer ")
        events.append("enter")
        try:
            yield Client()
        except BaseException:
            events.append("exit_error")
            raise
        events.append("exit")

    async def call(client, tool, request):
        assert tool == "status"
        events.append(request["view"])
        return dict(identity) if request["view"] == "identity" else dict(status)

    return connection, call


def _console_broker_expected(failing):
    for names, code in (
        (("identity_not_ok", "unauthenticated"), "tunnel_broker_auth_failed"),
        (("origin",), "tunnel_broker_origin_rejected"),
        (("scope",), "tunnel_broker_scope_rejected"),
        (("ingress",), "tunnel_broker_ingress_mismatch"),
    ):
        if failing.intersection(names):
            return code, ["enter", "identity", "exit_error"]
    calls = ["enter", "identity", "device", "list_tools", "exit"]
    for names, code in (
        (("status_not_ok", "active_job", "owner"), "broker_not_idle"),
        (("hardware", "uncertain"), "broker_hardware_not_closed"),
        (("discovery",), "mcp_discovery_failed"),
    ):
        if failing.intersection(names):
            return code, calls
    return None, calls


def _gui_broker_expected(failing):
    calls = ["enter", "identity", "device", "list_tools"]
    if "discovery" in failing:
        return "tunnel_broker_discovery_failed", [*calls, "exit_error"]
    calls.append("exit")
    for names, code in (
        (("unauthenticated",), "tunnel_broker_auth_failed"),
        (("origin",), "tunnel_broker_origin_rejected"),
        (("scope",), "tunnel_broker_scope_rejected"),
        (("backend",), "tunnel_broker_backend_mismatch"),
        (("ingress",), "tunnel_broker_ingress_mismatch"),
        (("status_not_ok",), "tunnel_broker_status_invalid"),
    ):
        if failing.intersection(names):
            return code, calls
    return None, calls


# One event loop per test: repeated asyncio.run() calls are needlessly slow and
# have stalled in local Windows sandboxes while creating the loop self-pipe.
async def test_console_check_broker_precedence_and_calls(monkeypatch):
    for failing in _subsets(BROKER_CONDITIONS):
        events: list[str] = []
        connection, call = _probe_fakes(failing, events)
        monkeypatch.setattr(tunnel, "connect", connection)
        monkeypatch.setattr(tunnel, "call", call)
        expected, calls = _console_broker_expected(failing)
        if expected is None:
            result = await tunnel.check_broker(URL, BEARER)
            assert result == {
                "ok": True,
                "backend": "serial" if "backend" in failing else "mock",
                "needs_review": False,
                "cloud_verified": False,
            }
        else:
            with pytest.raises(tunnel.TunnelError, match=f"^{expected}$"):
                await tunnel.check_broker(URL, BEARER)
        assert events == calls, failing


async def test_console_check_broker_reports_needs_review(monkeypatch):
    events: list[str] = []
    connection, call = _probe_fakes(set(), events)

    async def reviewing(client, tool, request):
        result = await call(client, tool, request)
        if request["view"] == "device":
            result["needs_review"] = "yes"
        return result

    monkeypatch.setattr(tunnel, "connect", connection)
    monkeypatch.setattr(tunnel, "call", reviewing)
    assert (await tunnel.check_broker(URL, BEARER))["needs_review"] is True


async def test_gui_probe_and_check_precedence_and_calls(monkeypatch):
    # ``broker_probe`` is ``asyncio.run(_broker_probe_async(...))`` off-loop; the
    # coroutine is driven directly here and the wrapper is pinned separately.
    for failing in _subsets(BROKER_CONDITIONS):
        events: list[str] = []
        connection, call = _probe_fakes(failing, events)
        monkeypatch.setattr(tm, "connect_mcp", connection)
        monkeypatch.setattr(tm, "call", call)
        expected, calls = _gui_broker_expected(failing)
        manager = tm.TunnelManager()
        if expected == "tunnel_broker_discovery_failed":
            with pytest.raises(tunnel.TunnelError, match=f"^{expected}$"):
                await tm._broker_probe_async(URL, BEARER)
        else:
            result = await tm._broker_probe_async(URL, BEARER)
            identity, status, _ = _observations(failing)
            assert result == {**identity, "backend": status["backend"], "status": status}
            if expected is None:
                assert manager._check_broker(result, "mock", False) is None
            else:
                with pytest.raises(tunnel.TunnelError, match=f"^{expected}$"):
                    manager._check_broker(result, "mock", False)
        assert events == calls, failing


def test_gui_sync_probe_wrapper_runs_the_same_calls(monkeypatch):
    events: list[str] = []
    connection, call = _probe_fakes({"active_job", "hardware"}, events)
    monkeypatch.setattr(tm, "connect_mcp", connection)
    monkeypatch.setattr(tm, "call", call)
    result = tm.broker_probe(URL, BEARER)
    assert result["status"]["active_job"] == "job" and result["backend"] == "mock"
    assert events == ["enter", "identity", "device", "list_tools", "exit"]


async def test_gui_sync_probe_wrapper_refuses_a_running_loop():
    with pytest.raises(tunnel.TunnelError, match="^tunnel_worker_required$"):
        tm.broker_probe(URL, BEARER)


@pytest.mark.parametrize("status", [None, "ok", ["ok"], {"ok": 1}, {"ok": "true"}])
def test_gui_check_requires_a_status_mapping_with_ok_true(status):
    result = {
        "authenticated": True,
        "origin": "remote_mcp",
        "scopes": ["read"],
        "backend": "mock",
        "tunnel_ingress_enabled": False,
        "status": status,
    }
    with pytest.raises(tunnel.TunnelError, match="^tunnel_broker_status_invalid$"):
        tm.TunnelManager()._check_broker(result, "mock", False)


@pytest.mark.parametrize("enabled", [1, "true", None])
def test_both_ingress_checks_require_a_strict_boolean_expectation(enabled):
    identity = {"tunnel_ingress_enabled": True}
    with pytest.raises(tunnel.TunnelError, match="^tunnel_broker_ingress_mismatch$"):
        tunnel.check_ingress_mode(identity, enabled)
