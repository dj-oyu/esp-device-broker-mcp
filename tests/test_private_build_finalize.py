import hashlib
import json
import sqlite3
import threading
from types import SimpleNamespace

import pytest

from cardputer_mcp.firmware_build import FirmwareBuild
from cardputer_mcp.private_artifact_store import PrivateArtifactStore
from cardputer_mcp.private_build_finalize import commit_private_candidate
from cardputer_mcp.store import Store


@pytest.fixture
def fixture(tmp_path):
    notifications = []
    store = Store(tmp_path / "synthetic.db", on_commit=lambda: notifications.append("commit"))
    artifacts = PrivateArtifactStore(store.db)
    plan = dict(
        job_id="fixture",
        project_id="fixture",
        test="firmware_build",
        build_principal="owner",
        private_input_id="fixture",
        private_input_receipt="a" * 64,
        private_binding_sha256="b" * 64,
        source_receipt="c" * 64,
        fingerprint="d" * 64,
    )
    payload = json.dumps(plan, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(payload.encode()).hexdigest()
    store.db.execute("INSERT INTO plans VALUES (?,?)", (digest, payload))
    store.add_job("fixture", digest)
    store.ledger.transition("fixture", "running")
    builder = SimpleNamespace(
        _lock=threading.RLock(),
        _committed_job=None,
        process=SimpleNamespace(active=False, uncertain=False, stop_requested=threading.Event()),
        _reprepare=lambda *_: dict(plan),
    )
    builder._check_cancelled = lambda: FirmwareBuild._check_cancelled(builder)
    args = dict(
        plan=plan,
        composition_identity="e" * 64,
        files=(("synthetic.bin", b"generated only"),),
        live=lambda: store.job("fixture")["state"] == "running",
    )
    notifications.clear()
    try:
        yield builder, store, artifacts, args, notifications
    finally:
        store.db.close()


def test_atomic_runtime_schema_commit_pin_audit_notification_and_stop(fixture):
    builder, store, artifacts, args, notifications = fixture
    receipt = commit_private_candidate(builder, store, artifacts, **args)
    assert store.job("fixture")["state"] == "succeeded"
    assert artifacts.resolve(receipt=receipt, principal="owner") == args["files"]
    assert (
        store.db.execute("SELECT event FROM execution_audit").fetchone()[0]
        == "private_build_committed"
    )
    assert artifacts.discard_unpinned(receipt=receipt, principal="owner") is False
    assert notifications == ["commit"]
    assert FirmwareBuild.request_stop(builder, "fixture") is False
    assert not builder.process.stop_requested.is_set()


@pytest.mark.parametrize("failure", ["stop", "authority", "child", "commit"])
def test_failed_finalization_preserves_running_and_rolls_back_everything(fixture, failure):
    builder, store, artifacts, args, notifications = fixture
    if failure == "stop":
        assert FirmwareBuild.request_stop(builder, "fixture")
    elif failure == "authority":
        args["live"] = lambda: False
    elif failure == "child":
        builder.process.active = True
    else:
        store.db.set_authorizer(
            lambda action, first, *rest: (
                sqlite3.SQLITE_DENY
                if action == sqlite3.SQLITE_TRANSACTION and first == "COMMIT"
                else sqlite3.SQLITE_OK
            )
        )
    with pytest.raises((ValueError, sqlite3.DatabaseError)):
        commit_private_candidate(builder, store, artifacts, **args)
    for table in (
        "private_artifact_receipts",
        "private_artifact_bytes",
        "private_artifact_pins",
        "execution_audit",
    ):
        assert store.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
    assert store.job("fixture")["state"] == "running"
    assert notifications == []
    assert builder._committed_job is None


def test_notification_failure_does_not_destroy_success(fixture):
    builder, store, artifacts, args, _ = fixture
    store.db.on_commit = lambda: (_ for _ in ()).throw(RuntimeError("synthetic"))
    receipt = commit_private_candidate(builder, store, artifacts, **args)
    assert artifacts.receipt_for_job(job_id="fixture", principal="owner") == receipt
    assert store.job("fixture")["state"] == "succeeded"


def test_reprepare_reentrant_stop_is_seen_before_commit(fixture):
    builder, store, artifacts, args, notifications = fixture

    def reprepare(*_):
        FirmwareBuild.request_stop(builder, "fixture")
        return dict(args["plan"])

    builder._reprepare = reprepare
    with pytest.raises(ValueError):
        commit_private_candidate(builder, store, artifacts, **args)
    assert store.job("fixture")["state"] == "running"
    assert store.db.execute("SELECT COUNT(*) FROM private_artifact_receipts").fetchone()[0] == 0
    assert notifications == []
