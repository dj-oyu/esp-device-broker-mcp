"""Broker startup with a real native guard and generated protected SQLite state."""

import asyncio
import sqlite3
import sys

import pytest
from test_private_state_windows_native import new_protected_directory

from cardputer_mcp.broker import Broker
from cardputer_mcp.config import Settings
from cardputer_mcp.private_store_session import PrivateStoreSession
from cardputer_mcp.store import Store

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="native Windows required")


@pytest.fixture
def generated_state(tmp_path):
    root = new_protected_directory(tmp_path)
    path = root / "history.sqlite3"
    path.touch()
    return root, path


async def test_native_broker_preopen_pin_commit_callback_and_final_release(
    generated_state, tmp_path, monkeypatch
):
    root, path = generated_state
    sessions, events, notifications = [], [], []
    initialize, close = Store.__init__, Store.close

    def pinned():
        with pytest.raises(PermissionError):
            path.rename(root / "moved.db")

    def factory(p, on_commit):
        assert p == path and on_commit == broker.operator_changes.notify
        session = PrivateStoreSession(p, on_commit=on_commit)
        sessions.append(session)
        return session

    broker = Broker(
        Settings(state_dir=root),
        lock_path=tmp_path / "device.lock",
        private_store_session_factory=factory,
    )
    notify = broker.operator_changes.notify

    def notified():
        notifications.append(1)
        notify()

    monkeypatch.setattr(broker.operator_changes, "notify", notified)

    def init(store, *args, **kwargs):
        pinned()
        events.append("preopen-pinned")
        initialize(store, *args, **kwargs)

    def shutdown(store):
        pinned()
        close(store)
        pinned()
        events.append("postclose-pinned")

    monkeypatch.setattr(Store, "__init__", init)
    monkeypatch.setattr(Store, "close", shutdown)
    try:
        await broker.start()
        db = broker._store().db
        assert broker.store is sessions[0].store
        assert broker.private_runtime_session is None
        assert broker.firmware_build.private_runtime is None
        before = len(notifications)
        db.commit()
        assert len(notifications) == before + 1
        await asyncio.to_thread(pinned)
    finally:
        await broker.close()
    assert events == ["preopen-pinned", "postclose-pinned"]
    assert sessions[0]._closed and broker.store is None
    assert not broker._state_lock.is_locked and not broker._device_lock.is_locked
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        db.execute("SELECT 1")
    await asyncio.to_thread(path.rename, root / "moved.db")
