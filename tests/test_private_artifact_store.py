import hashlib
import json
import sqlite3
from contextlib import closing

import pytest

from cardputer_mcp.private_artifact_store import PrivateArtifactStore


def create(path, *, autocommit=True):
    db = sqlite3.connect(path, autocommit=autocommit)
    db.executescript("""
        CREATE TABLE IF NOT EXISTS plans(hash TEXT PRIMARY KEY,payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY,plan_hash TEXT NOT NULL,
        state TEXT NOT NULL,result TEXT,finished TEXT);""")
    store = PrivateArtifactStore(db)
    plan = {
        "job_id": "fixture-job",
        "project_id": "fixture",
        "test": "firmware_build",
        "build_principal": "owner",
        "private_input_id": "fixture",
        "private_input_receipt": "a" * 64,
        "private_binding_sha256": "b" * 64,
        "source_receipt": "c" * 64,
        "fingerprint": "d" * 64,
    }
    payload = json.dumps(plan, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(payload.encode()).hexdigest()
    db.execute("INSERT INTO plans VALUES (?,?)", (digest, payload))
    db.execute("INSERT INTO jobs VALUES (?,?, 'running',NULL,NULL)", (plan["job_id"], digest))
    db.commit()
    args = dict(
        plan=plan,
        principal="owner",
        composition_identity="e" * 64,
        files=(("firmware.bin", b"generated fixture only"),),
        guard=lambda: True,
    )
    return db, store, args


def test_success_and_receipt_survive_reopen(tmp_path):
    path = tmp_path / "fixture.db"
    db, store, args = create(path)
    receipt = store.publish(**args)
    assert db.in_transaction is False
    assert db.execute("SELECT state FROM jobs").fetchone()[0] == "succeeded"
    db.close()
    db = sqlite3.connect(path, autocommit=True)
    try:
        reopened = PrivateArtifactStore(db)
        assert reopened.receipt_for_job(job_id="fixture-job", principal="owner") == receipt
        assert reopened.resolve(receipt=receipt, principal="owner") == args["files"]
        with pytest.raises(ValueError, match="not_owned"):
            reopened.resolve(receipt=receipt, principal="other")
        db.execute("UPDATE private_artifact_bytes SET payload=?", (b"tampered",))
        with pytest.raises(ValueError, match="bytes_changed"):
            reopened.resolve(receipt=receipt, principal="owner")
    finally:
        db.close()


@pytest.mark.parametrize("checks", [(False,), (True, False)])
def test_cancelled_final_guard_rolls_back_job_and_artifacts(tmp_path, checks):
    db, store, args = create(tmp_path / "fixture.db")
    try:
        checks = iter(checks)
        with pytest.raises(ValueError, match="commit_denied"):
            store.publish(**(args | {"guard": lambda: next(checks)}))
        assert db.execute("SELECT state FROM jobs").fetchone()[0] == "running"
        assert db.execute("SELECT COUNT(*) FROM private_artifact_receipts").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM private_artifact_bytes").fetchone()[0] == 0
        assert db.in_transaction is False
    finally:
        db.close()


@pytest.mark.parametrize("mode", [True, sqlite3.LEGACY_TRANSACTION_CONTROL])
def test_commit_failure_never_returns_success_and_reopen_is_empty(tmp_path, mode):
    path = tmp_path / "fixture.db"
    db, store, args = create(path, autocommit=mode)

    def authorizer(action, first, *rest):
        return (
            sqlite3.SQLITE_DENY
            if action == sqlite3.SQLITE_TRANSACTION and first == "COMMIT"
            else sqlite3.SQLITE_OK
        )

    db.set_authorizer(authorizer)
    with pytest.raises(sqlite3.DatabaseError):
        store.publish(**args)
    db.close()
    with closing(sqlite3.connect(path)) as reopened:
        assert reopened.execute("SELECT state FROM jobs").fetchone()[0] == "running"
        assert reopened.execute("SELECT COUNT(*) FROM private_artifact_receipts").fetchone()[0] == 0


def test_wrong_plan_and_active_transaction_rejected(tmp_path):
    db, store, args = create(tmp_path / "fixture.db")
    try:
        with pytest.raises(ValueError):
            store.publish(**(args | {"plan": args["plan"] | {"fingerprint": "f" * 64}}))
        db.execute("BEGIN")
        with pytest.raises(ValueError, match="transaction_active"):
            store.publish(**args)
        assert db.in_transaction
        db.execute("ROLLBACK")
    finally:
        db.close()


@pytest.mark.parametrize("reference", ["active:synthetic", "recovery:synthetic"])
def test_retention_never_deletes_pinned_artifact(tmp_path, reference):
    db, store, args = create(tmp_path / "fixture.db")
    try:
        receipt = store.publish(**args)
        store.pin(receipt=receipt, principal="owner", reference=reference)
        assert store.discard_unpinned(receipt=receipt, principal="owner") is False
        assert store.resolve(receipt=receipt, principal="owner") == args["files"]
    finally:
        db.close()


def test_unpinned_retention_is_owner_scoped(tmp_path):
    db, store, args = create(tmp_path / "fixture.db")
    try:
        receipt = store.publish(**args)
        with pytest.raises(ValueError, match="not_owned"):
            store.discard_unpinned(receipt=receipt, principal="other")
        assert store.discard_unpinned(receipt=receipt, principal="owner") is True
        with pytest.raises(ValueError, match="unavailable"):
            store.resolve(receipt=receipt, principal="owner")
        assert db.execute("SELECT state FROM jobs").fetchone()[0] == "succeeded"
    finally:
        db.close()


def test_artifact_limits_and_path_collisions_leave_job_running(tmp_path):
    db, store, args = create(tmp_path / "fixture.db")
    try:
        for files in ((), (("../escape", b"x"),), (("a", b"x"), ("A", b"y"))):
            with pytest.raises(ValueError):
                store.publish(**(args | {"files": files}))
        assert db.execute("SELECT state FROM jobs").fetchone()[0] == "running"
    finally:
        db.close()
