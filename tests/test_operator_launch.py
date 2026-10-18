"""Approval relay modules expose no executable slots or credential handling."""
import importlib

import pytest


@pytest.mark.parametrize("module", ["operator_launch", "derby_operator_launch", "operator_cli", "gui_approvals", "gui_policies", "gui_plan_badge"])
def test_retired_surface_has_no_runtime_approval_or_launch_objects(module):
    loaded = importlib.import_module("cardputer_mcp." + module)
    public = [name for name in vars(loaded) if not name.startswith("_")]
    assert public == []
