"""Synthetic writable Store sessions; native rename proof is a separate test."""

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from threading import Event

import pytest

from cardputer_mcp import private_store_session as module
from cardputer_mcp.private_input_store import PrivateInputStore


@pytest.fixture
def fixture(tmp_path):
    path = tmp_path / "generated.db"
    path.touch()  # Existing empty synthetic DB, never an actual private input.
    events = []
    count = [0]

    @contextmanager
    def guard(actual):
        assert actual == path
        count[0] += 1
        events.append("pin")
        try:
            yield (1, b"fixture")
        finally:
            count[0] -= 1
            events.append("unpin")

    return path, events, count, guard


def test_pin_precedes_store_open_close_precedes_unpin(fixture, monkeypatch):
    path, events, count, guard = fixture
    init, close = module.Store.__init__, module.Store.close

    def initialize(store, *a, **k):
        assert count[0] == 1
        events.append("store_open")
        init(store, *a, **k)

    def shutdown(store):
        assert count[0] == 1
        events.append("store_close")
        close(store)

    monkeypatch.setattr(module.Store, "__init__", initialize)
    monkeypatch.setattr(module.Store, "close", shutdown)
    session = module.PrivateStoreSession(path, path_guard=guard)
    with session:
        db = session.store.db
        PrivateInputStore(db)
        factory = session.receipt_factory
        with ThreadPoolExecutor() as pool:

            def read():
                with factory() as reader:
                    return reader.connection.execute(
                        "SELECT COUNT(*) FROM private_input_receipts"
                    ).fetchone()[0]

            assert pool.submit(read).result() == 0
    assert events[0:2] == ["pin", "store_open"]
    assert events[-2:] == ["store_close", "unpin"]
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        db.execute("SELECT 1")
    with pytest.raises(ValueError, match="not_available"):
        _ = session.receipt_factory
    with pytest.raises(ValueError, match="not_available"):
        with factory():
            pytest.fail("reused closed session")


def test_active_worker_retains_store_pin_until_explicit_retry(fixture):
    path, _, count, guard = fixture
    session = module.PrivateStoreSession(path, path_guard=guard)
    session.__enter__()
    db = session.store.db
    PrivateInputStore(db)
    factory = session.receipt_factory
    entered, release = Event(), Event()

    def read():
        with factory() as reader:
            entered.set()
            assert release.wait(5)
            assert reader.connection.execute("SELECT 1").fetchone()[0] == 1

    with ThreadPoolExecutor() as pool:
        pending = pool.submit(read)
        assert entered.wait(5)
        try:
            with pytest.raises(ValueError, match="readers_active"):
                session.close()
            assert count[0] == 2
            assert db.execute("SELECT 1").fetchone()[0] == 1
            with pytest.raises(ValueError, match="not_available"):
                with factory():
                    pytest.fail("admitted during closing")
        finally:
            release.set()
            pending.result()
    assert count[0] == 1  # No auto cleanup on last worker exit.
    session.close()
    assert count[0] == 0
    session.close()


def test_store_initialization_failure_closes_opened_db_before_unpin(fixture, monkeypatch):
    path, _, count, guard = fixture
    captured = []

    def broken(store, path):
        assert count[0] == 1
        store.db = sqlite3.connect(path)
        captured.append(store.db)
        raise RuntimeError("fixture constructor failure")

    monkeypatch.setattr(module.Store, "__init__", broken)
    with pytest.raises(RuntimeError, match="fixture constructor"):
        with module.PrivateStoreSession(path, path_guard=guard):
            pytest.fail("yielded")
    assert count[0] == 0
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        captured[0].execute("SELECT 1")


def test_owner_thread_only_and_body_failure_cleanup(fixture):
    path, _, count, guard = fixture
    session = module.PrivateStoreSession(path, path_guard=guard)
    with pytest.raises(RuntimeError, match="fixture"):
        with session:
            with ThreadPoolExecutor() as pool:
                with pytest.raises(ValueError, match="owner_thread"):
                    pool.submit(session.close).result()
            assert count[0] == 1
            raise RuntimeError("fixture")
    assert count[0] == 0


def test_failed_store_close_retains_guard_for_explicit_retry(fixture, monkeypatch):
    path, _, count, guard = fixture
    session = module.PrivateStoreSession(path, path_guard=guard)
    session.__enter__()
    close = module.Store.close
    monkeypatch.setattr(
        module.Store, "close", lambda _: (_ for _ in ()).throw(RuntimeError("fixture"))
    )
    with pytest.raises(RuntimeError, match="fixture"):
        session.close()
    assert count[0] == 1
    monkeypatch.setattr(module.Store, "close", close)
    session.close()
    assert count[0] == 0


def test_reader_cleanup_failure_does_not_release_session_guard(fixture):
    path, _, count, guard = fixture
    session = module.PrivateStoreSession(path, path_guard=guard)
    session.__enter__()

    @contextmanager
    def broken_reader():
        yield object()
        raise RuntimeError("fixture close uncertain")

    session._factory = broken_reader  # Synthetic failure at the reader-close boundary.
    with pytest.raises(RuntimeError, match="close uncertain"):
        with session.receipt_factory():
            pass
    with pytest.raises(ValueError, match="cleanup_uncertain"):
        session.close()
    assert count[0] == 1
    with pytest.raises(ValueError, match="not_available"):
        _ = session.receipt_factory
    # Test-only disposal: fake reader above never opened a connection or child.
    session._cleanup_uncertain = False
    session.close()
