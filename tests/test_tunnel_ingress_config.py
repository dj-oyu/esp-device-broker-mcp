"""Synthetic ingress configuration and launch routing; no cloud, client or USB."""

import json
import sys
import tomllib
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from test_gui_tunnel import URL as GUI_URL
from test_gui_tunnel import FakeManager
from test_tunnel_config import broker_file, legacy_file, metadata
from test_tunnel_manager import BEARER, KEY, URL
from test_tunnel_manager import configured as configured

from cardputer_mcp import tunnel
from cardputer_mcp.client import SURFACE_TOOLS, validate_url
from cardputer_mcp.config import Settings, load_settings
from cardputer_mcp.gui_tunnel import TunnelController, perform_tunnel


@pytest.mark.parametrize("value", [0, 1, "true", "false", None, [], {}])
def test_ingress_mode_is_a_strict_boolean_in_both_models(value):
    with pytest.raises(ValidationError):
        Settings(tunnel={"ingress_enabled": value})
    with pytest.raises(ValidationError):
        tunnel.TunnelSettings(**metadata(ingress_enabled=value))


@pytest.mark.parametrize("values", [None, {}, metadata()])
def test_canonical_omission_is_default_off(tmp_path, values):
    path = broker_file(tmp_path, values)
    broker = load_settings(path)
    assert broker.tunnel is None or broker.tunnel.ingress_enabled is False
    if values == metadata():
        assert tunnel.load_config(path, tmp_path).ingress_enabled is False


@pytest.mark.parametrize("enabled", [False, True])
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


@pytest.mark.parametrize("enabled", [False, True])
def test_old_legacy_omission_does_not_override_canonical_mode(tmp_path, enabled):
    path = broker_file(tmp_path, metadata(ingress_enabled=enabled))
    legacy = legacy_file(tmp_path)
    before = legacy.read_bytes()
    assert tunnel.load_config(legacy, tmp_path).ingress_enabled is enabled
    settings = tunnel.load_config(path, tmp_path)
    tunnel.save_gui_config(path, tmp_path, settings)
    assert tunnel.load_gui_config(path, tmp_path).ingress_enabled is enabled
    assert legacy.read_bytes() == before


@pytest.mark.parametrize("enabled", [False, True])
def test_matching_explicit_legacy_mode_round_trips(tmp_path, enabled):
    path = broker_file(tmp_path, metadata(ingress_enabled=enabled))
    legacy = legacy_file(tmp_path, metadata(ingress_enabled=enabled))
    assert tunnel.load_config(legacy, tmp_path) == tunnel.load_config(path, tmp_path)


@pytest.mark.parametrize("canonical", [None, False, True])
def test_explicit_conflicting_legacy_mode_fails_closed(tmp_path, canonical):
    values = metadata() if canonical is None else metadata(ingress_enabled=canonical)
    path = broker_file(tmp_path, values)
    legacy = legacy_file(tmp_path, metadata(ingress_enabled=canonical is not True))
    originals = path.read_bytes(), legacy.read_bytes()
    for selected in (path, legacy):
        with pytest.raises(tunnel.TunnelError, match="^tunnel_config_conflict$"):
            tunnel.load_config(selected, tmp_path)
    assert (path.read_bytes(), legacy.read_bytes()) == originals


@pytest.mark.parametrize("as_model", [False, True])
def test_save_omitted_mode_preserves_canonical_true(tmp_path, as_model):
    path = broker_file(tmp_path, metadata(ingress_enabled=True))
    selected = metadata(health_port=18766)
    if as_model:
        selected = tunnel.TunnelSettings(**selected, broker_config=path)
    tunnel.save_gui_config(path, tmp_path, selected)
    assert load_settings(path).tunnel.ingress_enabled is True
    assert load_settings(path).tunnel.health_port == 18766


