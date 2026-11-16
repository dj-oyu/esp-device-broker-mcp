"""Generated native state; simulated child uncertainty, no real broker activation."""

import sqlite3
import sys
from types import SimpleNamespace

import pytest
from test_private_state_windows_native import new_protected_directory

from cardputer_mcp.firmware_build import FirmwareBuild
from cardputer_mcp.private_artifact_store import PrivateArtifactStore
from cardputer_mcp.private_build_runtime import PrivateBuildRuntime
from cardputer_mcp.private_runtime_session import PrivateRuntimeSession
from cardputer_mcp.private_store_session import PrivateStoreSession

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="native Windows required")


def test_native_supervisor_refusal_retains_pin_until_explicit_join(tmp_path):
    root = new_protected_directory(tmp_path)
    path = root / "generated.db"
    path.touch()
    session = PrivateStoreSession(path)
    session.__enter__()
    db = session.store.db
    builder = FirmwareBuild(SimpleNamespace(build_projects={}))
    runtime = PrivateBuildRuntime(
        lambda *_: pytest.fail("producer must not run"), PrivateArtifactStore(db)
    )
    wrapper = PrivateRuntimeSession(builder, session, runtime)
    wrapper.__enter__()
    try:
        builder.process.uncertain = True  # Synthetic state, never a real child.
        with pytest.raises(ValueError, match="join_unconfirmed"):
            wrapper.close()
        with pytest.raises(ValueError, match="supervisor_active"):
            session.close()
        with pytest.raises(PermissionError):
            path.rename(root / "moved.db")
        with pytest.raises(PermissionError):
            root.rename(tmp_path / "moved-root")
        assert db.execute("SELECT 1").fetchone()[0] == 1
        builder.process.stop_requested.clear()
        with pytest.raises(ValueError, match="runtime_stopping"):
            runtime.begin_operation(builder, session, {})
        assert not runtime._accepting
    finally:
        builder.process.uncertain = False  # Fixture-only explicit reconciliation.
        wrapper.close()
    assert builder.private_runtime is None
    assert builder.private_store_factory is None
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        db.execute("SELECT 1")
    path.rename(root / "moved.db")
    root.rename(tmp_path / "moved-root")
