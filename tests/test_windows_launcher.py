"""Static PowerShell contracts for Start-Cardputer.ps1, not native Windows execution."""

from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "Start-Cardputer.ps1"


def source():
    return SCRIPT.read_text()


def test_launcher_takes_exe_and_profile_and_needs_no_python_environment():
    text = source()
    assert text.isascii()  # Windows PowerShell 5.1 does not require UTF-8 BOM here.
    assert text.startswith("#requires -Version 5.1\n")
    assert "[string]$Exe = ''" in text and "[string]$Profile = ''" in text
    assert "Both -Exe (path to cardputer.exe) and -Profile" in text
    assert "[switch]$NoSerial" in text and "EnableSerial" not in text
    for forbidden in (".venv", "python.exe", "uv run", "Get-Command -Name uv", "$PSScriptRoot"):
        assert forbidden not in text
    assert "launch-probe" not in text and "$probeSource" not in text
    assert "-ne '.exe'" in text and "-ne '.toml'" in text


def test_launcher_never_parses_the_profile():
    text = source()
    for forbidden in ("Get-Content", "ConvertFrom-Toml", "tomllib", "Select-String", "-match"):
        assert forbidden not in text
    # The exe alone loads the profile and refuses with its own reason.
    assert "serve --config ' + (Quote-PowerShellLiteral $profilePath)" in text


def test_url_defaults_to_the_exe_client_default_and_must_be_loopback_http():
    text = source()
    assert "[string]$Url = 'http://127.0.0.1:8765/mcp'" in text
    assert "$brokerUri.Scheme -ne 'http'" in text
    assert "@('127.0.0.1', 'localhost') -notcontains $brokerUri.Host" in text
    assert "-Url must be a loopback http URL" in text


def test_existing_listener_is_refused_before_any_credential_exists():
    text = source()
    assert "Net.Sockets.TcpClient" in text and "WaitOne(300)" in text
    assert "listener_exists" in text
    assert text.index("if (Test-Listener) {") < text.index("$newLocal = New-RunToken")
    assert "broker.lockfile" not in text and "FileLock" not in text  # serve enforces its own lock


def test_serial_is_passed_by_default_and_omitted_only_by_no_serial():
    text = source()
    assert text.count("$(if (!$NoSerial) { ' --enable-serial' } else { '' })") == 1
    assert "' --require-auth'" in text
    refusal = text.split("Authenticated startup not confirmed", 1)[1][:420]
    assert "serial_runtime_opt_in_required" in refusal and "-NoSerial" in refusal
    assert "server console" in refusal and "-Url" in refusal


def test_readiness_uses_existing_authenticated_status_command_and_checks_idle():
    text = source()
    assert "' --auth-source environment status'" in text
    assert "'--url ' + (Quote-FileArgument $Url)" in text
    assert "$status.ok -eq $true -and $status.backend" in text
    assert "$status.active_job -or $status.owner -or $status.hardware_connected" in text
    assert "broker_not_idle" in text and "readiness_timeout" in text
    assert "$status.needs_review" in text and "Write-Warning" in text
    # Only read-only client commands: no acquire/prepare/execute/flash.
    for forbidden in ("acquire", "prepare", "execute", "flash", " release"):
        assert (
            forbidden
            not in text.split("function Get-BrokerStatus", 1)[1].split("$tokensChanged", 1)[0]
        )


def test_broker_runs_from_the_profile_folder_with_the_exe():
    text = source()
    assert "$profileDirectory = [IO.Path]::GetDirectoryName($profilePath)" in text
    assert "WorkingDirectory = $profileDirectory" in text
    assert "Set-Location -LiteralPath ' +" in text
    assert "(Quote-PowerShellLiteral $exePath) +" in text
    assert "-NoExit -EncodedCommand" in text
    assert "[Text.Encoding]::Unicode.GetBytes($serverCommand)" in text


def test_secret_and_process_boundaries_are_preserved():
    text = source()
    command = text.split("$serverCommand =", 1)[1].split("$encoded =", 1)[0]
    assert "TOKEN" not in command and "$newLocal" not in command and "$newRemote" not in command
    assert "'Process'" in text and "'Machine'" not in text and "'User'" not in text
    assert "Stop-Process" not in text and "taskkill" not in text and "setx" not in text
    assert "$probe.Kill()" in text  # Only the bounded read-only status call's own handle.
    assert "if ($tokensChanged -and !$ready)" in text
    assert "SetEnvironmentVariable('CARDPUTER_LOCAL_TOKEN', $previousLocal" in text
    assert "SetEnvironmentVariable('CARDPUTER_REMOTE_TOKEN', $previousRemote" in text
