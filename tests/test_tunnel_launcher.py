"""Tunnel helpers only: no native Windows execution, cloud account, or hardware."""

import hashlib
import json
import subprocess
import sys
import warnings
import zipfile
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from filelock import Timeout

from cardputer_mcp import tunnel

REMOTE_TOKEN = "a" * 64
BEARER = "Bearer " + REMOTE_TOKEN
RUNTIME_KEY = "SYNTHETIC_RUNTIME_KEY_NEVER_REAL"
PRIVATE = "SYNTHETIC_SECRET_PRIVATE_DETAIL"
MEMBERS = {
    "tunnel-client.exe": b"synthetic executable fixture; never execute",
    "cloudflared.exe": b"synthetic optional companion; never execute",
    "cloudflared-manifest.json": b"{}",
    "LICENSE": b"fixture",
    "NOTICE": b"fixture",
    "tunnel-client-v0.0.15-windows-amd64-licenses.txt": b"fixture",
    "tunnel-client-v0.0.15-windows-amd64.spdx.json": b"{}",
}


@pytest.fixture
def settings(tmp_path):
    return tunnel.TunnelSettings(
        client_exe=tmp_path / "tunnel-client.exe",
        client_archive=tmp_path / "client.zip",
        tunnel_id="tunnel_" + "1" * 32,
        broker_config=tmp_path / "broker.toml",
        health_port=18766,
    )


def make_archive(settings, monkeypatch, members=None):
    entries = MEMBERS if members is None else members
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        with zipfile.ZipFile(settings.client_archive, "w") as archive:
            for name, contents in entries.items() if isinstance(entries, dict) else entries:
                archive.writestr(name, contents)
    settings.client_exe.write_bytes(MEMBERS["tunnel-client.exe"])
    monkeypatch.setattr(
        tunnel, "ARCHIVE_SHA256", hashlib.sha256(settings.client_archive.read_bytes()).hexdigest()
    )


def write_config(tmp_path, extra="", tunnel_id=None):
    path = tmp_path / "private.toml"
    path.write_text(
        'client_exe = "tunnel-client.exe"\n'
        'client_archive = "client.zip"\n'
        f'tunnel_id = "{tunnel_id or "tunnel_" + "1" * 32}"\n' + extra
    )
    return path


def test_placeholder_loads_for_mock_but_preflight_refuses_before_any_probe(tmp_path, monkeypatch):
    config = tunnel.load_config(write_config(tmp_path, tunnel_id="tunnel_" + "0" * 32), tmp_path)
    monkeypatch.setattr(tunnel, "load_settings", lambda *a: pytest.fail("loaded broker"))
    monkeypatch.setattr(tunnel, "verify_release", lambda *a: pytest.fail("probed release"))
    with pytest.raises(tunnel.TunnelError, match="^tunnel_id_placeholder$"):
        tunnel.preflight(config)


def test_release_matches_pinned_zip_member_without_running_it(settings, monkeypatch):
    make_archive(settings, monkeypatch)
    monkeypatch.setattr(
        tunnel.subprocess, "Popen", lambda *a, **kw: pytest.fail("verification executed software")
    )
    tunnel.verify_release(settings)


@pytest.mark.parametrize("tamper", ["archive", "exe"])
def test_release_mismatch_fails_closed(settings, monkeypatch, tamper):
    make_archive(settings, monkeypatch)
    if tamper == "archive":
        settings.client_archive.write_bytes(b"untrusted " + PRIVATE.encode())
    elif tamper == "exe":
        settings.client_exe.write_bytes(b"different executable")
    elif tamper == "renamed":
        renamed = settings.client_exe.with_name("other.exe")
        settings.client_exe.rename(renamed)
        settings = settings.model_copy(update={"client_exe": renamed})
    else:
        settings.client_exe.unlink()
    with pytest.raises(tunnel.TunnelError, match="^client_release_verification_failed$"):
        tunnel.verify_release(settings)


