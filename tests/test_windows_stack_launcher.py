"""Static PowerShell contracts for Start-CardputerStack.ps1, not native Windows execution."""

from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "Start-CardputerStack.ps1"


def source():
    return SCRIPT.read_text()


def test_order_is_broker_then_gateway_then_gui():
    text = source()
    broker = text.index("Start-Cardputer.ps1') @arguments")
    gateway = text.index("Start-Process")
    gui = text.index("--auth-source environment gui --config")
    assert broker < gateway < gui


def test_gateway_console_gets_paths_only_and_drops_secrets():
    text = source()
    command = text.split("$gatewayCommand =", 1)[1].split("$encoded =", 1)[0]
    assert "TOKEN" not in command and "API_KEY" not in command
    assert "Remove-Item -LiteralPath (''Env:'' + $_.Name)" in command
    assert "''CARDPUTER_*''" in command and "''OPENAI_*''" in command
    # Secrets are removed before the gateway starts.
    assert command.index("Remove-Item") < command.index("gateway --config")
    assert "--auth-source" not in command


def test_nothing_is_stopped_and_no_persistent_environment_is_written():
    text = source()
    assert "Stop-Process" not in text and "taskkill" not in text and "setx" not in text
    assert "'User'" not in text and "'Machine'" not in text
    assert "SetEnvironmentVariable" not in text


def test_same_arguments_as_the_gui_launcher():
    gui = (SCRIPT.parent / "Start-CardputerGui.ps1").read_text()
    params = lambda text: text.split("param(", 1)[1].split(")\n", 1)[0]  # noqa: E731
    assert params(source()) == params(gui)
