"""Static PowerShell contracts only; these do not validate a native Windows run.

Never run a tunnel client, obtain credentials, contact an account, or open USB.
"""

import base64
import re
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest

from cardputer_mcp import tunnel

PROJECT = Path(__file__).resolve().parents[1]
TUNNEL = PROJECT / "Start-CardputerTunnel.ps1"
BROKER = PROJECT / "Start-Cardputer.ps1"


def test_windows_scripts_remain_ascii_powershell_51_sources():
    for script in (TUNNEL, BROKER):
        source = script.read_text()
        assert source.isascii()  # Avoid PS 5.1's BOM-less UTF-8 decoding mismatch.
        assert source.startswith("#requires -Version 5.1\n")
        assert "[PlatformID]::Win32NT" in source
        assert "$PSVersionTable.PSEdition -eq 'Core'" in source
        assert "Join-Path $PSHOME $shellName" in source
        assert "Resolve-Path -LiteralPath $PSScriptRoot" in source


def test_default_tunnel_preparation_is_authenticated_nonexecuting_mock():
    source = TUNNEL.read_text()
    config = tomllib.loads((PROJECT / "examples/tunnel-mock.toml").read_text())
    assert config["backend"] == "mock"
    assert config["allow_mock_execution"] is False
    assert config["host"] == "127.0.0.1"
    before_connect = source.split("if ($Connect) {", 1)[0]
    assert "$backend = 'mock'" in before_connect
    assert "examples\\tunnel-mock.toml" in before_connect
    assert "if ($backend -eq 'mock') { $brokerArgs.Mock = $true }" in source
    assert "& (Join-Path $projectRoot 'Start-Cardputer.ps1') @brokerArgs" in source
    assert "if ($Mock) { ' --require-auth; ' }" in BROKER.read_text()
    default_branch = source.split("if (!$Connect) {", 1)[1].split("\n}", 1)[0]
    assert "cardputer_mcp.tunnel check" in default_branch
    assert "--config $brokerConfig --url $url" in default_branch
    assert "if ($LASTEXITCODE -ne 0)" in default_branch
    assert "return" in default_branch
    assert "Read-Host" not in default_branch
    assert source.index("if (!$Connect) {") < source.index("Read-Host")


def test_serial_and_client_key_modes_require_explicit_flags():
    source = TUNNEL.read_text()
    assert "if ($EnableSerial -and !$Connect)" in source
    gate = "if ($UseClientKey -and (!$Connect -or [string]::IsNullOrWhiteSpace($KeyRegistry)))"
    assert gate in source
    assert "if ($EnableSerial) { $preflightArgs += '--enable-serial' }" in source
    assert "if ($KeyRegistry) { $brokerArgs.KeyRegistry = $KeyRegistry }" in source
    assert "cardputer keys issue" not in source
    assert "cardputer keys rotate" not in source
    assert "cardputer ports" not in source


def test_tokens_remain_process_scoped_after_nested_broker_invocation():
    source = BROKER.read_text()
    for name in ("Local", "Remote"):
        assert (
            f"SetEnvironmentVariable('CARDPUTER_{name.upper()}_TOKEN', $new{name}, 'Process')"
            in source
        )
    assert "if ($tokensChanged -and !$ready)" in source
    assert source.index("$check = Invoke-Probe 'preflight'") < source.index(
        "$newLocal = New-RunToken"
    )
    launcher = TUNNEL.read_text()
    assert "SetEnvironmentVariable" not in launcher
    assert "GetEnvironmentVariable('CARDPUTER_REMOTE_TOKEN', 'Process')" in launcher
    assert "GetEnvironmentVariable('CARDPUTER_LOCAL_TOKEN'" not in launcher


def test_tunnel_child_environment_is_cleared_before_allowlisted_copy():
    source = TUNNEL.read_text()
    loop = re.search(r"foreach \(\$name in @\(([^)]+)\)\)", source)
    assert loop is not None
    names = set(re.findall(r"'([^']+)'", loop.group(1)))
    assert names == {"SystemRoot", "WINDIR", "SystemDrive", "TEMP", "TMP", "COMSPEC"}
    assert source.index("$info.EnvironmentVariables.Clear()") < loop.start()
    assert "GetEnvironmentVariable($name, 'Process')" in source
    assert source.count("$info.EnvironmentVariables.Clear()") >= 2
    assert "EnvironmentVariables['CARDPUTER_LOCAL_TOKEN']" not in source
    assert "EnvironmentVariables['OPENAI_API_KEY']" not in source
    assert "EnvironmentVariables['OPENAI_ADMIN_KEY']" not in source