@pytest.mark.parametrize("variant", ["nested", "duplicate", "extra", "symlink"])
def test_archive_member_contract_rejects_unsafe_layout(settings, monkeypatch, variant):
    members = dict(MEMBERS)
    if variant == "nested":
        members["nested/tunnel-client.exe"] = members.pop("tunnel-client.exe")
    elif variant == "duplicate":
        members = [*members.items(), ("tunnel-client.exe", MEMBERS["tunnel-client.exe"])]
    elif variant == "extra":
        members["../unrequested.exe"] = b"extra"
    elif variant == "symlink":
        info = zipfile.ZipInfo("tunnel-client.exe")
        info.create_system = 3
        info.external_attr = 0o120777 << 16
        members = [(info if key == info.filename else key, value) for key, value in members.items()]
    make_archive(settings, monkeypatch, members)
    with pytest.raises(tunnel.TunnelError, match="^client_release_verification_failed$"):
        tunnel.verify_release(settings)


def test_environment_is_allowlisted_and_only_contains_remote_credentials():
    source = {
        "SystemRoot": r"C:\Windows",
        "TEMP": r"C:\Temp",
        "CARDPUTER_LOCAL_TOKEN": "b" * 64,
        "CARDPUTER_REMOTE_TOKEN": "c" * 64,
        "TUNNEL_CLIENT_CONFIG": "hostile.yaml",
        "TUNNEL_CLIENT_PROFILE": "hostile",
        "TUNNEL_CLIENT_PROFILE_FILE": "hostile.yaml",
        "MCP_COMMAND": "malicious-child",
        "MCP_SERVER_URL": "https://untrusted.invalid/mcp",
        "HARPOON_TARGETS": "untrusted",
        "CLOUDFLARED_TUNNEL_TOKEN": PRIVATE,
        "CONTROL_PLANE_API_KEY": PRIVATE,
        "OPENAI_API_KEY": PRIVATE,
        "OPENAI_ADMIN_KEY": PRIVATE,
        "HTTPS_PROXY": "https://untrusted.invalid",
        "CA_BUNDLE": "untrusted.pem",
        "LOG_HTTP_RAW_UNSAFE": "true",
        "LOG_FILE": "unrequested.log",
        "ALLOW_REMOTE_UI": "true",
        "PATH": "untrusted-bin",
        "PYTHONPATH": "untrusted-python",
    }
    result = tunnel.child_environment(source, RUNTIME_KEY, BEARER)
    assert result == {
        "SystemRoot": r"C:\Windows",
        "TEMP": r"C:\Temp",
        "CARDPUTER_TUNNEL_API_KEY": RUNTIME_KEY,
        "CARDPUTER_TUNNEL_BEARER": BEARER,
    }
    assert PRIVATE not in str(result)
    assert source["MCP_COMMAND"] == "malicious-child"  # Do not mutate parent input.
    scoped = "Bearer cpk_" + "d" * 32 + "." + "e" * 64
    assert tunnel.child_environment({}, RUNTIME_KEY, scoped)["CARDPUTER_TUNNEL_BEARER"] == scoped


@pytest.mark.parametrize("key", ["", "has\nnewline", "x" * 513])
def test_invalid_runtime_key_is_rejected_without_echo(key):
    with pytest.raises(tunnel.TunnelError, match="^invalid_runtime_key$"):
        tunnel.child_environment({}, key, BEARER)


@pytest.mark.parametrize(
    "bearer",
    ["Bearer ", "Basic " + REMOTE_TOKEN, BEARER + "\n"],
)
def test_invalid_broker_bearer_is_rejected_without_echo(bearer):
    with pytest.raises(tunnel.TunnelError, match="^invalid_mcp_bearer$"):
        tunnel.child_environment({}, RUNTIME_KEY, bearer)


