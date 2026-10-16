"""One packaged request client + privileged CLI child against mock-only HTTP."""

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path

from cardputer_mcp.build_process import BuildProcess
from cardputer_mcp.client import call, connect
from cardputer_mcp.firmware_build import OS_ENV
from cardputer_mcp.operator_client import OperatorClient
from cardputer_mcp.operator_launch import CaptureLaunchScope, OperatorLaunchSlot
from cardputer_mcp.operator_launch_pipe import WindowsOperatorLaunchPipe, current_user_sid


async def smoke(executable: Path, url: str, cwd: Path) -> None:
    local = os.environ["CARDPUTER_LOCAL_TOKEN"]
    remote = os.environ["CARDPUTER_REMOTE_TOKEN"]
    async with connect(url, remote) as client:
        status = await call(client, "device_status")
        assert status["ok"] and status["backend"] == "mock" and not status["hardware_connected"]
        lease = await call(client, "acquire_session", {"owner": "remote_mcp", "lease_seconds": 300})
        prepared = await call(client, "prepare_test", {"session": lease["session"],
            "job_id": "packaged-launch-capture", "test": "capture_home"})
        submitted = await call(client, "execute_test", {"session": lease["session"],
            "plan_hash": prepared["plan_hash"], "wait_for_approval": True})
        assert submitted["state"] == "awaiting_approval"
        owner = await asyncio.to_thread(OperatorClient(url, local).state)
        scope = CaptureLaunchScope(broker_epoch=prepared["plan"]["broker_epoch"],
            job_id="packaged-launch-capture", plan_hash=prepared["plan_hash"],
            principal_id=owner["owner"]["principal_id"])
        digest = hashlib.sha256(await asyncio.to_thread(executable.read_bytes)).hexdigest()
        slot = OperatorLaunchSlot(executable, digest, cwd, url, local, current_user_sid(),
                                  None, broker_epoch=scope.broker_epoch,
                                  allow_image_delivery=True, lifetime_seconds=60)
        endpoint = WindowsOperatorLaunchPipe(slot)
        environment = {key.upper(): value for key, value in os.environ.items()
                       if key.upper() in OS_ENV}

        async def request(action: str, revision: str | None = None) -> dict:
            args = [str(executable), "operator-launch-request", "--pipe", endpoint.name,
                    "--action", action, "--epoch", scope.broker_epoch, "--job", scope.job_id,
                    "--plan", scope.plan_hash, "--principal", scope.principal_id]
            if revision is not None:
                args += ["--review-revision", revision, "--attestation", "", "--allow-image-delivery"]
            lines = []

            def execute():
                process = BuildProcess()
                code = process.run(args, environment, cwd,
                    lambda text, stream: lines.append(text) if stream == "stdout" else None,
                    timeout=25)
                assert code == 0 and not process.active and not process.uncertain
                return json.loads("\n".join(lines))

            task = asyncio.create_task(asyncio.to_thread(execute))
            while not task.done():
                await asyncio.to_thread(endpoint.poll)
                await asyncio.sleep(0.02)
            return await task

        try:
            review = await request("plan-review")
            assert review["ok"]
            waiting = await call(client, "job_status", {"job_id": scope.job_id})
            assert waiting["state"] == "awaiting_approval"
            approved = await request("approve-and-start", review["review"]["review_revision"])
            assert approved["ok"]
            async with asyncio.timeout(10):
                while True:
                    job = await call(client, "job_status", {"job_id": scope.job_id})
                    if job["state"] == "succeeded":
                        break
                    await asyncio.sleep(0.05)
            result = await client.call_tool("read_capture", {"request": {"job_id": scope.job_id}})
            assert not result.is_error and any(item.type == "image" for item in result.content)
            assert local not in json.dumps(review) and remote not in json.dumps(review)
            assert not slot.handle({**scope.model_dump(), "action": "approve-and-start"},
                                   peer_sid=slot.owner_sid)["ok"]
            assert not slot.process.active and not slot.process.uncertain
            await call(client, "release_session", {"session": lease["session"]})
        finally:
            assert endpoint.close()
            environment.clear()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--exe", type=Path, required=True)
    parser.add_argument("--url", required=True)
    parser.add_argument("--cwd", type=Path, required=True)
    args = parser.parse_args()
    try:
        asyncio.run(smoke(args.exe, args.url, args.cwd))
    except Exception:
        raise SystemExit(1) from None
