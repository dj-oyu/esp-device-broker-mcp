"""Generated-only ProfileJobs integration; receipt preparation/native producer mocked.

Uses actual registered-job admission, worker, FirmwareBuild.execute thread handoff,
finalization and Store. It does not establish native staging or installation safety.
"""

import asyncio
import threading

import pytest
from test_profile_jobs import prepare, register
from test_registry_http import registry_service as registry_service

from cardputer_mcp.broker import digest_plan
from cardputer_mcp.models import CancelRequest, ExecuteRequest
from cardputer_mcp.private_artifact_store import PrivateArtifactStore
from cardputer_mcp.private_build_runtime import PrivateBuildCandidate, PrivateBuildRuntime


@pytest.mark.parametrize("mode", ["success", "cancel", "default_closed"])
async def test_real_profile_worker_private_finalization(
    registry_service, tmp_path, monkeypatch, mode
):
    service = registry_service
    register(service, tmp_path)
    broker = service.resident
    store = broker._store()
    builder = broker.firmware_build
    key = service.keys["second"].metadata
    assert builder.private_runtime is None
    prepared = prepare(service, "private-worker")
    plan = store.plan(prepared["plan_hash"]) | {
        "private_input_id": "synthetic",
        "private_input_receipt": "a" * 64,
        "private_binding_sha256": "b" * 64,
        "source_receipt": "c" * 64,
    }
    digest = digest_plan(plan)
    store.put_plan(digest, plan)
    # Fixture-only boundary: no actual private receipt/registration is provisioned.
    monkeypatch.setattr(builder, "_reprepare", lambda *_: dict(plan))
    artifacts = PrivateArtifactStore(store.db)
    started = threading.Event()
    release = threading.Event()
    calls = []

    def produce(snapshot, logs):
        calls.append(snapshot)
        logs.append("synthetic private compiler text")
        started.set()
        if mode == "cancel":
            assert release.wait(5), "test failed to release generated producer"
        return PrivateBuildCandidate("e" * 64, (("fixture.bin", b"generated-only"),))

    if mode != "default_closed":
        builder.private_runtime = PrivateBuildRuntime(produce, artifacts)
    transitions = []
    original_transition = store.ledger.transition

    def transition(job, state, *args, **kwargs):
        transitions.append(state)
        return original_transition(job, state, *args, **kwargs)

    monkeypatch.setattr(store.ledger, "transition", transition)
    monkeypatch.setattr(
        builder, "commit_deferred", lambda *_: pytest.fail("ordinary artifact path used")
    )
    broker.profile_jobs.execute(
        ExecuteRequest(plan_hash=digest), key.principal_id, key.origin.value
    )
    try:
        if mode == "cancel":
            async with asyncio.timeout(3):
                while not started.is_set():
                    await asyncio.sleep(0.01)
            stopped = broker.profile_jobs.cancel(
                CancelRequest(job_id=plan["job_id"]), key.principal_id, key.origin.value
            )
            assert stopped["stop_requested"]
            release.set()
        await asyncio.wait_for(broker.profile_jobs.queue.join(), 5)
    finally:
        release.set()

    job = store.job(plan["job_id"])
    assert transitions.count("running") == 1
    assert "succeeded" not in transitions  # atomic finalizer, never a second ledger write
    assert not builder.manifests
    assert not builder._pending
    if mode == "success":
        assert job["state"] == "succeeded"
        receipt = artifacts.receipt_for_job(job_id=plan["job_id"], principal=key.principal_id)
        assert artifacts.resolve(receipt=receipt, principal=key.principal_id) == (
            ("fixture.bin", b"generated-only"),
        )
        assert store.db.execute(
            "SELECT COUNT(*) FROM execution_audit WHERE event='private_build_committed'"
        ).fetchone()[0] == 1
        assert store.db.execute("SELECT COUNT(*) FROM private_artifact_pins").fetchone()[0] == 1
        assert not builder.private_runtime.pending
        assert builder.request_stop(plan["job_id"]) is False
    else:
        assert job["state"] == ("cancelled" if mode == "cancel" else "failed")
        for table in (
            "private_artifact_receipts",
            "private_artifact_bytes",
            "private_artifact_pins",
        ):
            assert store.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
        assert store.db.execute(
            "SELECT COUNT(*) FROM execution_audit WHERE event='private_build_committed'"
        ).fetchone()[0] == 0
        if mode == "default_closed":
            assert not calls
            assert builder.private_runtime is None
        else:
            assert not builder.private_runtime.pending