def test_runtime_secrets_are_prompted_privately_and_cleaned_up():
    source = TUNNEL.read_text()
    assert "Read-Host 'Tunnel runtime API key' -AsSecureString" in source
    assert "Read-Host 'Previously issued Cardputer client key' -AsSecureString" in source
    assert "SecureStringToBSTR($secureApi)" in source
    assert "SecureStringToBSTR($secureMcp)" in source
    assert "ZeroFreeBSTR($apiPointer)" in source
    assert "ZeroFreeBSTR($mcpPointer)" in source
    assert "$secureApi.Dispose()" in source
    assert "$secureMcp.Dispose()" in source
    assert "Set-Content" not in source and "Out-File" not in source
    assert "setx" not in source and "Stop-Process" not in source


def test_bootstrap_arguments_are_quoted_and_contain_no_secrets():
    source = TUNNEL.read_text()
    assert "return '\"' + $Value + '\"'" in source
    arguments = source.split("$info.Arguments =", 1)[1].split("$info.WorkingDirectory", 1)[0]
    assert "Quote-TunnelArgument $projectRoot" in arguments
    assert "Quote-TunnelArgument $configPath" in arguments
    assert "Quote-TunnelArgument $shellPath" in arguments
    assert "TOKEN" not in arguments and "API_KEY" not in arguments
    assert "secureApi" not in arguments and "secureMcp" not in arguments
    assert "$info.FileName = $pythonPath" in source
    assert "$info.CreateNoWindow = $true" in source
    assert "$process.WaitForExit(10000)" in source


@pytest.mark.parametrize("enable_serial", [False, True])
def test_console_bootstrap_uses_new_console_and_secret_free_quoted_command(
    tmp_path, monkeypatch, enable_serial
):
    project = tmp_path / "user's project with spaces"
    config = project / "operator's tunnel config.toml"
    python = project / ".venv" / "Scripts" / "python.exe"
    shell = tmp_path / "shell's install" / "pwsh.exe"
    shell.parent.mkdir()
    shell.write_text("synthetic fixture; never execute")
    api_key = "SYNTHETIC_TEST_RUNTIME_KEY"
    remote = "a" * 64
    local = "b" * 64
    environment = {
        "SystemRoot": "C:\\Windows",
        "CARDPUTER_TUNNEL_API_KEY": api_key,
        "CARDPUTER_TUNNEL_BEARER": "Bearer " + remote,
        "CARDPUTER_LOCAL_TOKEN": local,
        "OPENAI_ADMIN_KEY": "SYNTHETIC_DO_NOT_INHERIT",
        "PYTHONPATH": "SYNTHETIC_DO_NOT_INHERIT",
    }
    captured = {}

    def popen(arguments, **kwargs):
        captured.update(kwargs)
        captured["arguments"] = arguments
        captured["env_snapshot"] = dict(kwargs["env"])
        return SimpleNamespace()

    monkeypatch.setattr(tunnel, "os", SimpleNamespace(name="nt", environ=environment))
    monkeypatch.setattr(tunnel, "sys", SimpleNamespace(executable=str(python)))
    monkeypatch.setattr(tunnel.subprocess, "Popen", popen)
    tunnel.launch_console(config, project, shell, enable_serial=enable_serial)
    assert captured["creationflags"] == 0x00000010  # Windows CREATE_NEW_CONSOLE
    assert captured["cwd"] == project
    assert captured.get("shell", False) is False
    # No redirection is important: the new console owns its I/O, not the hidden
    # bootstrap's captured pipes, which must close when that bootstrap exits.
    for stream in ("stdin", "stdout", "stderr"):
        assert captured.get(stream) is None
    assert captured["arguments"][:-1] == [
        str(shell),
        "-NoLogo",
        "-NoProfile",
        "-NoExit",
        "-EncodedCommand",
    ]
    command = base64.b64decode(captured["arguments"][-1]).decode("utf-16le")
    for path in (project, config, python):
        assert "'" + str(path).replace("'", "''") + "'" in command
    assert (" --enable-serial" in command) is enable_serial
    for secret in (api_key, remote, local, "SYNTHETIC_DO_NOT_INHERIT"):
        assert secret not in command
    assert captured["env_snapshot"] == {
        "SystemRoot": "C:\\Windows",
        "CARDPUTER_TUNNEL_API_KEY": api_key,
        "CARDPUTER_TUNNEL_BEARER": "Bearer " + remote,
    }
    assert captured["env"] == {}
    assert "CARDPUTER_TUNNEL_API_KEY" not in environment
    assert "CARDPUTER_TUNNEL_BEARER" not in environment
    assert environment["CARDPUTER_LOCAL_TOKEN"] == local
