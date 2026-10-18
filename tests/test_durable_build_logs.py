"""Receipt reads survive RAM loss; synthetic local bytes, never builds or USB."""

import asyncio
import json
import socket
import threading
from contextlib import asynccontextmanager

import pytest
import test_build_artifact_persistence as fixtures
import uvicorn
from mcp.types import CallToolRequest, CallToolRequestParams, CallToolResult

from cardputer_mcp.auth import AuthState
from cardputer_mcp.broker import Broker
from cardputer_mcp.build_logs import BuildLogs
from cardputer_mcp.client import call, connect
from cardputer_mcp.config import BuildProject, Settings
from cardputer_mcp.models import BuildLogRequest, Origin
from cardputer_mcp.server import create_app

LOCAL, REMOTE = "a" * 64, "b" * 64
OWNER = "ephemeral:remote_mcp"


@pytest.fixture
def receipt():
    case = fixtures.DurableBuildArtifactTests()
    case.setUp()
    try:
        yield case
    finally:
        case.doCleanups()


@pytest.mark.parametrize("retention", ["restart", "expired", "fresh"])
def test_durable_manifest_is_independent_of_log_retention(receipt, retention):
    root, manifest = receipt.publish()
    receipt.restart()
    if retention != "restart":
        logs = BuildLogs("alice")
        logs.append("warning: synthetic retained output")
        if retention == "expired":
            logs.deadline = 0
        receipt.builder.logs["build-0"] = logs
    before = list(receipt.store.db.iterdump())
    result = receipt.builder.read_logs(
        BuildLogRequest(job_id="build-0"), "alice", project_id="fixture", broker_epoch="f" * 32
    )
    assert result["manifest"] == manifest
    assert result["artifact_status"] == "verified"
    assert result["expired"] is (retention != "fresh")
    if retention != "fresh":
        assert result["entries"] == [] and result["gap"] is True
    if retention == "restart":
        assert result["reason"] == "not_started_or_history_not_retained"
        assert result["next_cursor"] == {"epoch": "f" * 32, "sequence": 0}
    assert str(root) not in json.dumps(result)
    assert list(receipt.store.db.iterdump()) == before
    assert receipt.builder.artifacts._records == {}


@pytest.mark.parametrize(
    "file,mutation",
    [
        ("broker-manifest.json", "corrupt"),
        ("app.bin", "corrupt"),
        ("app.elf", "corrupt"),
        ("flasher_args.json", "corrupt"),
        ("config/sdkconfig.json", "corrupt"),
        ("app.bin", "missing"),
        ("broker-manifest.json", "missing"),
        ("broker-build-identity.json", "corrupt"),
    ],
)
@pytest.mark.parametrize("retention", ["restart", "fresh"])
def test_invalid_receipt_never_falls_back_to_ram_manifest(receipt, file, mutation, retention):
    root, manifest = receipt.publish()
    receipt.restart()
    receipt.builder.manifests["build-0"] = ("alice", manifest)
    if retention == "fresh":
        logs = BuildLogs("alice")
        logs.append("warning: synthetic retained output")
        receipt.builder.logs["build-0"] = logs
    path = root / file
    if mutation == "missing":
        path.unlink()
    else:
        path.write_bytes(b"SYNTHETIC_PRIVATE_PATH_OR_SECRET")
    result = receipt.builder.read_logs(
        BuildLogRequest(job_id="build-0"), "alice", project_id="fixture"
    )
    assert result["expired"] is (retention == "restart")
    if retention == "restart":
        assert result["gap"]
    else:
        assert result["entries"]
    assert result["artifact_status"] == "unavailable"
    assert "manifest" not in result
    assert str(root) not in json.dumps(result)
    assert "SYNTHETIC_PRIVATE" not in json.dumps(result)
    assert receipt.store.job("build-0")["state"] == "succeeded"


@pytest.mark.parametrize("state", ["running", "cancelled", "needs_review", "failed"])
def test_unsuccessful_job_cannot_expose_a_durable_manifest(receipt, state):
    receipt.publish(finish=False)
    receipt.store.set_job("build-0", state)
    receipt.restart()
    result = receipt.builder.read_logs(
        BuildLogRequest(job_id="build-0"), "alice", project_id="fixture"
    )
    assert result["artifact_status"] == "unavailable"
    assert "manifest" not in result


@asynccontextmanager
async def running(receipt):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    settings = Settings(
        state_dir=receipt.state,
        port=port,
        build_projects={
            "fixture": BuildProject(
                project_dir=receipt.root,
                trusted_profile=receipt.root / "profile.ps1",
                build_root=receipt.project.build_root,
            )
        },
    )
    broker = Broker(settings, lock_path=receipt.root / "device.lockfile")
    auth = AuthState({Origin.LOCAL: LOCAL, Origin.REMOTE: REMOTE})
    app = create_app(settings, broker, auth=auth, require_auth=True)
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=port,
            log_level="critical",
            access_log=False,
        )
    )
    task = asyncio.create_task(server.serve())
    try:
        async with asyncio.timeout(5):
            while not server.started:
                if task.done():
                    await task
                    pytest.fail("Synthetic receipt broker stopped before readiness")
                await asyncio.sleep(0.01)
        yield f"http://127.0.0.1:{port}/mcp", broker, auth
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 5)


