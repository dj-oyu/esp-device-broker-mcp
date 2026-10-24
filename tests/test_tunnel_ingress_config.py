"""Synthetic ingress configuration and launch routing; no cloud, client or USB."""

import json
import sys
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from test_tunnel_config import broker_file, metadata
from test_tunnel_manager import BEARER, KEY, URL
from test_tunnel_manager import configured as configured

from cardputer_mcp import tunnel
from cardputer_mcp.client import SURFACE_TOOLS, validate_url
from cardputer_mcp.config import Settings, load_settings


@pytest.mark.parametrize("value", [1, "false", None])
def test_ingress_mode_is_a_strict_boolean_in_both_models(value):
    with pytest.raises(ValidationError):
        Settings(tunnel={"ingress_enabled": value})
    with pytest.raises(ValidationError):
        tunnel.TunnelSettings(**metadata(ingress_enabled=value))


@pytest.mark.parametrize("values", [metadata()])
def test_canonical_omission_is_default_off(tmp_path, values):
    path = broker_file(tmp_path, values)
    broker = load_settings(path)
    assert broker.tunnel is None or broker.tunnel.ingress_enabled is False
    if values == metadata():
        assert tunnel.load_config(path, tmp_path).ingress_enabled is False


@pytest.mark.parametrize("enabled", [True])
def test_canonical_round_trip_keeps_explicit_mode_without_any_start(tmp_path, monkeypatch, enabled):
    path = broker_file(tmp_path, metadata(ingress_enabled=enabled))
    monkeypatch.setattr(tunnel.subprocess, "Popen", lambda *a, **kw: pytest.fail("started client"))
    monkeypatch.setattr(tunnel.socket, "socket", lambda *a, **kw: pytest.fail("opened socket"))
    settings = tunnel.load_config(path, tmp_path)
    assert settings.ingress_enabled is enabled
    assert tunnel.load_gui_config(path, tmp_path) == settings
    tunnel.save_gui_config(path, tmp_path, settings)
    assert load_settings(path).tunnel.ingress_enabled is enabled
    assert tunnel.load_config(path, tmp_path) == settings
    tunnel.save_gui_config(path, tmp_path, metadata(health_port=18766))
    assert load_settings(path).tunnel.ingress_enabled is True


@pytest.mark.parametrize("enabled", [False, True])
def test_only_child_url_switches_to_exact_ingress_path(tmp_path, enabled):
    path = broker_file(tmp_path, metadata(ingress_enabled=enabled))
    settings = tunnel.load_config(path, tmp_path)
    args = tunnel.client_arguments(settings, URL)
    expected = URL.replace("/mcp", "/tunnel/mcp") if enabled else URL
    assert [arg for arg in args if arg.startswith("--mcp.server-url=")] == [
        "--mcp.server-url=" + expected
    ]
    assert validate_url(URL) == URL
    with pytest.raises(ValueError):
        validate_url(URL.replace("/mcp", "/tunnel/mcp"))


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:18765/tunnel/mcp",
        "https://127.0.0.1:18765/mcp",
        "http://localhost:18765/mcp",
        "http://127.0.0.1:18765/mcp?x=1",
    ],
)
def test_child_arguments_reject_arbitrary_or_noncanonical_targets(tmp_path, url):
    path = broker_file(tmp_path, metadata(ingress_enabled=True))
    with pytest.raises(tunnel.TunnelError, match="^invalid_local_broker_url$"):
        tunnel.client_arguments(tunnel.load_config(path, tmp_path), url)


@pytest.mark.parametrize(
    ("enabled", "runtime"), [(True, None), (False, 1), (True, "opposite"), (False, "true")]
)
def test_manager_refuses_stale_unknown_or_nonboolean_runtime_before_child(
    configured, enabled, runtime
):
    c = configured
    c.settings.broker_config.write_text(
        f'backend = "mock"\nport = 18765\n[tunnel]\ningress_enabled = {str(enabled).lower()}\n'
    )
    settings = c.settings.model_copy(update={"ingress_enabled": enabled})
    assert c.manager.configure(settings, URL)["configured"] is True
    c.identity["tunnel_ingress_enabled"] = not enabled if runtime == "opposite" else runtime
    if runtime is None:
        del c.identity["tunnel_ingress_enabled"]
    result = c.manager.connect(KEY, BEARER)
    assert result["status_code"] == "tunnel_broker_ingress_mismatch"
    assert result["owned"] is False
    assert c.capture["starts"] == 0 and not c.lock.held


@pytest.fixture
def console_probe(monkeypatch):
    identity = {
        "ok": True,
        "authenticated": True,
        "origin": "remote_mcp",
        "scopes": ["read"],
        "tunnel_ingress_enabled": False,
    }
    calls = []

    class Client:
        async def list_tools(self):
            return SimpleNamespace(tools=[SimpleNamespace(name=name) for name in SURFACE_TOOLS])

    @asynccontextmanager
    async def connection(url, token):
        assert url == URL and token == BEARER.removeprefix("Bearer ")
        yield Client()

    async def call(client, tool, request):
        # Only the read-only status tool is ever used by the console probe.
        assert tool == "status"
        calls.append(request["view"])
        if request["view"] == "identity":
            return identity
        assert request == {"view": "device"}
        return {"ok": True, "backend": "mock"}

    monkeypatch.setattr(tunnel, "connect", connection)
    monkeypatch.setattr(tunnel, "call", call)
    return identity, calls


@pytest.mark.parametrize("runtime", [None, "true"])
async def test_console_unknown_or_stale_mode_fails_before_further_probes(console_probe, runtime):
    identity, calls = console_probe
    identity["tunnel_ingress_enabled"] = runtime
    with pytest.raises(tunnel.TunnelError, match="^tunnel_broker_ingress_mismatch$"):
        await tunnel.check_broker(URL, BEARER, ingress_enabled=True)
    assert calls == ["identity"]


@pytest.mark.parametrize(
    "change,code",
    [
        ({"ok": False}, "tunnel_broker_auth_failed"),
        ({"origin": "local_cli"}, "tunnel_broker_origin_rejected"),
        ({"scopes": ["control"]}, "tunnel_broker_scope_rejected"),
    ],
)
async def test_console_requires_remote_read_identity_before_child(console_probe, change, code):
    identity, calls = console_probe
    identity.update(change)
    with pytest.raises(tunnel.TunnelError, match="^" + code + "$"):
        await tunnel.check_broker(URL, BEARER)
    assert calls == ["identity"]


def test_console_run_refuses_stale_broker_before_child(
    tmp_path, monkeypatch, capsys, console_probe
):
    path = broker_file(tmp_path, metadata(ingress_enabled=True))
    monkeypatch.setattr(tunnel, "preflight", lambda *a, **kw: {"url": URL})
    monkeypatch.setattr(tunnel, "supervise", lambda *a, **kw: pytest.fail("started child"))
    monkeypatch.setenv("CARDPUTER_TUNNEL_API_KEY", KEY)
    monkeypatch.setenv("CARDPUTER_TUNNEL_BEARER", BEARER)
    monkeypatch.setattr(
        sys, "argv", ["tunnel", "run", "--project", str(tmp_path), "--config", str(path)]
    )
    with pytest.raises(SystemExit) as caught:
        tunnel.main()
    assert caught.value.code == 1
    assert json.loads(capsys.readouterr().out) == {
        "ok": False,
        "error": "tunnel_broker_ingress_mismatch",
    }
