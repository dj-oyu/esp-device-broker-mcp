"""Offline history archive: references stay live, old finished rows move; no hardware."""

import hashlib
import json
import sqlite3
import sys

import pytest
from filelock import FileLock

from cardputer_mcp import cli
from cardputer_mcp.history_archive import HistoryArchiveError, archive_history
from cardputer_mcp.job_logs import DATABASE_NAME, JobLogStore
from cardputer_mcp.store import ARCHIVE_TABLES, Store

PRINCIPAL = "ephemeral:local_cli"


def digest(plan):
    return hashlib.sha256(
        json.dumps(plan, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def add(store, job_id, state="succeeded", *, reviewed=0, plan=None, register=True):
    plan = {"version": 1, "job_id": job_id, "test": "diagnostics", **(plan or {})}
    plan_hash = digest(plan)
    store.put_plan(plan_hash, plan)
    if register:
        store.add_job(job_id, plan_hash)
        store.set_job(job_id, state, "synthetic")
        store.db.execute("UPDATE jobs SET reviewed=? WHERE id=?", (reviewed, job_id))
        store.db.execute(
            "INSERT INTO execution_requests(job_id,plan_hash,principal_id) VALUES(?,?,?)",
            (job_id, plan_hash, PRINCIPAL),
        )
        store.db.commit()
    return plan_hash


def build_reference(store, job_id, slot):
    store.db.execute(
        "INSERT INTO build_artifact_references VALUES(?,?,?)",
        (
            job_id,
            PRINCIPAL,
            json.dumps({"project_id": "app", "fingerprint": slot, "environment_fingerprint": "e"}),
        ),
    )
    store.db.commit()


def history(state):
    """A store with every kind of reference the archive must keep."""
    state.mkdir()
    store = Store(state / "history.sqlite3")
    for index in range(5):
        add(store, f"old-{index}")
    add(store, "reviewed-review", "needs_review", reviewed=1)
    add(store, "open-review", "needs_review", reviewed=0)
    add(store, "still-running", "running")
    add(store, "fetch", plan={"test": "firmware_fetch_source", "source_principal": PRINCIPAL})
    store.db.execute(
        "INSERT INTO source_publications VALUES(?,?,?,?)", ("a" * 64, "fetch", "app", PRINCIPAL)
    )
    add(store, "build-old", plan={"test": "firmware_build", "build_principal": PRINCIPAL})
    build_reference(store, "build-old", "slot-1")
    add(store, "build-new", plan={"test": "firmware_build", "build_principal": PRINCIPAL})
    build_reference(store, "build-new", "slot-1")
    add(store, "build-flashed", plan={"test": "firmware_build", "build_principal": PRINCIPAL})
    build_reference(store, "build-flashed", "slot-2")
    add(store, "build-flashed-newer", plan={"test": "firmware_build"})
    build_reference(store, "build-flashed-newer", "slot-2")
    add(store, "backup", plan={"test": "firmware_backup", "flash_principal": PRINCIPAL})
    add(
        store,
        "flash",
        plan={
            "test": "firmware_flash",
            "flash_principal": PRINCIPAL,
            "build_job_id": "build-flashed",
            "backup_job_id": "backup",
        },
    )
    store.db.execute(
        "INSERT INTO firmware_lkg VALUES(?,?,?,?,?,?)",
        ("device", PRINCIPAL, "b" * 64, "artifact_bundle", "backup", "flash"),
    )
    store.db.execute(
        "INSERT INTO firmware_lkg_history VALUES(?,?,?,?,?,?)",
        ("b" * 64, "device", PRINCIPAL, "artifact_bundle", "backup", "flash"),
    )
    add(store, "chain-parent")
    add(store, "chain-child", "queued", plan={"continues": {"parent_job_id": "chain-parent"}})
    add(store, "derby-old")
    store.db.execute(
        "INSERT INTO firmware_job_summaries VALUES(?,?,?)", ("derby-old", PRINCIPAL, "{}")
    )
    add(store, "never-executed", register=False)
    for request_id, request_state, reviewed in (
        ("request-old", "released", 0),
        ("request-waiting", "waiting", 0),
        ("request-review", "needs_review", 0),
        ("request-reviewed", "needs_review", 1),
    ):
        store.db.execute(
            "INSERT INTO device_requests(id,session_id,state,lease_seconds,reviewed) "
            "VALUES(?,?,?,?,?)",
            (request_id, f"work-{request_id}", request_state, 30, reviewed),
        )
        store.db.execute(
            "INSERT INTO work_sessions(id,epoch,owner,principal_id,scopes,state) "
            "VALUES(?,?,?,?,?,?)",
            (f"work-{request_id}", "e" * 32, "local_cli", PRINCIPAL, "[]", "closed"),
        )
    store.db.commit()
    logs = JobLogStore(state / "job-logs")
    for job_id in ("old-0", "open-review"):
        log = logs.begin(job_id, PRINCIPAL, "device")
        log.append("device", f"line for {job_id}")
        log.close()
    logs.close()
    for job_id in ("old-1", "flash"):
        (state / "captures" / job_id).mkdir(parents=True)
        (state / "captures" / job_id / "capture.png").write_bytes(b"png")
    store.close()


def logged_jobs(folder):
    db = sqlite3.connect(folder / DATABASE_NAME)
    try:
        return {row[0] for row in db.execute("SELECT job_id FROM log_jobs")}
    finally:
        db.close()


def ids(state, table="jobs", column="id"):
    db = sqlite3.connect(state / "history.sqlite3")
    try:
        return {row[0] for row in db.execute(f"SELECT {column} FROM {table}")}
    finally:
        db.close()


KEPT = {
    "open-review",
    "still-running",
    "fetch",
    "build-new",
    "build-flashed",
    "build-flashed-newer",
    "backup",
    "flash",
    "chain-parent",
    "chain-child",
}
ARCHIVED = {f"old-{index}" for index in range(5)} | {"reviewed-review", "build-old", "derby-old"}


def test_archive_keeps_every_reference_and_moves_old_finished_rows(tmp_path):
    state = tmp_path / "state"
    history(state)
    before = {job_id: row for job_id, row in rows(state).items() if job_id in KEPT}
    result = archive_history(state, keep_latest=0)
    assert result["ok"] and not result["dry_run"] and result["replayed"] is False
    assert ids(state) == KEPT
    assert {job_id: row for job_id, row in rows(state).items()} == before
    assert result["archive"]["jobs"] == len(ARCHIVED)
    assert ids(state, "device_requests") == {"request-waiting", "request-review"}
    assert ids(state, "work_sessions") == {"work-request-waiting", "work-request-review"}
    assert ids(state, "firmware_lkg", "completed_job") == {"flash"}
    assert ids(state, "source_publications", "job_id") == {"fetch"}
    assert ids(state, "build_artifact_references", "job_id") == {
        "build-new",
        "build-flashed",
        "build-flashed-newer",
    }
    assert ids(state, "execution_requests", "job_id") == KEPT
    assert "never-executed" not in ids(state, "timeline_order", "job_id")
    assert all(json.loads(p)["job_id"] in KEPT for p in ids(state, "plans", "payload"))

    archive = state / result["archive_directory"]
    assert archive.parent.name == "history-archive"
    db = sqlite3.connect(archive / "history.sqlite3")
    try:
        assert {row[0] for row in db.execute("SELECT id FROM jobs")} == ARCHIVED
        assert {row[0] for row in db.execute("SELECT job_id FROM firmware_job_summaries")} == {
            "derby-old"
        }
        assert {row[0] for row in db.execute("SELECT id FROM device_requests")} == {
            "request-old",
            "request-reviewed",
        }
        archived_plans = {
            json.loads(row[0])["job_id"] for row in db.execute("SELECT payload FROM plans")
        }
        assert archived_plans == ARCHIVED | {"never-executed"}
        info = dict(db.execute("SELECT key,value FROM archive_info"))
        assert info["keep_latest"] == "0"
    finally:
        db.close()
    # Durable files move with their job; kept jobs still read theirs.
    assert logged_jobs(archive / "job-logs") == {"old-0"}
    assert logged_jobs(state / "job-logs") == {"open-review"}
    assert (archive / "captures" / "old-1" / "capture.png").is_file()
    assert (state / "captures" / "flash" / "capture.png").is_file()
    reader = JobLogStore(state / "job-logs")
    kept_log = reader.read("open-review", PRINCIPAL, "device")
    reader.close()
    assert kept_log is not None and "line for open-review" in json.dumps(kept_log)
    store = Store(state / "history.sqlite3")
    try:
        assert store.needs_review()
        assert store.job("open-review")["reviewed"] == 0
    finally:
        store.close()
    # Nothing more to move: a second run creates no archive.
    again = archive_history(state, keep_latest=0)
    assert again["archive"]["jobs"] == 0 and again["archive_directory"] is None
    # A later job that reuses an archived ID starts a fresh log, not the old one.
    reuse = JobLogStore(state / "job-logs")
    log = reuse.begin("old-0", "someone-else", "device")
    log.close()
    assert reuse.read("old-0", "someone-else", "device")["entries"] == []
    reuse.close()
    # A log no live job owns (e.g. left by an interrupted run) moves on the next run.
    swept = archive_history(state, keep_latest=0)
    assert swept["archive"] == {
        "jobs": 0,
        "plans": 0,
        "device_requests": 0,
        "work_sessions": 0,
        "job_logs": 1,
        "captures": 0,
    }
    assert logged_jobs(state / swept["archive_directory"] / "job-logs") == {"old-0"}


def rows(state):
    db = sqlite3.connect(state / "history.sqlite3")
    db.row_factory = sqlite3.Row
    try:
        return {row["id"]: dict(row) for row in db.execute("SELECT * FROM jobs")}
    finally:
        db.close()


def test_dry_run_reports_counts_and_changes_nothing(tmp_path):
    state = tmp_path / "state"
    history(state)
    before = {
        table: sorted(
            map(tuple, sqlite3.connect(state / "history.sqlite3").execute(f"SELECT * FROM {table}"))
        )
        for table, _, _ in ARCHIVE_TABLES
    }
    result = archive_history(state, keep_latest=0, dry_run=True)
    assert result["dry_run"] and result["archive_directory"] is None
    assert result["archive"] == {
        "jobs": len(ARCHIVED),
        "plans": len(ARCHIVED) + 1,
        "device_requests": 2,
        "work_sessions": 2,
        "job_logs": 1,
        "captures": 1,
    }
    assert result["kept"]["jobs"] == len(KEPT)
    assert result["protection"]["unreviewed_needs_review"] == 1
    assert not (state / "history-archive").exists()
    after = {
        table: sorted(
            map(tuple, sqlite3.connect(state / "history.sqlite3").execute(f"SELECT * FROM {table}"))
        )
        for table, _, _ in ARCHIVE_TABLES
    }
    assert after == before
    assert logged_jobs(state / "job-logs") == {"old-0", "open-review"}


def test_keep_latest_keeps_the_newest_registrations(tmp_path):
    state = tmp_path / "state"
    history(state)
    archive_history(state, keep_latest=3)
    remaining = ids(state)
    # The newest three registrations: chain-child, derby-old and never-executed's plan.
    assert {"derby-old", "chain-child"} <= remaining
    assert "old-0" not in remaining
    assert "never-executed" in {json.loads(p)["job_id"] for p in ids(state, "plans", "payload")}


def test_refuses_while_the_broker_holds_the_state_lock(tmp_path):
    state = tmp_path / "state"
    history(state)
    lock = FileLock(str(state / "broker.lockfile"))
    lock.acquire()
    try:
        with pytest.raises(HistoryArchiveError, match="stop_broker_before_history_archive"):
            archive_history(state, keep_latest=0)
    finally:
        lock.release()
    assert "old-0" in ids(state)


def test_refusals_for_missing_state_and_bad_window(tmp_path):
    with pytest.raises(HistoryArchiveError, match="broker_state_not_found"):
        archive_history(tmp_path / "missing")
    state = tmp_path / "state"
    history(state)
    for value in (-1, 10001):
        with pytest.raises(HistoryArchiveError, match="history_archive_keep_latest_invalid"):
            archive_history(state, keep_latest=value)


def test_unreadable_plan_is_never_archived(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    store = Store(state / "history.sqlite3")
    store.db.execute("INSERT INTO plans VALUES(?,?)", ("c" * 64, "not json"))
    store.db.execute(
        "INSERT INTO jobs(id,plan_hash,state) VALUES('broken',?,'succeeded')", ("c" * 64,)
    )
    store.db.commit()
    store.close()
    archive_history(state, keep_latest=0)
    assert ids(state) == {"broken"}


def test_cli_archive_prints_the_report(tmp_path, monkeypatch, capsys):
    state = tmp_path / "state"
    history(state)
    argv = ["cardputer", "archive", "--state-dir", str(state), "--dry-run"]
    monkeypatch.setattr(sys, "argv", argv)
    cli.main()
    report = json.loads(capsys.readouterr().out)
    # The default window keeps the newest 1000 registrations: nothing moves here.
    assert report["dry_run"] is True and report["archive"]["jobs"] == 0
    monkeypatch.setattr(sys, "argv", [*argv, "--keep-latest", "0"])
    cli.main()
    assert json.loads(capsys.readouterr().out)["archive"]["jobs"] == len(ARCHIVED)
    lock = FileLock(str(state / "broker.lockfile"))
    lock.acquire()
    try:
        monkeypatch.setattr(sys, "argv", ["cardputer", "archive", "--state-dir", str(state)])
        with pytest.raises(SystemExit):
            cli.main()
    finally:
        lock.release()
    assert "stop_broker_before_history_archive" in capsys.readouterr().err


def test_pending_continuations_and_home_recovery_links_stay_live(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    store = Store(state / "history.sqlite3")
    for job_id in ("parent", "ticket-parent", "ticket-child", "home-source", "home-fix", "plain"):
        add(store, job_id)
    store.db.execute(
        "INSERT INTO deferred_jobs(job_id,plan_hash,parent_job_id,principal_id,origin,epoch,"
        "state,created) VALUES('deferred','d',?,?,'local_cli','e','pending',0)",
        ("parent", PRINCIPAL),
    )
    store.db.execute(
        "INSERT INTO continuation_tickets(ticket_id,parent_job_id,principal_id,epoch,state,"
        "consumed_by_job,chain_started,created) VALUES('t','ticket-parent',?,'e','issued',"
        "'ticket-child',0,0)",
        (PRINCIPAL,),
    )
    store.db.execute("UPDATE jobs SET state='needs_review',reviewed=0 WHERE id='home-source'")
    store.db.execute(
        "INSERT INTO home_recovery_audit(source_job,recovery_job,event) "
        "VALUES('home-source','home-fix','attempt_consumed')"
    )
    store.db.commit()
    store.close()
    archive_history(state, keep_latest=0)
    assert ids(state) == {"parent", "ticket-parent", "ticket-child", "home-source", "home-fix"}
    assert ids(state, "deferred_jobs", "job_id") == {"deferred"}
    assert ids(state, "continuation_tickets", "ticket_id") == {"t"}
    assert ids(state, "home_recovery_audit", "recovery_job") == {"home-fix"}


def test_existing_destination_is_refused_without_changes(tmp_path):
    state = tmp_path / "state"
    history(state)
    store = Store(state / "history.sqlite3")
    try:
        (tmp_path / "taken.sqlite3").write_bytes(b"")
        with pytest.raises(ValueError, match="history_archive_unavailable"):
            store.archive_rows(tmp_path / "taken.sqlite3", {"jobs": ["old-0"]}, {})
    finally:
        store.close()
    assert "old-0" in ids(state)
