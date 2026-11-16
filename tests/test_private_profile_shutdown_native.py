"""Native state pin plus real ProfileJobs shutdown; generated inputs only."""

import asyncio
import sqlite3
import sys
from pathlib import Path

import pytest
from test_private_state_windows_native import new_protected_directory
from test_profile_jobs import prepare, register
from test_registry_http import registry_service as registry_service

from cardputer_mcp.models import ExecuteRequest
from cardputer_mcp.private_artifact_store import PrivateArtifactStore
from cardputer_mcp.private_build_runtime import PrivateBuildRuntime
from cardputer_mcp.private_runtime_session import PrivateRuntimeSession
from cardputer_mcp.private_store_session import PrivateStoreSession

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="native Windows required")


@pytest.fixture(autouse=True)
def protected_state(tmp_path):
    new_protected_directory(tmp_path).rename(tmp_path / "state")


async def test_native_queue_cancel_unknown_join_then_store_close(
    registry_service, tmp_path, monkeypatch
):
    service = registry_service
    register(service, tmp_path)
    broker = service.resident
    old = broker._store()
    path = Path(old.db.execute("PRAGMA database_list").fetchone()[2])
    old.close()
    session = PrivateStoreSession(path)
    session.__enter__()
    broker.store = session.store
    db = session.store.db
    builder = broker.firmware_build
    runtime = PrivateBuildRuntime(
        lambda *_: pytest.fail("synthetic build must not invoke producer"),
        PrivateArtifactStore(db),
    )
    wrapper = PrivateRuntimeSession(builder, session, runtime)
    wrapper.__enter__()
    broker.private_runtime_session = wrapper
    jobs = broker.profile_jobs
    started, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def synthetic_build(plan, stage, *, defer_publish):
        calls.append(plan["job_id"])
        started.set()
        await release.wait()
        builder._check_cancelled()
        pytest.fail("cancelled build succeeded")

    monkeypatch.setattr(builder, "execute", synthetic_build)
    owner = service.keys["second"].metadata
    try:
        for name in ("running-native", "queued-native"):
            plan = prepare(service, name)
            jobs.execute(
                ExecuteRequest(plan_hash=plan["plan_hash"]), owner.principal_id, owner.origin.value
            )
        await asyncio.wait_for(started.wait(), timeout=5)
        worker = jobs.worker
        closing = asyncio.create_task(broker.close())
        await asyncio.sleep(0)
        closing.cancel()
        with pytest.raises(asyncio.CancelledError):
            await closing
        assert worker is not None and not worker.cancelled() and not worker.done()
        assert not runtime._accepting
        with pytest.raises(PermissionError):
            await asyncio.to_thread(path.rename, path.with_name("moved.db"))
        with pytest.raises(ValueError, match="broker_closing"):
            jobs.execute(ExecuteRequest(plan_hash="a" * 64), owner.principal_id, owner.origin.value)
        # Explicit synthetic unknown-child flag, not a real process exit proof.
        builder.process.uncertain = True
        release.set()
        with pytest.raises(ValueError, match="join_unconfirmed"):
            await broker.close()
        assert jobs.worker is None and calls == ["running-native"]
        assert broker.job("queued-native")["state"] == "cancelled"
        assert db.execute("SELECT 1").fetchone()[0] == 1
        with pytest.raises(ValueError, match="supervisor_active"):
            session.close()
        with pytest.raises(PermissionError):
            await asyncio.to_thread(path.rename, path.with_name("moved.db"))
        builder.process.uncertain = False
        await broker.close()
        assert broker.store is None
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            db.execute("SELECT 1")
        await asyncio.to_thread(path.rename, path.with_name("moved.db"))
    finally:
        release.set()
        builder.process.uncertain = False
        await broker.close()