async def test_actual_mcp_receipt_survives_broker_restart_and_denies_other_principal(receipt):
    _, manifest = receipt.publish(owner=OWNER)
    async with running(receipt) as (url, broker, _), connect(url, REMOTE) as owner:
        logs = BuildLogs(OWNER)
        logs.append("warning: synthetic job output")
        broker.firmware_build.logs["build-0"] = logs
        first = await call(owner, "read_build_logs", {"job_id": "build-0"})
        assert first["ok"] and first["manifest"] == manifest and not first["expired"]
    before = list(receipt.store.db.iterdump())
    async with (
        running(receipt) as (url, broker, _),
        connect(url, REMOTE) as owner,
        connect(url, LOCAL) as stranger,
    ):
        assert not broker.firmware_build.logs and not broker.firmware_build.manifests
        result = await call(owner, "read_build_logs", {"job_id": "build-0"})
        assert result["ok"] and result["expired"] and result["gap"]
        assert result["artifact_status"] == "verified" and result["manifest"] == manifest
        denied = await call(stranger, "read_build_logs", {"job_id": "build-0"})
        assert denied == {"ok": False, "error": "build_log_not_available"}
        assert broker.session is None and broker.backend.calls == 0
        assert list(receipt.store.db.iterdump()) == before


@pytest.mark.parametrize("invalid", ["manifest", "missing_bin"])
async def test_mcp_unavailable_artifact_preserves_log_gap_and_success_history(receipt, invalid):
    root, _ = receipt.publish(owner=OWNER)
    if invalid == "manifest":
        (root / "broker-manifest.json").write_text("SYNTHETIC_PRIVATE_ERROR")
    else:
        (root / "app.bin").unlink()
    async with running(receipt) as (url, broker, _), connect(url, REMOTE) as owner:
        result = await call(owner, "read_build_logs", {"job_id": "build-0"})
        assert result["ok"] and result["expired"] and result["gap"]
        assert result["artifact_status"] == "unavailable" and "manifest" not in result
        assert "SYNTHETIC_PRIVATE" not in json.dumps(result)
        assert str(root) not in json.dumps(result)
        assert broker.job("build-0")["state"] == "succeeded"


@pytest.mark.parametrize(
    "mutation", ["none", "revoke", "registration", "job_state", "plan_hash", "epoch"]
)
async def test_hash_io_is_off_loop_and_rechecks_authority_before_delivery(
    receipt, monkeypatch, mutation
):
    receipt.publish(owner=OWNER)
    entered, release = threading.Event(), threading.Event()
    async with running(receipt) as (url, broker, auth), connect(url, REMOTE) as owner:
        original = broker.firmware_build._verify_owned_artifacts

        def verify(root, manifest):
            entered.set()
            assert release.wait(4)
            original(root, manifest)

        monkeypatch.setattr(broker.firmware_build, "_verify_owned_artifacts", verify)

        async def read_without_catalog_refresh():
            # Revocation must be observed on the original in-flight result,
            # without a client-side tools/list refresh hiding that response.
            result = await owner.session.send_request(
                CallToolRequest(
                    params=CallToolRequestParams(
                        name="read_build_logs",
                        arguments={"request": {"job_id": "build-0"}},
                    )
                ),
                CallToolResult,
            )
            return result.structured_content

        reading = asyncio.create_task(read_without_catalog_refresh())
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            async with connect(url, REMOTE) as status_client:
                concurrent = await asyncio.wait_for(
                    call(status_client, "read_build_logs", {"job_id": "build-0"}), 1
                )
                assert concurrent["ok"] and concurrent["artifact_status"] == "busy"
                assert "manifest" not in concurrent
                status = await asyncio.wait_for(call(status_client, "device_status"), 1)
                assert status["ok"] and status["active_job"] is None
            if mutation == "revoke":
                auth._tokens[Origin.REMOTE] = "c" * 64
            elif mutation == "registration":
                broker.settings.build_projects.clear()
            elif mutation == "job_state":
                broker._store().set_job("build-0", "needs_review", "synthetic_invalidation")
            elif mutation == "plan_hash":
                store = broker._store()
                store.db.execute("UPDATE jobs SET plan_hash=? WHERE id='build-0'", ("f" * 64,))
                store.db.commit()
            elif mutation == "epoch":
                broker.epoch = "f" * 32
        finally:
            release.set()
        result = await asyncio.wait_for(reading, 3)
        if mutation == "none":
            assert result["ok"] and result["artifact_status"] == "verified"
            again = await call(owner, "read_build_logs", {"job_id": "build-0"})
            assert again["ok"] and again["artifact_status"] == "verified"
            assert again["manifest"] == result["manifest"]
        else:
            assert result["ok"] is False and "manifest" not in result
            assert result["error"] in {"authentication_required", "build_log_not_available"}
