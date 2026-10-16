"""Actual formal CLI children and authenticated mock HTTP, through native local IPC."""

import asyncio
import hashlib
import json
import os
import sys

import pytest
from test_operator_launch_pipe import exchange
from test_registry_http import LOCAL
from test_registry_http import registry_service as registry_service

from cardputer_mcp.build_process import BuildProcess
from cardputer_mcp.client import call, connect
from cardputer_mcp.operator_launch import CaptureLaunchScope, OperatorLaunchSlot
from cardputer_mcp.operator_launch_pipe import WindowsOperatorLaunchPipe, current_user_sid

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Actual Windows same-user pipe and CLI")


class PythonCliProcess(BuildProcess):
    """Test adapter only: exercise the installed formal CLI without building an EXE."""

    def run(self, args, *positional, **options):
        return super().run(
            [sys.executable, "-m", "cardputer_mcp.cli", *args[1:]], *positional, **options
        )


async def prepare_slot(env, remote, tmp_path):
    env.resident.settings = env.resident.settings.model_copy(update={"allow_mock_execution": False})
    lease = await call(remote, "acquire_session", {"owner": "remote_mcp", "lease_seconds": 300})
    prepared = await call(
        remote,
        "prepare_test",
        {"session": lease["session"], "job_id": "launch-http-one", "test": "capture_home"},
    )
    submitted = await call(
        remote,
        "execute_test",
        {
            "session": lease["session"],
            "plan_hash": prepared["plan_hash"],
            "wait_for_approval": True,
        },
    )
    assert submitted["state"] == "awaiting_approval" and env.backend.calls == 0
    from pathlib import Path

    executable = Path(sys.executable)
    scope = CaptureLaunchScope(
        broker_epoch=prepared["plan"]["broker_epoch"],
        job_id="launch-http-one",
        plan_hash=prepared["plan_hash"],
        principal_id=env.keys["second"].metadata.principal_id,
    )
    executable_hash = hashlib.sha256(await asyncio.to_thread(executable.read_bytes)).hexdigest()
    slot = OperatorLaunchSlot(
        executable,
        executable_hash,
        tmp_path,
        env.url,
        LOCAL,
        current_user_sid(),
        None,
        broker_epoch=scope.broker_epoch,
        allow_image_delivery=True,
        lifetime_seconds=30,
        process=PythonCliProcess(),
    )
    return lease, slot, WindowsOperatorLaunchPipe(slot), scope


async def test_native_entry_runs_only_formal_cli_and_one_exact_mock_capture(
    registry_service, tmp_path
):
    env = registry_service
    async with connect(env.url, env.keys["second"].token) as remote:
        _, slot, endpoint, scope = await prepare_slot(env, remote, tmp_path)
        try:
            reviewed = await exchange(endpoint, {**scope.model_dump(), "action": "plan-review"})
            assert reviewed["ok"], reviewed
            assert slot.scope == scope
            assert env.backend.calls == 0
            payload = {
                **slot.scope.model_dump(),
                "action": "approve-and-start",
                "review_revision": reviewed["review"]["review_revision"],
                "attestation": "",
                "allow_image_delivery": True,
            }
            approved = await exchange(endpoint, payload)
            assert approved["ok"], approved
            await env.resident.queue.join()
            assert env.backend.calls == 1 and not slot.process.active and not slot.process.uncertain
            result = await remote.call_tool(
                "read_capture", {"request": {"job_id": slot.scope.job_id}}
            )
            assert not result.is_error and any(item.type == "image" for item in result.content)
            assert LOCAL not in json.dumps(reviewed) and LOCAL not in json.dumps(approved)
            assert not slot.handle(payload, peer_sid=slot.owner_sid)["ok"]
            assert env.backend.calls == 1 and slot._local_token is None
            assert "operator_launch_request" not in {
                item.name for item in (await remote.list_tools()).tools
            }
        finally:
            assert endpoint.close()


@pytest.mark.parametrize("change", ["cancel", "revoke", "expire", "epoch"])
async def test_broker_rechecks_current_authority_after_ipc_review(
    registry_service, tmp_path, change
):
    env = registry_service
    async with connect(env.url, env.keys["second"].token) as remote:
        lease, slot, endpoint, scope = await prepare_slot(env, remote, tmp_path)
        try:
            reviewed = await exchange(endpoint, {**scope.model_dump(), "action": "plan-review"})
            assert reviewed["ok"], reviewed
            if change == "cancel":
                await call(
                    remote, "cancel_job", {"session": lease["session"], "job_id": slot.scope.job_id}
                )
            elif change == "revoke":
                env.registry.revoke(env.keys["second"].metadata.key_id)
            elif change == "expire":
                env.resident.session.deadline = env.resident.clock()
            else:
                env.resident.epoch = "f" * 32
            payload = {
                **slot.scope.model_dump(),
                "action": "approve-and-start",
                "review_revision": reviewed["review"]["review_revision"],
                "attestation": "",
                "allow_image_delivery": True,
            }
            result = await exchange(endpoint, payload)
            assert result == {"ok": False, "error": "operator_launch_approval_outcome_unknown"}
            assert env.backend.calls == 0 and slot.closed and slot._local_token is None
            assert not slot.handle(payload, peer_sid=slot.owner_sid)["ok"]
        finally:
            assert endpoint.close()