def test_gui_load_and_save_preserve_read_only_mode_and_direct_broker_url(tmp_path):
    path = broker_file(tmp_path, metadata(ingress_enabled=True), port=8765)
    manager = FakeManager()
    seen = []
    configure = manager.configure

    def capture(settings, url, **kwargs):
        seen.append((settings.ingress_enabled, url))
        return configure(settings, url, **kwargs)

    manager.configure = capture
    controller = TunnelController()
    operation = controller.load(path)
    assert controller.accept(perform_tunnel(manager, operation, GUI_URL, tmp_path))
    assert controller.metadata["ingress_enabled"] is True
    operation = controller.save(metadata(health_port=18766))
    assert controller.accept(perform_tunnel(manager, operation, GUI_URL, tmp_path))
    assert controller.metadata["ingress_enabled"] is True
    assert tomllib.loads(path.read_text())["tunnel"]["ingress_enabled"] is True
    assert seen == [(True, GUI_URL), (True, GUI_URL)]
    assert manager.calls == ["configure", "configure"]


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
        "http://192.0.2.1:18765/mcp",
        "http://127.0.0.1:18765/mcp?x=1",
        "http://127.0.0.1:18765/mcp#x",
        "http://user@127.0.0.1:18765/mcp",
        "http://127.0.0.1:18765/mcp/",
        "http://127.0.0.1:01024/mcp",
        "http://127.0.0.1:1000/mcp",
        "http://127.0.0.1:65536/mcp",
        "http://127.0.0.1:18765/mcp\n",
    ],
)
def test_child_arguments_reject_arbitrary_or_noncanonical_targets(tmp_path, url):
    path = broker_file(tmp_path, metadata(ingress_enabled=True))
    with pytest.raises(tunnel.TunnelError, match="^invalid_local_broker_url$"):
        tunnel.client_arguments(tunnel.load_config(path, tmp_path), url)


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("runtime", [None, 0, 1, "true", "false", "opposite"])
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


def test_manager_enabled_mode_probes_direct_route_then_starts_only_ingress_child(configured):
    c = configured
    c.settings.broker_config.write_text("port = 18765\n[tunnel]\ningress_enabled = true\n")
    settings = c.settings.model_copy(update={"ingress_enabled": True})
    c.identity["tunnel_ingress_enabled"] = True
    assert c.manager.configure(settings, URL)["configured"] is True
    assert c.manager.connect(KEY, BEARER)["state"] == "running"
    assert c.capture["broker_calls"] == c.capture["starts"] == 1
    assert "--mcp.server-url=http://127.0.0.1:18765/tunnel/mcp" in c.capture["args"]
    assert c.manager.close() is True


def test_manager_refuses_unloaded_config_mode_before_any_probe(configured):
    c = configured
    settings = c.settings.model_copy(update={"ingress_enabled": True})
    result = c.manager.configure(settings, URL)
    assert result["status_code"] == "tunnel_config_conflict"
    assert result["configured"] is False
    assert c.capture["broker_calls"] == c.capture["starts"] == 0


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


@pytest.mark.parametrize("enabled", [False, True])
async def test_console_matching_mode_is_checked_on_direct_route(console_probe, enabled):
    identity, calls = console_probe
    identity["tunnel_ingress_enabled"] = enabled
    result = await tunnel.check_broker(URL, BEARER, ingress_enabled=enabled)
    assert result["ok"] is True and result["cloud_verified"] is False
    assert calls == ["identity", "device"]


@pytest.mark.parametrize("runtime", [None, 1, 0, "true", False])
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
        ({"authenticated": False}, "tunnel_broker_auth_failed"),
        ({"authenticated": 1}, "tunnel_broker_auth_failed"),
        ({"origin": "local_cli"}, "tunnel_broker_origin_rejected"),
        ({"scopes": None}, "tunnel_broker_scope_rejected"),
        ({"scopes": ["control"]}, "tunnel_broker_scope_rejected"),
        ({"scopes": "read"}, "tunnel_broker_scope_rejected"),
    ],
)
async def test_console_requires_remote_read_identity_before_child(console_probe, change, code):
    identity, calls = console_probe
    identity.update(change)
    with pytest.raises(tunnel.TunnelError, match="^" + code + "$"):
        await tunnel.check_broker(URL, BEARER)
    assert calls == ["identity"]


@pytest.mark.parametrize("runtime", [False, True])
def test_console_check_command_uses_selected_canonical_mode(
    tmp_path, monkeypatch, capsys, console_probe, runtime
):
    identity, _ = console_probe
    identity["tunnel_ingress_enabled"] = runtime
    path = broker_file(tmp_path, {"ingress_enabled": True})
    monkeypatch.setenv("CARDPUTER_REMOTE_TOKEN", BEARER.removeprefix("Bearer "))
    monkeypatch.setattr(
        sys,
        "argv",
        ["tunnel", "check", "--project", str(tmp_path), "--config", str(path), "--url", URL],
    )
    if runtime:
        tunnel.main()
        assert json.loads(capsys.readouterr().out)["ok"] is True
    else:
        with pytest.raises(SystemExit) as caught:
            tunnel.main()
        assert caught.value.code == 1
        assert json.loads(capsys.readouterr().out) == {
            "ok": False,
            "error": "tunnel_broker_ingress_mismatch",
        }


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
