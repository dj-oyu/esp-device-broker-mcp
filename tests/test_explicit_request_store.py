"""Durable intent is a new client request, never a converted legacy approval."""

import hashlib
import json
import sqlite3

import pytest

from cardputer_mcp.store import Store


def plan(store, job="new-job", **fields):
    payload = {"job_id": job, "test": "capture_home", **fields}
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    store.put_plan(digest, payload)
    return digest


def legacy(path):
    db = sqlite3.connect(path)
    db.executescript("""
        CREATE TABLE plans(hash TEXT PRIMARY KEY,payload TEXT NOT NULL);
        CREATE TABLE approvals(hash TEXT PRIMARY KEY);
        CREATE TABLE jobs(id TEXT PRIMARY KEY,plan_hash TEXT NOT NULL,state TEXT NOT NULL,
            stage TEXT,result TEXT,diagnostic TEXT,reviewed INTEGER NOT NULL DEFAULT 0,
            created TEXT DEFAULT CURRENT_TIMESTAMP,finished TEXT);
        INSERT INTO approvals VALUES('historical-grant');
    """)
    for state in ("awaiting_approval", "queued", "running", "succeeded", "needs_review"):
        db.execute(
            "INSERT INTO jobs(id,plan_hash,state,result,diagnostic) VALUES(?,?,?,?,?)",
            (state, "old-plan", state, "old-result", '{"kept":true}'),
        )
    # A stale historical review cannot settle a later in-flight physical action.
    db.execute("UPDATE jobs SET reviewed=1 WHERE id='running'")
    db.commit()
    db.close()


def test_migration_never_replays_or_invents_an_execution_request(tmp_path):
    path = tmp_path / "history.sqlite3"
    legacy(path)
    store = Store(path)
    try:
        for job in ("awaiting_approval", "queued"):
            row = store.job(job)
            assert row["state"] == "cancelled"
            assert row["result"] == "explicit_requests_migration_no_replay"
            assert row["diagnostic"] == {"kept": True}
        assert store.job("running")["state"] == "needs_review"
        assert store.job("running")["reviewed"] == 0
        assert store.job("succeeded")["result"] == "old-result"
        assert store.job("needs_review")["result"] == "old-result"
        assert store.db.execute("SELECT hash FROM approvals").fetchone()[0] == "historical-grant"
        assert store.db.execute("SELECT count(*) FROM execution_requests").fetchone()[0] == 0
        assert store.db.execute("SELECT count(*) FROM execution_audit").fetchone()[0] == 3
        digest = plan(store)
        store.enqueue_requested("new-job", digest, "client")
        store.migrate_explicit_requests()
        assert store.job("new-job")["state"] == "queued"
        assert store.db.execute("SELECT count(*) FROM execution_audit").fetchone()[0] == 4
        for action in (
            lambda: store.approve(digest),
            lambda: store.approved_enqueue("new-job", digest, True, awaiting_approval=True),
            lambda: store.add_awaiting_job("another-job", digest),
        ):
            with pytest.raises(ValueError, match="approval_interface_retired"):
                action()
        assert store.job("new-job")["state"] == "queued"
    finally:
        store.close()
    reopened = Store(path)
    try:
        assert reopened.job("new-job")["state"] == "queued"
        reopened.recover()
        assert reopened.job("new-job")["state"] == "cancelled"
        assert reopened.job("running")["reviewed"] == 0
    finally:
        reopened.close()


def test_explicit_enqueue_commits_identity_and_audit_without_approval(tmp_path):
    store = Store(tmp_path / "history.sqlite3")
    try:
        digest = plan(store)
        store.db.execute("INSERT INTO approvals VALUES(?)", (digest,))
        store.db.commit()
        store.enqueue_requested("new-job", digest, "requesting-client")
        assert store.job("new-job")["state"] == "queued"
        row = store.db.execute("SELECT * FROM execution_requests").fetchone()
        assert (row["job_id"], row["plan_hash"], row["principal_id"]) == (
            "new-job",
            digest,
            "requesting-client",
        )
        assert (
            store.db.execute("SELECT event FROM execution_audit").fetchone()[0]
            == "client_requested"
        )
        assert store.db.execute("SELECT count(*) FROM approvals").fetchone()[0] == 1
        with pytest.raises(sqlite3.IntegrityError):
            store.enqueue_requested("new-job", digest, "other-client")
        assert store.db.execute("SELECT count(*) FROM execution_audit").fetchone()[0] == 1
        assert (
            store.db.execute("SELECT principal_id FROM execution_requests").fetchone()[0]
            == "requesting-client"
        )
    finally:
        store.close()


def test_enqueue_rolls_back_when_audit_cannot_be_saved(tmp_path):
    store = Store(tmp_path / "history.sqlite3")
    try:
        digest = plan(store)
        store.db.executescript("""CREATE TRIGGER fail_audit BEFORE INSERT ON execution_audit
            BEGIN SELECT RAISE(ABORT,'synthetic audit failure'); END;""")
        with pytest.raises(sqlite3.IntegrityError):
            store.enqueue_requested("new-job", digest, "client")
        assert store.job("new-job") is None
        assert store.db.execute("SELECT count(*) FROM execution_requests").fetchone()[0] == 0
        assert store.db.execute("SELECT count(*) FROM execution_audit").fetchone()[0] == 0
    finally:
        store.close()


def test_bad_or_foreign_plan_cannot_create_a_request(tmp_path):
    store = Store(tmp_path / "history.sqlite3")
    try:
        digest = plan(store, principal_id="original-client")
        for job, requested_digest, client in (
            ("different-job", digest, "original-client"),
            ("new-job", digest, "different-client"),
            ("new-job", digest, ""),
            ("new-job", "0" * 64, "original-client"),
        ):
            with pytest.raises(ValueError, match="explicit_request_invalid"):
                store.enqueue_requested(job, requested_digest, client)
        assert store.db.execute("SELECT count(*) FROM jobs").fetchone()[0] == 0
    finally:
        store.close()


def test_migration_failure_rolls_back_cancellation_audit_and_marker(tmp_path):
    path = tmp_path / "history.sqlite3"
    legacy(path)
    with sqlite3.connect(path) as db:
        db.executescript("""CREATE TRIGGER fail_cancel BEFORE UPDATE ON jobs
            BEGIN SELECT RAISE(ABORT,'synthetic update failure'); END;""")
    with pytest.raises(sqlite3.IntegrityError):
        Store(path)
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT state FROM jobs WHERE id='queued'").fetchone()[0] == "queued"
        assert db.execute("SELECT count(*) FROM execution_audit").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM operation_migrations").fetchone()[0] == 0
