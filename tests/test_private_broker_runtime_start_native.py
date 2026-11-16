"""Native protected startup with generated output; not an IDF or real-input build."""

import asyncio
import sys
from types import SimpleNamespace

import pytest
from test_private_state_windows_native import new_protected_directory
from test_profile_jobs import register

from cardputer_mcp.broker import Broker, digest_plan
from cardputer_mcp.config import Settings
from cardputer_mcp.models import ExecuteRequest, PrepareRequest
from cardputer_mcp.private_artifact_store import PrivateArtifactStore
from cardputer_mcp.private_build_runtime import PrivateBuildCandidate, PrivateBuildRuntime
from cardputer_mcp.private_runtime_session import PrivateRuntimeSession
from cardputer_mcp.private_store_session import PrivateStoreSession

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="native Windows required")


def setup_native(tmp_path):
    root = new_protected_directory(tmp_path)
    path = root / "history.sqlite3"
    path.touch()
    made = []

    def factory(builder, session):
        assert broker.worker is None and broker.profile_jobs.worker is None
        runtime = PrivateBuildRuntime(
            lambda *_: PrivateBuildCandidate("e" * 64, (("fixture.bin", b"generated-only"),)),
            PrivateArtifactStore(session.store.db),
        )
        wrapper = PrivateRuntimeSession(builder, session, runtime)
        made.append(wrapper)
        return wrapper

    broker = Broker(
        Settings(state_dir=root),
        lock_path=tmp_path / "device.lock",
        private_store_session_factory=lambda p, callback: PrivateStoreSession(
            p, on_commit=callback
        ),
        private_runtime_session_factory=factory,
    )
    return broker, path, made


def pinned(path):
    with pytest.raises(PermissionError):
        path.rename(path.with_name("moved.db"))


async def test_native_startup_profile_atomic_finalize_and_release(tmp_path, monkeypatch):
    broker, path, made = setup_native(tmp_path)
    register(SimpleNamespace(resident=broker), tmp_path)
    broker.principal_valid = lambda *_: True
    await broker.start()
    try:
        wrapper = made[0]
        await asyncio.to_thread(pinned, path)
        prepared = broker.profile_jobs.prepare(
            PrepareRequest(job_id="native-startup", test="firmware_build", project_id="fixture"),
            "owner",
            "local",
        )
        store = broker._store()
        plan = store.plan(prepared["plan_hash"]) | {
            "private_input_id": "synthetic",
            "private_input_receipt": "a" * 64,
            "private_binding_sha256": "b" * 64,
            "source_receipt": "c" * 64,
        }
        digest = digest_plan(plan)
        store.put_plan(digest, plan)
        # Source/recipe resolution is synthetic; native DB guards remain real.
        monkeypatch.setattr(broker.firmware_build, "_reprepare", lambda *_: dict(plan))
        broker.profile_jobs.execute(ExecuteRequest(plan_hash=digest), "owner", "local")
        await asyncio.wait_for(broker.profile_jobs.queue.join(), 5)
        assert store.job(plan["job_id"])["state"] == "succeeded"
        receipt = wrapper.runtime.artifacts.receipt_for_job(
            job_id=plan["job_id"], principal="owner"
        )
        assert wrapper.runtime.artifacts.resolve(receipt=receipt, principal="owner") == (
            ("fixture.bin", b"generated-only"),
        )
        assert not broker.firmware_build.manifests
        await asyncio.to_thread(pinned, path)
    finally:
        await broker.close()
    assert made[0]._closed and made[0].session._closed
    assert broker.firmware_build.private_runtime is None and broker.store is None
    await asyncio.to_thread(path.rename, path.with_name("moved.db"))


async def test_native_failed_start_retains_pin_until_verified_close(tmp_path, monkeypatch):
    broker, path, made = setup_native(tmp_path)
    monkeypatch.setattr(
        broker.profile_jobs, "start", lambda: (_ for _ in ()).throw(RuntimeError("fixture"))
    )
    with pytest.raises(RuntimeError, match="fixture"):
        await broker.start()
    wrapper = made[0]
    try:
        assert wrapper._installed and not wrapper.runtime._accepting
        assert not wrapper.session._closed and broker._state_lock.is_locked
        await asyncio.to_thread(pinned, path)
    finally:
        await broker.close()
    assert wrapper._closed and wrapper.session._closed
    assert not broker._state_lock.is_locked
    await asyncio.to_thread(path.rename, path.with_name("moved.db"))
