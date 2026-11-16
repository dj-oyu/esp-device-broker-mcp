"""Generated-only native pipeline; source receipt and compiler are synthetic."""

import asyncio
import hashlib
import sys
from contextlib import contextmanager, nullcontext
from types import SimpleNamespace

import pytest
from test_private_state_windows_native import new_protected_directory
from test_profile_jobs import prepare, register
from test_registry_http import registry_service as registry_service

from cardputer_mcp import private_build_workspace, private_registration_ingest
from cardputer_mcp.broker import digest_plan
from cardputer_mcp.models import ExecuteRequest
from cardputer_mcp.private_artifact_store import PrivateArtifactStore
from cardputer_mcp.private_build_producer import NativePrivateProducer
from cardputer_mcp.private_build_runtime import PrivateBuildRuntime
from cardputer_mcp.private_input_contract import InputFile
from cardputer_mcp.private_input_registration import PrivateInputRegistration
from cardputer_mcp.private_input_store import PrivateInputStore, inventory_digest
from cardputer_mcp.store import Store

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="native Windows required")


@pytest.fixture(autouse=True)
def generated_protected_state(tmp_path):
    new_protected_directory(tmp_path).rename(tmp_path / "state")


async def test_native_ingestion_to_profile_atomic_finalize(registry_service, tmp_path, monkeypatch):
    service = registry_service
    project = register(service, tmp_path)
    broker = service.resident
    store = broker._store()
    builder = broker.firmware_build
    owner = service.keys["second"].metadata
    prepared = prepare(service, "native-pipeline")
    plan = store.plan(prepared["plan_hash"])
    private = tmp_path / "generated-input"
    private.mkdir()
    payload = b"generated private input"
    (private / "fixture.bin").write_bytes(payload)
    manifest = (InputFile("fixture.bin", len(payload), hashlib.sha256(payload).hexdigest()),)
    reg = PrivateInputRegistration(
        local_root=private,
        destination="assets",
        source_commit="a" * 40,
        recipe_sha256=plan["fingerprint"],
        inventory_sha256=inventory_digest(manifest),
    )
    project.private_inputs["fixture"] = reg
    public_bytes = (project.project_dir / "CMakeLists.txt").read_bytes()
    metadata = {
        "commit": "a" * 40,
        "files": {
            "CMakeLists.txt": {
                "bytes": len(public_bytes),
                "sha256": hashlib.sha256(public_bytes).hexdigest(),
            }
        },
    }
    snapshot = SimpleNamespace(
        cache_guard=lambda *_: nullcontext(),
        _resolve=lambda *_: (project.project_dir, metadata),
    )
    monkeypatch.setattr(private_registration_ingest, "SourceSnapshots", lambda _: snapshot)
    monkeypatch.setattr(private_build_workspace, "SourceSnapshots", lambda _: snapshot)
    original_prepare = builder.prepare
    monkeypatch.setattr(builder, "prepare", lambda *_a, **_k: {"fingerprint": reg.recipe_sha256})
    receipt = private_registration_ingest.ingest_registered_input(
        builder,
        store,
        project_id="fixture",
        input_id="fixture",
        source_receipt="c" * 64,
        principal=owner.principal_id,
        manifest=manifest,
        authorized=lambda: True,
        cancelled=lambda: False,
    )
    monkeypatch.setattr(builder, "prepare", original_prepare)
    plan = plan | {
        "private_input_id": "fixture",
        "private_input_receipt": receipt,
        "private_binding_sha256": "b" * 64,
        "source_receipt": "c" * 64,
    }
    digest = digest_plan(plan)
    store.put_plan(digest, plan)
    monkeypatch.setattr(builder, "_reprepare", lambda *_: dict(plan))

    @contextmanager
    def inputs():
        # Worker uses its own SQLite connection; no cross-thread DB reuse.
        other = Store(tmp_path / "state/history.sqlite3")
        try:
            yield PrivateInputStore(other.db)
        finally:
            other.close()

    builder.private_store_factory = inputs
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    paths = []

    def compiler(source, output, logs):
        paths.extend((source, output))
        assert (source / "assets/fixture.bin").read_bytes() == payload
        assert (source / "CMakeLists.txt").read_bytes() == public_bytes
        assert source != output
        generated = b"generated firmware result"
        (output / "firmware.bin").write_bytes(generated)
        return (InputFile("firmware.bin", len(generated), hashlib.sha256(generated).hexdigest()),)

    artifacts = PrivateArtifactStore(store.db)
    builder.private_runtime = PrivateBuildRuntime(
        NativePrivateProducer(builder, scratch, compiler), artifacts
    )
    broker.profile_jobs.execute(
        ExecuteRequest(plan_hash=digest), owner.principal_id, owner.origin.value
    )
    await asyncio.wait_for(broker.profile_jobs.queue.join(), 10)
    assert store.job(plan["job_id"])["state"] == "succeeded"
    output_receipt = artifacts.receipt_for_job(job_id=plan["job_id"], principal=owner.principal_id)
    assert artifacts.resolve(receipt=output_receipt, principal=owner.principal_id) == (
        ("firmware.bin", b"generated firmware result"),
    )
    assert len(paths) == 2 and all(not path.exists() for path in paths)
    assert not builder.private_runtime.pending
    assert builder.private_runtime._operation is None
    assert (
        store.db.execute(
            "SELECT COUNT(*) FROM execution_audit WHERE event='private_build_committed'"
        ).fetchone()[0]
        == 1
    )
    assert store.db.execute("SELECT COUNT(*) FROM private_artifact_pins").fetchone()[0] == 1
    assert not builder.manifests and not builder._pending
