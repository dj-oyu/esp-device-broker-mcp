"""Real same-user Windows pipes with fake CLI children; never user tokens or USB."""

import asyncio
import json
import os
from unittest.mock import Mock

import pytest
from test_operator_launch import LOCAL, request
from test_operator_launch import armed as armed

from cardputer_mcp import cli
from cardputer_mcp.operator_launch import OperatorLaunchError
from cardputer_mcp.operator_launch_pipe import (
    WindowsOperatorLaunchPipe,
    current_user_sid,
    request_operator_launch,
)


async def exchange(endpoint, payload):
    task = asyncio.create_task(asyncio.to_thread(request_operator_launch, endpoint.name, payload))
    try:
        async with asyncio.timeout(12):
            while not task.done():
                await asyncio.to_thread(endpoint.poll)
                await asyncio.sleep(0.01)
        return await task
    finally:
        if not task.done():
            endpoint.close()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.skipif(os.name != "nt", reason="Windows kernel pipe security")
async def test_real_pipe_has_explicit_owner_only_dacl_and_one_capture_lifecycle(armed):
    import win32security

    armed.slot.owner_sid = current_user_sid()
    endpoint = WindowsOperatorLaunchPipe(armed.slot)
    try:
        descriptor = win32security.GetSecurityInfo(
            endpoint._handle,
            win32security.SE_KERNEL_OBJECT,
            win32security.OWNER_SECURITY_INFORMATION | win32security.DACL_SECURITY_INFORMATION,
        )
        assert win32security.ConvertSidToStringSid(descriptor.GetSecurityDescriptorOwner()) == (
            armed.slot.owner_sid
        )
        acl = descriptor.GetSecurityDescriptorDacl()
        assert descriptor.GetSecurityDescriptorControl()[0] & 0x1000  # protected DACL
        assert acl is not None and acl.GetAceCount() == 1
        header, rights, sid = acl.GetAce(0)
        assert header[0] == win32security.ACCESS_ALLOWED_ACE_TYPE
        assert win32security.ConvertSidToStringSid(sid) == armed.slot.owner_sid
        assert rights & 0x2 and not rights & 0x4  # data write, not pipe-instance creation
        assert LOCAL not in endpoint.name and LOCAL not in repr(endpoint)
        reviewed = await exchange(endpoint, request(armed))
        assert reviewed["ok"] and reviewed["status"] == "reviewed"
        result = await exchange(endpoint, request(armed, "approve-and-start"))
        assert result["ok"] and result["status"] == "approval_requested"
        assert LOCAL not in json.dumps(result) and len(armed.process.calls) == 2
        endpoint.poll()
        assert armed.slot.closed
        with pytest.raises(OperatorLaunchError):
            await asyncio.to_thread(
                request_operator_launch,
                endpoint.name,
                request(armed, "approve-and-start"),
                timeout=0.1,
            )
        assert len(armed.process.calls) == 2
    finally:
        assert endpoint.close()


@pytest.mark.skipif(os.name != "nt", reason="Windows kernel pipe security")
def test_native_pipe_refuses_owner_mismatch_before_publishing_endpoint(armed):
    assert armed.slot.owner_sid != current_user_sid()
    with pytest.raises(OperatorLaunchError, match="invalid_owner"):
        WindowsOperatorLaunchPipe(armed.slot)
    assert armed.process.calls == []


@pytest.mark.parametrize(
    "name",
    [
        r"\\remote\pipe\cardputer-operator-" + "a" * 32,
        r"\\.\pipe\other-name",
        r"\\.\pipe\cardputer-operator-abc",
    ],
)
def test_sender_never_connects_remote_or_unrecognized_pipe_names(name):
    with pytest.raises(OperatorLaunchError, match="invalid_pipe"):
        request_operator_launch(name, {})


def test_request_cli_uses_os_identity_and_never_loads_or_passes_broker_tokens(monkeypatch, capsys):
    from cardputer_mcp import operator_launch_pipe

    sender = Mock(return_value={"ok": True, "status": "reviewed"})
    monkeypatch.setattr(operator_launch_pipe, "request_operator_launch", sender)
    unexpected = Mock(side_effect=AssertionError("No token lookup or broker API"))
    for name in ("load_environment_token", "load_client_token", "OperatorClient", "run_client"):
        monkeypatch.setattr(cli, name, unexpected)
    monkeypatch.setattr(
        "sys.argv",
        [
            "cardputer",
            "operator-launch-request",
            "--pipe",
            "public-pipe",
            "--action",
            "plan-review",
            "--epoch",
            "e" * 32,
            "--job",
            "one",
            "--plan",
            "b" * 64,
            "--principal",
            "synthetic-owner",
        ],
    )
    cli.main()
    assert json.loads(capsys.readouterr().out)["ok"]
    assert sender.call_args.args[0] == "public-pipe"
    assert sender.call_args.args[1]["principal_id"] == "synthetic-owner"
    unexpected.assert_not_called()


@pytest.mark.parametrize(
    "options",
    [
        ["--auth-source", "environment"],
        ["--client", "remote_mcp"],
        ["--auth-file", "unused.env"],
        ["--url", "http://example.invalid/mcp"],
    ],
)
def test_request_cli_refuses_broker_auth_or_destination_overrides(monkeypatch, options, capsys):
    from cardputer_mcp import operator_launch_pipe

    sender = Mock(side_effect=AssertionError("No IPC on invalid authentication context"))
    monkeypatch.setattr(operator_launch_pipe, "request_operator_launch", sender)
    monkeypatch.setattr(
        "sys.argv",
        [
            "cardputer",
            *options,
            "operator-launch-request",
            "--pipe",
            "public-pipe",
            "--action",
            "plan-review",
            "--epoch",
            "e" * 32,
            "--job",
            "one",
            "--plan",
            "b" * 64,
            "--principal",
            "synthetic-owner",
        ],
    )
    with pytest.raises(SystemExit, match="1"):
        cli.main()
    assert "operator_launch_uses_os_identity_only" in capsys.readouterr().err
    sender.assert_not_called()
