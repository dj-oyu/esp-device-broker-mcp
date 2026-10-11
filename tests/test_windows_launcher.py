"""Tests embedded launcher probe logic, not native PowerShell/Windows execution."""

import asyncio
import importlib.util
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from filelock import Timeout
from test_serial_http import serial_service  # noqa: F401 - imported pytest fixture

SCRIPT = Path(__file__).resolve().parents[1] / "Start-Cardputer.ps1"


@pytest.fixture
def probe(tmp_path):
    text = SCRIPT.read_text()
    source = text.split("$probeSource = @'\n", 1)[1].split("\n'@", 1)[0]
    file = tmp_path / "launcher_probe.py"
    file.write_text(source)
    spec = importlib.util.spec_from_file_location("launcher_probe", file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def settings(tmp_path):
    return SimpleNamespace(backend="serial", host="127.0.0.1", port=9876, state_dir=tmp_path)


def setup_preflight(probe, monkeypatch, *, listening=False, locked=False):
    seen = []

    class Socket:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def settimeout(self, timeout):
            assert timeout <= 0.3

        def connect_ex(self, target):
            seen.append(("connect", target))
            return 0 if listening else 111

    class Lock:
        def __init__(self, path, timeout):
            assert timeout == 0
            self.path = path

        def __enter__(self):
            if locked:
                raise Timeout(self.path)
            seen.append(("acquire", self.path))
            return self

        def __exit__(self, *args):
            seen.append(("release", self.path))

    monkeypatch.setattr(probe, "socket", SimpleNamespace(socket=Socket))
    monkeypatch.setattr(probe, "FileLock", Lock)
    monkeypatch.setattr(
        probe, "device_lock_path", lambda backend: Path("synthetic-device.lockfile")
    )
    return seen


def test_preflight_refuses_existing_listener_without_locking(probe, settings, monkeypatch):
    seen = setup_preflight(probe, monkeypatch, listening=True)
    assert probe.preflight(settings) == {"ok": False, "reason": "listener_exists"}
    assert [item[0] for item in seen] == ["connect"]


def test_preflight_refuses_existing_broker_lock(probe, settings, monkeypatch):
    setup_preflight(probe, monkeypatch, locked=True)
    assert probe.preflight(settings) == {"ok": False, "reason": "broker_locked"}


def test_preflight_checks_global_and_state_locks(probe, settings, monkeypatch):
    seen = setup_preflight(probe, monkeypatch)
    assert probe.preflight(settings) == {"ok": True, "url": "http://127.0.0.1:9876/mcp"}
    assert sum(item[0] == "acquire" for item in seen) == 2
    assert sum(item[0] == "release" for item in seen) == 2


def test_preflight_handles_new_state_directory(probe, settings, monkeypatch):
    settings.state_dir /= "not-created"
    seen = setup_preflight(probe, monkeypatch)
    assert probe.preflight(settings)["ok"]
    assert not settings.state_dir.exists()
    assert sum(item[0] == "acquire" for item in seen) == 1


def test_preflight_needs_serial_configuration(probe, settings):
    settings.backend = "mock"
    assert probe.preflight(settings) == {"ok": False, "reason": "serial_config_required"}


def setup_status(probe, monkeypatch, status):
    calls = []

    @asynccontextmanager
    async def connect(url, token):
        assert token == "SYNTHETIC-NOT-A-REAL-TOKEN"
        calls.append(url)
        yield object()

    async def call(client, tool):
        assert tool == "device_status"  # No acquire, prepare, execute or hardware action.
        return status

    monkeypatch.setattr(probe, "connect", connect)
    monkeypatch.setattr(probe, "call", call)
    monkeypatch.setattr(
        probe, "load_environment_token", lambda origin: "SYNTHETIC-NOT-A-REAL-TOKEN"
    )
    return calls


@pytest.mark.parametrize(
    "change",
    [
        {"ok": False},
        {"backend": "mock"},
        {"active_job": "synthetic-job"},
        {"owner": "local_cli"},
        {"hardware_connected": True},
    ],
)
async def test_readiness_requires_authenticated_idle_serial_broker(
    probe, settings, monkeypatch, change
):
    status = {"ok": True, "backend": "serial", "hardware_connected": False, **change}
    setup_status(probe, monkeypatch, status)
    result = await probe.readiness(settings, 2)
    assert not result["ok"]


async def test_readiness_reports_review_without_claiming_a_device_test(
    probe, settings, monkeypatch
):
    setup_status(probe, monkeypatch, {"ok": True, "backend": "serial", "needs_review": True})
    assert await probe.readiness(settings, 2) == {"ok": True, "needs_review": True}


async def test_readiness_deadline_is_bounded(probe, settings, monkeypatch):
    setup_status(probe, monkeypatch, {})
    assert await probe.readiness(settings, 0) == {"ok": False, "reason": "readiness_timeout"}


def test_probe_errors_do_not_echo_secret_or_private_path(probe, settings, monkeypatch, capsys):
    def fail(path):
        raise ValueError("SYNTHETIC_SECRET private/path")

    monkeypatch.setattr(probe, "load_settings", fail)
    monkeypatch.setattr(sys, "argv", ["probe", "preflight", "synthetic.toml", "10"])
    probe.main()
    output = capsys.readouterr().out
    assert "SYNTHETIC_SECRET" not in output and "private/path" not in output
    assert "probe_failed_check_config_and_uv_sync" in output


async def test_launcher_probe_authenticates_over_real_http_without_opening_usb(
    probe,
    serial_service,  # noqa: F811 - imported pytest fixture
    monkeypatch,
):
    _, resident, port = serial_service
    monkeypatch.setattr(probe, "load_environment_token", lambda origin: "a" * 64)
    result = await asyncio.wait_for(probe.readiness(resident.settings, 3), timeout=5)
    assert result == {"ok": True, "needs_review": False}
    assert port.opens == 0 and port.writes == []


def test_launcher_structure_preserves_process_and_secret_boundaries():
    source = SCRIPT.read_text()
    assert source.isascii()  # Windows PowerShell 5.1 does not require UTF-8 BOM here.
    assert "$PSScriptRoot" in source and "WorkingDirectory = $projectRoot" in source
    assert "-NoExit -EncodedCommand" in source
    assert "[Text.Encoding]::Unicode.GetBytes($serverCommand)" in source
    assert "@(Get-Command -Name uv" in source and "$uvMatches[0].Path" in source
    assert source.index("$check = Invoke-Probe 'preflight'") < source.index(
        "$newLocal = New-RunToken"
    )
    command = source.split("$serverCommand =", 1)[1].split("$encoded =", 1)[0]
    assert "TOKEN" not in command and "$newLocal" not in command and "$newRemote" not in command
    assert "'Process'" in source and "'Machine'" not in source and "'User'" not in source
    assert "Stop-Process" not in source and "taskkill" not in source and "setx" not in source
    assert "$probe.Kill()" in source  # Only the bounded read-only probe's own handle.
    assert "if ($tokensChanged -and !$ready)" in source
    assert "SetEnvironmentVariable('CARDPUTER_LOCAL_TOKEN', $previousLocal" in source
    assert "SetEnvironmentVariable('CARDPUTER_REMOTE_TOKEN', $previousRemote" in source
