"""Retired HOME approval launchers cannot execute or carry credentials."""

from cardputer_mcp import home_recovery_launch


def test_legacy_home_launch_has_no_action_or_schema():
    for name in ("HomeRecoveryLaunchScope", "HomeRecoveryLaunchRequest", "HomeRecoveryLaunchSlot"):
        assert not hasattr(home_recovery_launch, name)
