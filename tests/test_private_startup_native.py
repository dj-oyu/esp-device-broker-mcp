"""Native startup composition only; no inputs, compiler invocation or live changes."""

import sys

import pytest
from test_private_state_windows_native import new_protected_directory

from cardputer_mcp.broker import Broker
from cardputer_mcp.config import Settings
from cardputer_mcp.private_build_producer import NativePrivateProducer
from cardputer_mcp.private_startup import PrivateStartup, private_startup_factories

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="native Windows required")


async def test_native_composed_startup_uses_real_producer_and_retains_store(tmp_path):
    state_parent, scratch_parent = tmp_path / "state", tmp_path / "scratch"
    state_parent.mkdir()
    scratch_parent.mkdir()
    state = new_protected_directory(state_parent)
    scratch = new_protected_directory(scratch_parent)
    database = state / "history.sqlite3"
    database.touch()
    settings = Settings(state_dir=state)
    factories = private_startup_factories(settings, PrivateStartup(True, scratch))
    broker = Broker(settings, lock_path=tmp_path / "device.lock", **factories)
    await broker.start()
    try:
        session = broker.private_runtime_session
        assert session is not None
        assert session.builder is broker.firmware_build
        assert session.runtime.artifacts.db is broker._store().db
        producer = session.runtime.produce
        assert isinstance(producer, NativePrivateProducer)
        assert producer.builder is broker.firmware_build
        assert producer.compile_staged is None
        assert producer.container == scratch
        with pytest.raises(PermissionError):
            database.rename(state / "moved.sqlite3")
        assert not session.runtime.pending
    finally:
        await broker.close()
    database.rename(state / "moved.sqlite3")


@pytest.mark.parametrize("unprotected", ["state", "scratch"])
async def test_native_composed_startup_refuses_unprotected_roots_without_db_write(
    tmp_path, unprotected
):
    state_parent, scratch_parent = tmp_path / "state", tmp_path / "scratch"
    state_parent.mkdir()
    scratch_parent.mkdir()
    state = state_parent if unprotected == "state" else new_protected_directory(state_parent)
    scratch = (
        scratch_parent if unprotected == "scratch" else new_protected_directory(scratch_parent)
    )
    database = state / "history.sqlite3"
    database.touch()
    settings = Settings(state_dir=state)
    factories = private_startup_factories(settings, PrivateStartup(True, scratch))
    broker = Broker(settings, lock_path=tmp_path / "device.lock", **factories)
    with pytest.raises(ValueError, match="private_workspace_acl_"):
        await broker.start()
    assert broker.store is None
    assert broker.firmware_build.private_runtime is None
    assert broker.worker is None and broker.profile_jobs.worker is None
    assert database.read_bytes() == b""
    assert not database.with_name(database.name + "-wal").exists()
    await broker.close()
    database.rename(state / "moved.sqlite3")
