"""New generated databases only; native pin spans main Store open and lifetime."""

import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing

import pytest
from test_private_state_windows_native import new_protected_directory

from cardputer_mcp.private_input_store import PrivateInputStore
from cardputer_mcp.private_receipt_factory import PrivateReceiptFactory
from cardputer_mcp.private_state_windows import verified_private_database_path
from cardputer_mcp.store import Store

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="native Windows required")


def test_native_thread_factory_readonly_identity_and_lifetime(tmp_path):
    root = new_protected_directory(tmp_path)
    path = root / "generated #.db"
    path.touch()
    with verified_private_database_path(path) as identity:
        store = Store(path)
        with closing(store.db):
            PrivateInputStore(store.db)
            factory = PrivateReceiptFactory.capture(store)
            assert factory.identity == identity
            observed = []

            def worker():
                with factory() as reader:
                    observed.append(reader.connection)
                    assert reader.connection is not store.db
                    with pytest.raises(sqlite3.OperationalError, match="readonly"):
                        reader.connection.execute("CREATE TABLE forbidden(x)")
                    with pytest.raises(PermissionError):
                        path.rename(root / "moved.db")
                    assert (
                        reader.connection.execute(
                            "SELECT COUNT(*) FROM private_input_receipts"
                        ).fetchone()[0]
                        == 0
                    )
                with pytest.raises(sqlite3.ProgrammingError, match="closed"):
                    observed[-1].execute("SELECT 1")

            with ThreadPoolExecutor(max_workers=1) as pool:
                pool.submit(worker).result(timeout=10)
                pool.submit(worker).result(timeout=10)
            assert observed[0] is not observed[1]
    # Actual file replacement only after every generated handle and pin is closed.
    path.rename(root / "old.db")
    replacement = Store(path)
    with closing(replacement.db):
        PrivateInputStore(replacement.db)
        with pytest.raises(ValueError, match="database_changed"):
            with factory():
                pytest.fail("replacement identity accepted")

