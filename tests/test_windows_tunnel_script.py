"""Static PowerShell contracts only; these do not validate a native Windows run.

Never run a tunnel client, obtain credentials, contact an account, or open USB.
"""

import base64
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from cardputer_mcp import tunnel

PROJECT = Path(__file__).resolve().parents[1]
TUNNEL = PROJECT / "Start-CardputerTunnel.ps1"
BROKER = PROJECT / "Start-Cardputer.ps1"
GUI = PROJECT / "Start-CardputerGui.ps1"


def test_windows_scripts_remain_ascii_powershell_51_sources():
    for script in (TUNNEL, BROKER, GUI):
        source = script.read_text()
        assert source.isascii()  # Avoid PS 5.1's BOM-less UTF-8 decoding mismatch.
        assert source.startswith("#requires -Version 5.1\n")
        assert "[PlatformID]::Win32NT" in source
        assert "[string]$Exe = ''" in source and "[string]$Profile = ''" in source
        assert "[switch]$NoSerial" in source
        assert "[string]$Url = 'http://127.0.0.1:8765/mcp'" in source
        assert "launch-probe" not in source
        assert ".venv" not in source and "python.exe" not in source and "uv run" not in source
    for script in (TUNNEL, BROKER):
        source = script.read_text()
        assert "$PSVersionTable.PSEdition -eq 'Core'" in source
        assert "Join-Path $PSHOME $shellName" in source


def test_scripts_never_parse_the_profile_or_branch_on_its_contents():
    for script in (TUNNEL, BROKER, GUI):
        source = script.read_text()
        for forbidden in ("Get-Content", "ConvertFrom-Toml", "tomllib", "Select-String"):
            assert forbidden not in source
    source = TUNNEL.read_text()
    assert "tunnel_configured" not in source and "$check.backend" not in source
    for field in ("tunnel_id", "client_exe", "client_archive", "health_port", "ingress_enabled"):
        assert f"--{field}" not in source and f"-{field}" not in source.replace("tunnel_id", "")


def test_tunnel_and_serial_are_default_with_explicit_opt_outs():
    source = TUNNEL.read_text()
    assert "[switch]$NoTunnel" in source
    assert "$serialArgument = if (!$NoSerial) { ' --enable-serial' } else { '' }" in source
    assert "if (!$NoTunnel) {" in source and "if ($NoTunnel) {" in source
    assert "if ($NoSerial) { $brokerArgs.NoSerial = $true }" in source
    assert "-Connect" not in source.replace("click Connect", "")
    refusal = source.split("Tunnel preflight refused", 1)[1][:400]
    assert "-NoTunnel" in refusal and "-NoSerial" in refusal
    preflight = "'-m cardputer_mcp.tunnel preflight' + $tunnelSuffix + $serialArgument"
    assert preflight in source
    # Tunnel preflight (which can refuse) must precede the broker, then the prompt and bootstrap.
    broker = source.index("& (Join-Path $PSScriptRoot 'Start-Cardputer.ps1')")
    assert source.index("tunnel preflight") < broker < source.index("Read-Host")


def test_url_is_passed_to_the_broker_launcher_and_checked_against_the_exe_reported_url():
    source = TUNNEL.read_text()
    assert "$brokerArgs = @{ Exe = $exePath; Profile = $profilePath; Url = $Url }" in source
    mismatch = source.split("$tunnelCheck.url -ne $Url", 1)[1][:260]
    assert "Rerun with -Url" in mismatch and "Nothing was started" in mismatch
    # With -NoTunnel nothing depends on a Tunnel preflight result.
    assert source.index("if (!$NoTunnel) {") < source.index("$tunnelCheck.url")


def test_api_key_prompt_is_only_reachable_when_the_tunnel_starts():
    source = TUNNEL.read_text()
    skip = source.split("if ($NoTunnel) {", 1)[1].split("}", 1)[0]
    assert "return" in skip and "Read-Host" not in skip
    assert source.index("if ($NoTunnel) {") < source.index("Read-Host")
    assert "CARDPUTER_TUNNEL_API_KEY" in source and "CONTROL_PLANE_API_KEY" in source


def test_tokens_remain_process_scoped_after_nested_broker_invocation():
    source = BROKER.read_text()
    for name in ("Local", "Remote"):
        assert (
            f"SetEnvironmentVariable('CARDPUTER_{name.upper()}_TOKEN', $new{name}, 'Process')"
            in source
        )
    assert "if ($tokensChanged -and !$ready)" in source
    assert source.index("if (Test-Listener) {") < source.index("$newLocal = New-RunToken")
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


def test_client_key_mode_requires_a_registry():
    source = TUNNEL.read_text()
    assert "if ($UseClientKey -and [string]::IsNullOrWhiteSpace($KeyRegistry))" in source
    assert "if ($KeyRegistry) { $brokerArgs.KeyRegistry = $KeyRegistry }" in source
    for forbidden in ("cardputer keys issue", "cardputer keys rotate", "cardputer ports"):
        assert forbidden not in source


def test_bootstrap_arguments_are_quoted_and_contain_no_secrets():
    source = TUNNEL.read_text()
    assert "return '\"' + $Value + '\"'" in source
    suffix = source.split("$tunnelSuffix =", 1)[1].split("if (!$NoTunnel)", 1)[0]
    assert "Quote-TunnelArgument $profileDirectory" in suffix
    assert "Quote-TunnelArgument $profilePath" in suffix
    arguments = source.split("$info.Arguments =", 1)[1].split("$info.WorkingDirectory", 1)[0]
    assert "launch-console" in arguments and "Quote-TunnelArgument $shellPath" in arguments
    assert "$tunnelSuffix" in arguments
    assert "TOKEN" not in arguments and "API_KEY" not in arguments
    assert "secureApi" not in arguments and "secureMcp" not in arguments
    assert "$info.FileName = $exePath" in source
    assert "$info.CreateNoWindow = $true" in source
    assert "$process.WaitForExit(10000)" in source


def test_gui_launcher_uses_the_same_exe_profile_pair():
    source = GUI.read_text()
    assert "Start-Cardputer.ps1" in source
    assert "--auth-source environment gui --config $profilePath" in source
    assert "TOKEN" not in source and "Stop-Process" not in source
    assert "if ($NoSerial) { $arguments.NoSerial = $true }" in source


@pytest.mark.parametrize("enable_serial", [False, True])
def test_console_bootstrap_uses_new_console_and_secret_free_quoted_command(
    tmp_path, monkeypatch, enable_serial
):
    project = tmp_path / "user's project with spaces"
    config = project / "operator's tunnel config.toml"
    python = project / "cardputer.exe"
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