def test_arguments_keep_secrets_out_and_pin_loopback_main_route(settings):
    args = tunnel.client_arguments(settings, "http://127.0.0.1:18765/mcp")
    assert args[:2] == [str(settings.client_exe), "run"]
    assert RUNTIME_KEY not in str(args) and REMOTE_TOKEN not in str(args)
    assert "--control-plane.base-url=https://api.openai.com" in args
    assert "--control-plane.poll-channel=main" in args
    assert "--control-plane.api-key=env:CARDPUTER_TUNNEL_API_KEY" in args
    assert "--mcp.server-url=http://127.0.0.1:18765/mcp" in args
    assert "--mcp.extra-headers=Authorization: env:CARDPUTER_TUNNEL_BEARER" in args
    assert "--mcp.discovery-extra-headers=Authorization: env:CARDPUTER_TUNNEL_BEARER" in args
    assert "--log.http-raw-unsafe=false" in args
    assert "--allow-remote-ui=false" in args
    assert "--open-web-ui=false" in args
    assert "--harpoon.capture-payloads=false" in args
    assert not any(arg.startswith(("--config", "--profile", "--mcp.command")) for arg in args)


def preflight_fakes(monkeypatch, settings, *, backend="mock", port=18765, locked=False):
    monkeypatch.setattr(
        tunnel,
        "load_settings",
        lambda path: SimpleNamespace(backend=backend, port=port, tunnel=None),
    )
    monkeypatch.setattr(tunnel, "verify_release", lambda selected: None)
    monkeypatch.setattr(tunnel, "check_listener", lambda port: None)
    monkeypatch.setattr(tunnel, "lock_path", lambda: settings.client_exe.parent / "owned.lock")

    class Lock:
        def __init__(self, path, timeout):
            assert timeout == 0
            self.path = path

        def __enter__(self):
            if locked:
                raise Timeout(self.path)
            return self

        def __exit__(self, *args):
            pass

    monkeypatch.setattr(tunnel, "FileLock", Lock)


def test_preflight_serial_requires_explicit_opt_in(settings, monkeypatch):
    preflight_fakes(monkeypatch, settings, backend="serial")
    with pytest.raises(tunnel.TunnelError, match="^serial_tunnel_requires_explicit_enable_serial$"):
        tunnel.preflight(settings)
    assert tunnel.preflight(settings, enable_serial=True)["backend"] == "serial"


@pytest.mark.parametrize("refusal", ["lock", "port"])
def test_preflight_rejects_occupied_lock_without_starting_process(settings, monkeypatch, refusal):
    if refusal == "lock":
        preflight_fakes(monkeypatch, settings, locked=True)
        expected = "tunnel_launcher_already_running"
    else:
        preflight_fakes(monkeypatch, settings, port=settings.health_port)
        expected = "health_port_conflicts_with_broker"
    monkeypatch.setattr(tunnel.subprocess, "Popen", lambda *a, **kw: pytest.fail("started child"))
    with pytest.raises(tunnel.TunnelError, match=f"^{expected}$"):
        tunnel.preflight(settings)


def supervisor_fakes(monkeypatch, settings, *, http_error=False, keep_alive=False, status=200):
    preflight_fakes(monkeypatch, settings)
    captured = {}

    class Child:
        def __init__(self):
            self.polls = 0
            self.terminated = False
            self.waited = False

        def poll(self):
            self.polls += 1
            if self.terminated:
                return 0
            return None if http_error or keep_alive or self.polls == 1 else 0

        def terminate(self):
            self.terminated = True

        def kill(self):
            self.terminated = True

        def wait(self, timeout=None):
            self.waited = True
            return 0

    child = Child()

    def popen(args, **kwargs):
        captured.update(kwargs)
        captured["args"] = args
        captured["env_at_start"] = dict(kwargs["env"])
        return child

    class HTTP:
        def __init__(self, **kwargs):
            assert kwargs["trust_env"] is False
            assert kwargs["timeout"] <= 1

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def get(self, url):
            assert url.startswith("http://127.0.0.1:18766/")
            if http_error:
                raise RuntimeError(PRIVATE)
            return SimpleNamespace(
                status_code=status,
                text=PRIVATE,
                json=lambda: {
                    "schema_version": 1,
                    "component": "control-plane",
                    "status": "ok",
                    "state": "polling",
                    "details": {"last_success": "2026-10-02T10:00:00Z", "consecutive_failures": 0},
                },
            )

    monkeypatch.setattr(tunnel.subprocess, "Popen", popen)
    monkeypatch.setattr(tunnel.httpx2, "Client", HTTP)
    monkeypatch.setattr(tunnel.time, "sleep", lambda seconds: None)
    return child, captured


