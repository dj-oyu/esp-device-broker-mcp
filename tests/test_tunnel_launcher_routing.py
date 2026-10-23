"""Static PowerShell safeguards, not native Windows execution claims."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_tunnel_launcher_has_no_gui_or_legacy_config_routes():
    source = (ROOT / "Start-CardputerTunnel.ps1").read_text()
    # The GUI has its own launcher; the console launcher is exe + profile only.
    for removed in ("$Gui", "-Gui", "$Config", "tunnel.local.toml", "device.mock.local.toml"):
        assert removed not in source
    assert "Start-Cardputer.ps1" in source and "launch-console" in source


def test_gui_launcher_starts_one_broker_then_the_manager_from_the_same_exe_and_profile():
    source = (ROOT / "Start-CardputerGui.ps1").read_text()
    assert source.isascii()
    assert "Start-Cardputer.ps1" in source
    assert "& $exePath --auth-source environment gui --config $profilePath" in source
    assert source.index("Start-Cardputer.ps1") < source.index("--auth-source environment gui")
    assert "Url = $Url" in source and "if ($NoSerial)" in source
    # No private mock copy, checkout/venv or credential handling any more.
    for forbidden in ("CreateNew", "device.mock.local.toml", ".venv", "TOKEN", "Stop-Process"):
        assert forbidden not in source
    assert "Tunnel stays disconnected until you click Connect" in source
