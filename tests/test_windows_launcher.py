"""Static PowerShell contracts for Start-Cardputer.ps1, not native Windows execution."""

from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "Start-Cardputer.ps1"


def source():
    return SCRIPT.read_text()


def test_existing_listener_is_refused_before_any_credential_exists():
    text = source()
    assert "Net.Sockets.TcpClient" in text and "WaitOne(300)" in text
    assert "listener_exists" in text
    assert text.index("if (Test-Listener) {") < text.index("$newLocal = New-RunToken")
    assert "broker.lockfile" not in text and "FileLock" not in text  # serve enforces its own lock


def test_secret_and_process_boundaries_are_preserved():
    text = source()
    command = text.split("$serverCommand =", 1)[1].split("$encoded =", 1)[0]
    assert "TOKEN" not in command and "$newLocal" not in command and "$newRemote" not in command
    assert "'Process'" in text and "'Machine'" not in text and "'User'" not in text
    assert "Stop-Process" not in text and "taskkill" not in text and "setx" not in text
    assert "$probe.Kill()" in text  # Only the bounded read-only status call's own handle.
    assert "if ($tokensChanged -and !$ready)" in text
    assert "broker_not_idle" in text and "readiness_timeout" in text
    assert "SetEnvironmentVariable('CARDPUTER_LOCAL_TOKEN', $previousLocal" in text
    assert "SetEnvironmentVariable('CARDPUTER_REMOTE_TOKEN', $previousRemote" in text