def test_supervisor_suppresses_native_output_and_clears_child_env(settings, monkeypatch, capsys):
    _, captured = supervisor_fakes(monkeypatch, settings)
    env = tunnel.child_environment({}, RUNTIME_KEY, BEARER)
    assert tunnel.supervise(settings, "http://127.0.0.1:18765/mcp", env) == 1
    assert env == {}
    for stream in ("stdin", "stdout", "stderr"):
        assert captured[stream] == subprocess.DEVNULL
    assert captured.get("shell", False) is False
    assert captured["cwd"] == settings.client_exe.parent
    assert "CARDPUTER_LOCAL_TOKEN" not in captured["env_at_start"]
    output = capsys.readouterr().out
    assert PRIVATE not in output and REMOTE_TOKEN not in output and RUNTIME_KEY not in output
    assert "tunnel_client_exited" in output
    assert "cloud" in output.lower() and "verif" in output.lower()


@pytest.mark.parametrize("interrupt", [False, True])
def test_supervisor_reaps_owned_child_on_unexpected_failure(settings, monkeypatch, interrupt):
    child, _ = supervisor_fakes(
        monkeypatch, settings, http_error=not interrupt, keep_alive=interrupt
    )
    if interrupt:

        def stop(seconds):
            raise KeyboardInterrupt

        monkeypatch.setattr(tunnel.time, "sleep", stop)
        assert tunnel.supervise(settings, "http://127.0.0.1:18765/mcp", {}) == 0
    else:
        with pytest.raises(RuntimeError, match=PRIVATE):
            tunnel.supervise(settings, "http://127.0.0.1:18765/mcp", {})
    assert child.terminated and child.waited


@pytest.mark.parametrize(("status", "deadline"), [(401, False), (503, True)])
def test_nonready_status_never_claims_ready_or_echoes_body(
    settings, monkeypatch, capsys, status, deadline
):
    supervisor_fakes(monkeypatch, settings, status=status)
    if deadline:
        ticks = iter([0, *([46] * 10)])
        monkeypatch.setattr(tunnel.time, "monotonic", lambda: next(ticks))
    assert tunnel.supervise(settings, "http://127.0.0.1:18765/mcp", {}) == 1
    output = capsys.readouterr().out
    assert "readiness_only" not in output and "reports_ready" not in output
    assert PRIVATE not in output
    if deadline:
        assert output.count("tunnel_readiness_not_confirmed") == 1


def test_main_reports_only_fixed_failure_code(monkeypatch, capsys, tmp_path):
    def fail(*args, **kwargs):
        raise ValueError(PRIVATE)

    monkeypatch.setattr(tunnel, "load_config", fail)
    monkeypatch.setattr(
        sys, "argv", ["tunnel", "preflight", "--project", str(tmp_path), "--config", "x.toml"]
    )
    with pytest.raises(SystemExit) as caught:
        tunnel.main()
    assert caught.value.code == 1
    output = capsys.readouterr().out
    assert json.loads(output) == {"ok": False, "error": "tunnel_preparation_failed"}
    assert PRIVATE not in output and str(tmp_path) not in output


@pytest.mark.parametrize(
    "url",
    [
        "https://example.invalid/mcp",
        "http://localhost:8765/mcp",
        "http://127.0.0.1:8765/mcp?secret=x",
    ],
)
async def test_broker_probe_rejects_noncanonical_url_before_auth_transmission(monkeypatch, url):
    @asynccontextmanager
    async def forbidden_connect(*args, **kwargs):
        pytest.fail("untrusted URL received a credential-bearing connection")
        yield

    monkeypatch.setattr(tunnel, "connect", forbidden_connect)
    with pytest.raises(ValueError):
        await tunnel.check_broker(url, BEARER)
