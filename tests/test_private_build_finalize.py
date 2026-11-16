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


def test_firmware_deferred_private_runtime_end_to_end(fixture):
    from cardputer_mcp.private_build_runtime import PrivateBuildCandidate, PrivateBuildRuntime

    _, store, artifacts, args, notifications = fixture
    builder = FirmwareBuild(SimpleNamespace(build_projects={}))
    builder._reprepare = lambda *_: dict(args["plan"])

    def produce(plan, logs):
        assert plan == args["plan"]
        logs.append("synthetic private source text must not escape")
        return PrivateBuildCandidate("e" * 64, args["files"])

    builder.private_runtime = PrivateBuildRuntime(produce, artifacts)
    result = builder._execute(args["plan"], lambda _: None, defer_publish=True)
    assert result == "private_build_succeeded_no_flash"
    assert store.job("fixture")["state"] == "running"
    receipt = builder.private_runtime.commit(builder, store, args["plan"], args["live"])
    assert artifacts.resolve(receipt=receipt, principal="owner") == args["files"]
    assert store.job("fixture")["state"] == "succeeded"
    assert builder.private_runtime.pending == {}
    assert notifications == ["commit"]
    assert builder.request_stop("fixture") is False


def test_default_runtime_gate_and_device_path_remain_closed(fixture):
    from cardputer_mcp.private_build_runtime import PrivateBuildRuntime

    _, _, artifacts, args, _ = fixture
    builder = FirmwareBuild(SimpleNamespace(build_projects={}))
    for installed in (False, True):
        if installed:
            builder.private_runtime = PrivateBuildRuntime(
                lambda *_: pytest.fail("must not run"), artifacts
            )
        with pytest.raises(ValueError, match="private_build_workspace_unavailable"):
            builder._execute(args["plan"], lambda _: None, defer_publish=False)
    builder.private_runtime = None
    with pytest.raises(ValueError, match="private_build_workspace_unavailable"):
        builder._execute(args["plan"], lambda _: None, defer_publish=True)


def test_private_runtime_cancel_and_discard_never_publish(fixture):
    from cardputer_mcp.private_build_runtime import PrivateBuildCandidate, PrivateBuildRuntime

    _, store, artifacts, args, notifications = fixture
    builder = FirmwareBuild(SimpleNamespace(build_projects={}))
    builder._reprepare = lambda *_: dict(args["plan"])
    builder.private_runtime = PrivateBuildRuntime(
        lambda *_: PrivateBuildCandidate("e" * 64, args["files"]), artifacts
    )
    builder._execute(args["plan"], lambda _: None, defer_publish=True)
    assert builder.request_stop("fixture")
    with pytest.raises(ValueError):
        builder.private_runtime.commit(builder, store, args["plan"], args["live"])
    builder.discard_deferred("fixture")
    assert builder.private_runtime.pending == {}
    assert store.job("fixture")["state"] == "running"
    assert notifications == []


def test_stop_during_first_reprepare_never_starts_private_producer(fixture):
    from cardputer_mcp.private_build_runtime import PrivateBuildRuntime

    _, _, artifacts, args, _ = fixture
    builder = FirmwareBuild(SimpleNamespace(build_projects={}))

    def prepare(*_):
        assert builder.request_stop("fixture")
        return dict(args["plan"])

    builder._reprepare = prepare
    builder.private_runtime = PrivateBuildRuntime(
        lambda *_: pytest.fail("producer started"), artifacts
    )
    with pytest.raises(ValueError):
        builder._execute(args["plan"], lambda _: None, defer_publish=True)
    assert not builder.private_runtime.running
    assert builder.private_runtime.pending == {}
