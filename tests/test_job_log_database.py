"""The job-log SQLite store: replay order, batching, concurrency, damage and old files."""

import json
import sqlite3
import threading

from cardputer_mcp import job_logs
from cardputer_mcp.job_logs import JobLogStore

OWNER = "key:owner"


def raw(root, sql, args=()):
    connection = sqlite3.connect(root / job_logs.DATABASE_NAME)
    try:
        return connection.execute(sql, args).fetchall()
    finally:
        connection.close()


def test_database_is_wal_with_incremental_vacuum_and_a_clustered_key(tmp_path):
    store = JobLogStore(tmp_path)
    store.begin("job-1", OWNER, "observe").close()
    assert raw(tmp_path, "PRAGMA journal_mode") == [("wal",)]
    assert raw(tmp_path, "PRAGMA auto_vacuum") == [(2,)]
    assert raw(tmp_path, "PRAGMA user_version") == [(1,)]
    store.close()


def test_replaying_a_job_is_one_ordered_range_scan_without_a_sort(tmp_path):
    store = JobLogStore(tmp_path)
    log = store.begin("job-1", OWNER, "observe")
    for index in range(10):
        log.append("device", f"line {index}")
    log.close()
    plan = raw(
        tmp_path,
        "EXPLAIN QUERY PLAN SELECT seq, text FROM log_lines "
        "WHERE job_id=? AND seq>? ORDER BY seq LIMIT 5",
        ("job-1", 0),
    )
    detail = " ".join(row[3] for row in plan)
    assert "SEARCH log_lines" in detail and "TEMP B-TREE" not in detail
    store.close()


def test_entries_come_back_in_arrival_order_even_if_the_clock_goes_backwards(tmp_path, monkeypatch):
    ticks = iter([5_000, 4_000, 3_000, 2_000])
    monkeypatch.setattr(job_logs, "_millis", lambda: next(ticks, 1_000))
    store = JobLogStore(tmp_path)
    log = store.begin("job-1", OWNER, "observe")
    for text in ("first", "second", "third"):
        log.append("device", text)
    log.close()
    texts = [entry["text"] for entry in store.read("job-1", OWNER, "device")["entries"]]
    assert texts == ["first", "second", "third"]
    store.close()


def test_a_reader_sees_buffered_lines_of_a_running_job(tmp_path):
    store = JobLogStore(tmp_path)
    log = store.begin("job-1", OWNER, "observe")
    log.append("device", "still running")
    assert raw(tmp_path, "SELECT count(*) FROM log_lines") == [(0,)]  # buffered
    assert [e["text"] for e in store.read("job-1", OWNER, "device")["entries"]] == ["still running"]
    assert raw(tmp_path, "SELECT closed FROM log_jobs") == [(0,)]
    log.close()
    assert raw(tmp_path, "SELECT closed FROM log_jobs") == [(1,)]
    store.close()


def test_a_full_buffer_is_written_without_a_read(tmp_path):
    store = JobLogStore(tmp_path)
    log = store.begin("job-1", OWNER, "observe")
    for index in range(job_logs.FLUSH_ROWS):
        log.append("device", f"line {index}")
    assert raw(tmp_path, "SELECT count(*) FROM log_lines") == [(job_logs.FLUSH_ROWS,)]
    log.close()
    store.close()


def test_two_jobs_written_from_many_threads_keep_their_own_order(tmp_path):
    store = JobLogStore(tmp_path)
    logs = {name: store.begin(name, OWNER, "observe") for name in ("job-a", "job-b")}

    def write(name):
        for index in range(300):
            logs[name].append("device", f"{name} {index}")

    threads = [threading.Thread(target=write, args=(name,)) for name in logs]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    for name, log in logs.items():
        log.close()
        entries = store.read(name, OWNER, "device")["entries"]
        assert [e["text"] for e in entries] == [f"{name} {i}" for i in range(300)]
        assert not log.unavailable
    store.close()


def test_a_damaged_database_is_set_aside_and_logging_starts_over(tmp_path):
    store = JobLogStore(tmp_path)
    store.begin("job-1", OWNER, "observe").close()
    store.close()
    path = tmp_path / job_logs.DATABASE_NAME
    path.write_bytes(b"this is not a database" * 100)
    for suffix in ("-wal", "-shm"):
        (tmp_path / (job_logs.DATABASE_NAME + suffix)).unlink(missing_ok=True)
    fresh = JobLogStore(tmp_path)
    log = fresh.begin("job-2", OWNER, "observe")
    log.append("device", "after damage")
    log.close()
    assert [e["text"] for e in fresh.read("job-2", OWNER, "device")["entries"]] == ["after damage"]
    assert list(tmp_path.glob(job_logs.DATABASE_NAME + ".corrupt-*"))
    fresh.close()


def test_a_job_logged_before_the_switch_is_still_read_from_its_jsonl_file(tmp_path):
    lines = [
        {
            "schema_version": 1,
            "job_id": "old-job",
            "principal_id": OWNER,
            "kind": "firmware_observe",
            "created": "2026-01-01T00:00:00+00:00",
        },
        {"seq": 1, "t": "2026-01-01T00:00:01+00:00", "stream": "device", "level": "info",
         "text": "from the old file"},
    ]  # fmt: skip
    (tmp_path / "old-job.jsonl").write_bytes(
        b"".join(json.dumps(line, separators=(",", ":")).encode() + b"\n" for line in lines)
    )
    store = JobLogStore(tmp_path)
    assert store.owner("old-job") == OWNER
    assert [e["text"] for e in store.read("old-job", OWNER, "device")["entries"]] == [
        "from the old file"
    ]
    assert store.read("old-job", "key:other", "device") is None
    # Nothing is ever written to a .jsonl file any more.
    log = store.begin("new-job", OWNER, "observe")
    log.append("device", "new")
    log.close()
    assert sorted(path.name for path in tmp_path.glob("*.jsonl")) == ["old-job.jsonl"]
    store.close()


def test_closing_the_store_closes_open_logs_and_releases_the_file(tmp_path):
    store = JobLogStore(tmp_path)
    log = store.begin("job-1", OWNER, "observe")
    log.append("device", "kept")
    store.close()
    assert raw(tmp_path, "SELECT closed FROM log_jobs") == [(1,)]
    assert raw(tmp_path, "SELECT text FROM log_lines") == [("kept",)]
    (tmp_path / job_logs.DATABASE_NAME).unlink()  # no open handle left on Windows
