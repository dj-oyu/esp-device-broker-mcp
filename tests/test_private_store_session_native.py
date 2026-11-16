"""Generated native session lifecycle; no existing state or ACL changes."""

import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from test_private_state_windows_native import new_protected_directory

from cardputer_mcp import private_store_session as module
from cardputer_mcp.private_input_store import PrivateInputStore

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="native Windows required")


def test_native_preopen_pin_active_reader_retention_and_release(tmp_path, monkeypatch):
    root = new_protected_directory(tmp_path)
    path = root / "generated.db"
    path.touch()
    events = []
    original_init, original_close = module.Store.__init__, module.Store.close

    def pinned():
        with pytest.raises(PermissionError):
            path.rename(root / "renamed.db")
        with pytest.raises(PermissionError):
            root.rename(tmp_path / "renamed-root")

    def initialize(store, *args, **kwargs):
        pinned()  # Actual native denial before SQLite is opened.
        events.append("before_open")
        original_init(store, *args, **kwargs)

    def shutdown(store):
        original_close(store)
        pinned()  # Actual pin still held after SQLite is closed.
        events.append("after_close")

    monkeypatch.setattr(module.Store, "__init__", initialize)
    monkeypatch.setattr(module.Store, "close", shutdown)
    session = module.PrivateStoreSession(path)
    session.__enter__()
    db = session.store.db
    PrivateInputStore(db)
    factory = session.receipt_factory
    entered, release = Event(), Event()

    def worker():
        with factory() as reader:
            entered.set()
            assert release.wait(10)
            assert reader.connection.execute("SELECT 1").fetchone()[0] == 1

    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(worker)
            try:
                assert entered.wait(10)
                with pytest.raises(ValueError, match="readers_active"):
                    session.close()
                pinned()
                assert db.execute("SELECT 1").fetchone()[0] == 1
                with pytest.raises(ValueError, match="not_available"):
                    with factory():
                        pytest.fail("new reader admitted during close")
            finally:
                release.set()
                pending.result(timeout=10)
        pinned()  # Last worker exit must not implicitly unpin the session.
        session.close()
        assert events == ["before_open", "after_close"]
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            db.execute("SELECT 1")
        path.rename(root / "renamed.db")  # Only now is native rename permitted.
        root.rename(tmp_path / "renamed-root")
    finally:
        release.set()
        session.close()
