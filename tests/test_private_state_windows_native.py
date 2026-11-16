"""Native generated state only; never mutate an existing ACL or broker database."""

import sys
from contextlib import ExitStack, closing

import pytest

from cardputer_mcp.private_input_windows import _root_parts
from cardputer_mcp.private_state_windows import verified_private_database
from cardputer_mcp.private_workspace_windows import _Scratch
from cardputer_mcp.store import Store

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="native Windows required")


def new_protected_directory(container):
    native = _Scratch()
    parts = _root_parts(str(container))
    with ExitStack() as held:
        parent = native.anchor(parts[0], held)
        for part in parts[1:]:
            parent = native.child(parent, part, True, held)
        handle = native.open_object(parent, "protected", True, create=True)
        held.callback(native.close, handle)
        native.verify_acl(handle)
    return container / "protected"


def test_native_sqlite_reads_writes_and_no_delete_pin(tmp_path):
    root = new_protected_directory(tmp_path)
    path = root / "state.sqlite3"
    store = Store(path)
    with closing(store.db):
        store.db.execute("PRAGMA journal_mode=WAL")
        store.db.execute("CREATE TABLE native_fixture (value TEXT)")
        store.db.commit()
        assert path.with_name(path.name + "-wal").exists()
        assert path.with_name(path.name + "-shm").exists()
        with verified_private_database(store):
            store.db.execute("INSERT INTO native_fixture VALUES ('synthetic')")
            store.db.commit()
            assert store.db.execute("SELECT value FROM native_fixture").fetchone()[0] == "synthetic"
            with pytest.raises(PermissionError):
                path.rename(root / "renamed.sqlite3")
            with pytest.raises(PermissionError):
                root.rename(tmp_path / "moved")
        assert store.db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_native_unprotected_parent_rejected(tmp_path):
    store = Store(tmp_path / "state.sqlite3")
    with closing(store.db):
        with pytest.raises(ValueError, match="private_workspace_acl_"):
            with verified_private_database(store):
                pytest.fail("unprotected parent accepted")
