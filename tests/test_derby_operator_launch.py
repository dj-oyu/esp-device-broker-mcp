"""Legacy DERBY approval launcher has no executable or credential surface."""

import cardputer_mcp.derby_operator_launch as retired


def test_derby_approval_launcher_is_retired():
    assert not hasattr(retired, "DerbyLaunchScope")
    assert not hasattr(retired, "DerbyOperatorLaunchSlot")
    assert not hasattr(retired, "HOME_ATTESTATION")
