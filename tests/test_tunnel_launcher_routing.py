"""Static PowerShell safeguards, not native Windows execution claims."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_gui_routes_to_owned_manager_before_legacy_console_launch():
    source = (ROOT / "Start-CardputerTunnel.ps1").read_text()
    branch = source.split("if ($Gui) {", 1)[1].split("if (!$Config)", 1)[0]
    assert "Start-CardputerGui.ps1" in branch and "return" in branch
    assert "SecureStringToBSTR" not in branch and "launch-console" not in branch
    assert source.index("if ($Gui)") < source.index("$secureApi =")
    assert "No automatic cloud connection" in branch


def test_gui_starts_with_private_generic_mock_copy_only_when_absent():
    source = (ROOT / "Start-CardputerGui.ps1").read_text()
    assert "'device.mock.local.toml'" in source
    assert "if (!(Test-Path -LiteralPath $privateMock))" in source
    assert "[IO.FileMode]::CreateNew" in source and "-Force" not in source
    assert source.index("Win32NT") < source.index("[IO.File]::OpenRead")
    assert "CARDPUTER_LOCAL_TOKEN" not in source and "CARDPUTER_REMOTE_TOKEN" not in source
    assert (
        "Start-Cardputer.ps1" in source
        and "--auth-source environment gui --config $Config" in source
    )


def test_console_preserves_legacy_metadata_source_fallback():
    source = (ROOT / "Start-CardputerTunnel.ps1").read_text()
    assert "$Config = 'device.mock.local.toml'" in source
    assert "$Config = 'tunnel.local.toml'" in source
    assert "tunnel preflight" not in source.lower() or "preflight refused" in source.lower()
