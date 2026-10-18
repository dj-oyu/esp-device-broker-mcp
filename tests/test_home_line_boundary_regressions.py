"""Legacy diagnostic grants/probe helpers cannot enable raw collection or USB."""

from types import SimpleNamespace

import pytest
from test_home_line_integration import SECRET, prepare, running
from test_serial_capture import device

from cardputer_mcp.home_line import HomeLineProbe
from cardputer_mcp.serial_backend import SerialFailure


def retained_probe():
    probe = HomeLineProbe(lambda: 10.0, 130.0)
    probe.begin_window()
    probe.observe_line(b"HOME_READY private=" + SECRET.encode() + b"\n")
    probe.end_window()
    probe.finish()
    assert SECRET in probe.preview()["escaped_line"]
    return probe


@pytest.mark.parametrize("policy_authorized", [False, True])
async def test_old_diagnostic_entry_is_erased_not_inherited_by_execution(
    tmp_path, policy_authorized
):
    async with running(tmp_path) as env:
        prepared = prepare(env)
        probe = retained_probe()
        env.broker.local_home_diagnostic = SimpleNamespace(probe=probe)
        result = env.broker._take_local_home_probe(
            env.broker._store().plan(prepared["plan_hash"]),
            env.broker.session,
            policy_authorized=policy_authorized,
        )
        assert result is None and env.broker.local_home_diagnostic is None
        assert "escaped_line" not in probe.preview()
        assert env.port.opens == 0 and env.backend.calls == 0


@pytest.mark.parametrize("test", ["capture_home", "hello_smoke"])
async def test_old_backend_probe_helper_clears_bytes_and_refuses_without_open(test):
    backend, port, plan = device()
    plan["test"] = test
    probe = retained_probe()
    with pytest.raises(SerialFailure, match="command_not_allowlisted"):
        await backend.execute_with_local_probe(plan, lambda _: None, probe)
    assert port.opens == 0 and not port.writes
    assert backend.active == 0 and backend.calls == 0
    assert "escaped_line" not in probe.preview()
