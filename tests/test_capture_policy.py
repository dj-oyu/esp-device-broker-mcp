"""Retired grants cannot authorize an operation or mutate historical policy data."""

import importlib
import sqlite3

from cardputer_mcp import capture_policy
from cardputer_mcp.store import Store


def test_capture_policy_and_arming_have_no_executable_surface():
    for name in (
        "CapturePolicies",
        "CaptureAuthorization",
        "CapturePolicyError",
        "HOME_ATTESTATION",
        "_Policy",
        "_Arm",
    ):
        assert not hasattr(capture_policy, name)


def test_policy_history_is_preserved_without_loading_or_reusing_grants(tmp_path):
    path = tmp_path / "history.sqlite3"
    payload = '{"capture_home":true,"image_delivery":true,"mode":"persistent"}'
    with sqlite3.connect(path) as db:
        db.executescript("""
            CREATE TABLE capture_policies(policy_id TEXT PRIMARY KEY,payload TEXT NOT NULL);
            CREATE TABLE capture_policy_audit(
                id INTEGER PRIMARY KEY,event TEXT NOT NULL,at REAL NOT NULL,policy_id TEXT);
        """)
        db.execute("INSERT INTO capture_policies VALUES(?,?)", ("legacy-policy", payload))
        db.execute(
            "INSERT INTO capture_policy_audit VALUES(1,'policy_created',1000,'legacy-policy')"
        )
    importlib.reload(capture_policy)
    store = Store(path)
    try:
        store.recover()
        assert tuple(store.db.execute("SELECT * FROM capture_policies").fetchone()) == (
            "legacy-policy",
            payload,
        )
        assert tuple(store.db.execute("SELECT * FROM capture_policy_audit").fetchone()) == (
            1,
            "policy_created",
            1000.0,
            "legacy-policy",
        )
        assert store.db.execute("SELECT count(*) FROM execution_requests").fetchone()[0] == 0
        assert store.db.execute("SELECT count(*) FROM jobs").fetchone()[0] == 0
    finally:
        store.close()
