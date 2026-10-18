"""Retired relay returns without opening files, pipes or launching processes."""

import builtins
import subprocess

from cardputer_mcp.operator_launch_pipe import request_operator_launch


def test_old_launch_request_never_uses_os_or_turns_into_execution(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("No file, pipe or process is permitted")

    monkeypatch.setattr(builtins, "open", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    for payload in (
        {},
        {"action": "approve-and-start", "plan_hash": "a" * 64},
        {"action": "derby-image-decision"},
    ):
        assert request_operator_launch("legacy-pipe", payload) == {
            "ok": False,
            "error": "product_approval_retired",
        }
