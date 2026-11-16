"""Generated SQLite/thread fixtures; native pin identity is tested separately."""

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from cardputer_mcp import private_receipt_factory as module
from cardputer_mcp.private_input_store import PrivateInputStore
from cardputer_mcp.private_state_windows import verified_private_database_path


@pytest.fixture
def fixture(tmp_path):
    path = tmp_path / "generated #.db"
    db = sqlite3.connect(path)
    PrivateInputStore(db)
    events = []
    current = [(1, b"synthetic-file-id")]

    @contextmanager
    def guard(actual):
        assert actual == path
        events.append("pin")
        try:
            yield current[0]
        finally:
            events.append("unpin")

    factory = module.PrivateReceiptFactory.capture(SimpleNamespace(db=db), path_guard=guard)
    try:
        yield factory, db, events, current
    finally:
        db.close()


def test_default_native_and_capture_not_connection_handle_proof():
    assert module.PrivateReceiptFactory.capture.__func__.__kwdefaults__["path_guard"] is (
        verified_private_database_path
    )
    assert "replaced before capture" in module.PrivateReceiptFactory.__doc__


def test_worker_uses_own_ro_connection_and_closes_before_unpin(fixture):
    factory, main, events, _ = fixture
    observed = []

    def worker():
        with factory() as reader:
            observed.append(reader.connection)
            assert events[-1] == "pin"
            assert reader.connection is not main
            assert not reader.connection.in_transaction
            with pytest.raises(sqlite3.OperationalError, match="readonly"):
                reader.connection.execute("CREATE TABLE forbidden(x)")
            with pytest.raises(sqlite3.OperationalError, match="readonly"):
                reader.connection.execute("INSERT INTO private_input_receipts VALUES('x','x')")
            assert (
                reader.connection.execute("SELECT COUNT(*) FROM private_input_receipts").fetchone()[
                    0
                ]
                == 0
            )
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            observed[-1].execute("SELECT 1")

    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(worker).result()
        pool.submit(worker).result()
    assert observed[0] is not observed[1]
    assert events == ["pin", "unpin"] * 3
    assert main.execute("SELECT COUNT(*) FROM private_input_receipts").fetchone()[0] == 0


def test_replacement_rejected_before_sqlite_connect(fixture, monkeypatch):
    factory, _, _, current = fixture
    current[0] = (1, b"replacement")
    monkeypatch.setattr(module.sqlite3, "connect", lambda *a, **k: pytest.fail("connected"))
    with pytest.raises(ValueError, match="database_changed"):
        with factory():
            pytest.fail("yielded")


def test_missing_schema_no_initialization(fixture):
    factory, db, events, _ = fixture
    db.execute("DROP TABLE private_input_receipts")
    with pytest.raises(sqlite3.OperationalError, match="no such table"):
        with factory():
            pytest.fail("yielded")
    assert (
        db.execute("SELECT name FROM sqlite_master WHERE name='private_input_receipts'").fetchall()
        == []
    )
    assert events[-1] == "unpin"


def test_exception_closes_connection(fixture):
    factory, _, events, _ = fixture
    captured = []
    with pytest.raises(RuntimeError, match="fixture"):
        with factory() as reader:
            captured.append(reader.connection)
            raise RuntimeError("fixture")
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        captured[0].execute("SELECT 1")
    assert events[-1] == "unpin"


def test_connect_path_mismatch_closes_and_fails(fixture, monkeypatch):
    factory, _, events, _ = fixture
    monkeypatch.setattr(module, "private_database_path", lambda _: factory.path.with_name("other"))
    with pytest.raises(ValueError, match="database_changed"):
        with factory():
            pytest.fail("yielded")
    assert events[-1] == "unpin"

